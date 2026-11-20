import csv
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from openpyxl import Workbook
from pydantic import BaseModel

import account_manager as am
import db
import progress_store
from detail_hash import flatten_detail

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
EXPORT_DIR = DATA_DIR / "exports"
LOG_DIR = DATA_DIR / "logs"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

task_queue = queue.Queue()
last_task_started = 0.0
queue_lock = threading.Lock()
MIN_TASK_INTERVAL = float(os.getenv("BOSS_MIN_TASK_INTERVAL", "300"))
# 浏览器刷新后产出的 stoken 即可用，等待时间主要用于让服务端完成状态同步
STOKEN_WAIT = float(os.getenv("BOSS_STOKEN_WAIT", "60"))

app = FastAPI(title="Boss 招聘采集服务")


class TaskRequest(BaseModel):
    mode: str = "recommend"
    keyword: str = ""
    city: str = ""
    pages: int = 1
    page_size: int = 15
    detail_limit: int = -1          # -1=全部；0=不抓；N=前 N 条
    detail_interval: float = 20.0   # 详情请求间隔秒数（浏览器引擎下可缩短）
    delay: float = 3.0
    account_id: str = ""   # 指定账号；留空则轮询一个有效账号


class AccountLogin(BaseModel):
    label: str = ""


class OpenLinkRequest(BaseModel):
    url: str
    task_id: str = ""


def log_line(log_path, message):
    """把一条带时间戳的记录追加到任务日志。"""
    if not log_path:
        return
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fh.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def run_cmd(args, timeout, log_path=None):
    log_line(log_path, "$ " + " ".join(str(item) for item in args))
    result = subprocess.run(
        [sys.executable] + args,
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                if result.stdout:
                    fh.write(result.stdout)
                if result.stderr:
                    fh.write("[stderr]\n" + result.stderr)
                fh.write("\n")
        except OSError:
            pass
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "unknown error")[-3000:])
    return result.stdout


def save_details(task_id, prefix, account_id, log_path=None):
    """把 job_api 产出的详情信封入库，返回 (ok, fail, skipped, status)。

    status: none / full / partial / blocked
    """
    files = sorted(
        DATA_DIR.glob(f"{prefix}_details_*.json"),
        key=lambda p: p.stat().st_mtime,
    )
    if not files:
        log_line(log_path, "detail envelope not found")
        return 0, 0, 0, "none"
    log_line(log_path, f"detail envelope: {files[-1]}")
    envelope = json.loads(files[-1].read_text(encoding="utf-8"))
    requested = int(envelope.get("requested", 0))
    skipped = int(envelope.get("skipped", 0))
    log_line(
        log_path,
        f"detail envelope summary: requested={requested} ok={envelope.get('ok')} "
        f"failed={envelope.get('failed')} skipped={skipped} "
        f"aborted={envelope.get('aborted') or '-'}",
    )
    if requested == 0:
        return 0, 0, 0, "none"

    ok = fail = 0
    request_rows = []
    for item in envelope.get("items", []):
        raw_time = item.get("requested_at") or ""
        try:
            requested_at = datetime.fromisoformat(raw_time)
        except ValueError:
            requested_at = datetime.now()
        request_rows.append({
            "account_id": account_id,
            "task_id": task_id,
            "lid": item.get("lid", ""),
            "requested_at": requested_at,
            "api_code": item.get("api_code", -1),
            "ok": item.get("api_code") == 0,
            "note": "aborted" if envelope.get("aborted") else "",
        })
        if item.get("api_code") == 0 and item.get("zpData"):
            try:
                row = flatten_detail(
                    item["zpData"], item["lid"], item.get("security_id", "")
                )
                db.insert_detail_snapshot(task_id, row, requested_at)
                ok += 1
            except Exception as exc:
                fail += 1
                print(f"[detail] save failed lid={item.get('lid')}: {exc}", flush=True)
                log_line(log_path, f"detail save failed lid={item.get('lid')}: {exc}")
        else:
            fail += 1

    if request_rows:
        try:
            db.log_detail_requests(request_rows)
        except Exception as exc:
            print(f"[detail] log requests failed: {exc}", flush=True)

    if envelope.get("aborted"):
        if account_id and str(envelope.get("aborted", "")).startswith("code="):
            db.set_account_detail_block(account_id, datetime.now() + timedelta(minutes=30))
        return ok, fail, skipped, "blocked"
    if fail == 0:
        return ok, fail, skipped, "full"
    return ok, fail, skipped, "partial"


def worker():
    global last_task_started
    while True:
        task = task_queue.get()
        task_id = task["task_id"]
        log_path = LOG_DIR / f"task_{task_id}.log"
        log_line(log_path, f"=== task start {task_id} ===")
        log_line(
            log_path,
            "params: mode={mode} keyword={keyword} city={city} pages={pages} "
            "page_size={page_size} detail_limit={detail_limit} "
            "detail_interval={detail_interval} account={account} port={port}".format(
                mode=task.get("mode"), keyword=task.get("keyword"),
                city=task.get("city"), pages=task.get("pages"),
                page_size=task.get("page_size"),
                detail_limit=task.get("detail_limit"),
                detail_interval=task.get("detail_interval"),
                account=task.get("account_id"), port=task.get("cdp_port"),
            ),
        )
        account_id = task.get("account_id", "")
        port = task.get("cdp_port", 0)
        cookie_path = task.get("cookie_path", "")
        browser_started = False
        progress_store.write_progress(
            task_id,
            phase="prepare",
            message="排队准备",
            pages=task.get("pages"),
            done=0,
            total=0,
            ok=0,
            failed=0,
            skipped=0,
            started_at=datetime.now().isoformat(timespec="seconds"),
        )
        try:
            with queue_lock:
                elapsed = time.time() - last_task_started
                if elapsed < MIN_TASK_INTERVAL:
                    time.sleep(MIN_TASK_INTERVAL - elapsed)
                last_task_started = time.time()

            # 执行前二次探活：失效账号不执行任务
            if account_id:
                check = am.probe_account(account_id)
                log_line(log_path, f"probe account: valid={check['valid']} code={check['code']}")
                progress_store.write_progress(task_id, phase="probe",
                                              message="检测账号登录态")
                if not check["valid"]:
                    db.update_account(account_id, status="expired",
                                      last_probe_at=datetime.now())
                    raise RuntimeError(
                        f"账号 {account_id} 凭证已失效(code={check['code']}: {check['message']})，"
                        "请在账号管理里重新登录"
                    )
                db.update_account(account_id, last_probe_at=datetime.now())

            # 启动该账号的浏览器（独立 profile + 端口），用完关闭、保留 profile
            if account_id and port:
                db.update_task_status(task_id, "running", "launching account browser")
                print(f"[task {task_id}] launching browser on {port}", flush=True)
                log_line(log_path, f"phase: launching browser on {port}")
                am.launch_browser(port, am.profile_dir(account_id))
                browser_started = True
                progress_store.write_progress(task_id, phase="browser",
                                              message="启动浏览器")
                time.sleep(am.PAGE_SETTLE_SECONDS)

            print(f"[task {task_id}] refreshing stoken", flush=True)
            log_line(log_path, "phase: refreshing stoken")
            progress_store.write_progress(task_id, phase="stoken",
                                          message="刷新登录令牌")
            db.update_task_status(task_id, "running", "refreshing stoken")
            stoken_cmd = ["stoken_browser.py"]
            if port:
                stoken_cmd += ["--port", str(port)]
            if cookie_path:
                stoken_cmd += ["--cookies", cookie_path]
            run_cmd(stoken_cmd, timeout=180, log_path=log_path)
            print(f"[task {task_id}] stoken refreshed", flush=True)
            log_line(log_path, "phase: stoken refreshed")

            if STOKEN_WAIT > 0:
                time.sleep(STOKEN_WAIT)

            # 详情阶段：账号处于风控冷却期时跳过，避免继续触发 code=37
            detail_limit = int(task.get("detail_limit", -1))
            detail_interval = float(task.get("detail_interval", 20))
            detail_skip_note = ""
            if detail_limit and account_id and db.is_account_detail_blocked(account_id):
                detail_limit = 0
                detail_skip_note = "详情处于冷却期，本次跳过"

            print(f"[task {task_id}] calling boss api (detail_limit={detail_limit})", flush=True)
            log_line(log_path, "phase: calling boss api")
            progress_store.write_progress(task_id, phase="list",
                                          message="抓取职位列表", page=0,
                                          pages=task.get("pages"))
            db.update_task_status(task_id, "running", "calling boss api")
            prefix = f"task_{task_id}"
            cmd = [
                "job_api.py",
                "--mode", task["mode"],
                "--pages", str(task["pages"]),
                "--page-size", str(task["page_size"]),
                "--detail-limit", str(detail_limit),
                "--detail-interval", str(detail_interval),
                "--delay", str(task["delay"]),
                "--prefix", prefix,
                "--task-id", task_id,
            ]
            if task["mode"] == "search":
                cmd.extend(["--keyword", task["keyword"]])
            if task["city"]:
                cmd.extend(["--city", task["city"]])
            if cookie_path:
                cmd += ["--cookies", cookie_path]
            if port:
                cmd += ["--port", str(port)]

            # 详情阶段会按间隔串行请求，超时时间随详情条数放大
            max_jobs = int(task.get("pages", 1)) * int(task.get("page_size", 15))
            detail_count = max_jobs if detail_limit < 0 else min(detail_limit, max_jobs)
            run_cmd(cmd, timeout=1800 + int(detail_count * detail_interval) + 300,
                    log_path=log_path)

            candidates = sorted(
                DATA_DIR.glob(f"{prefix}_*.json"),
                key=lambda p: p.stat().st_mtime,
            )
            # 详情信封也是 *_details_*.json，避免把它当成列表结果
            candidates = [p for p in candidates if "_details_" not in p.name]
            if not candidates:
                raise RuntimeError("job_api.py produced no JSON output")

            rows = json.loads(candidates[-1].read_text(encoding="utf-8"))
            inserted = db.insert_jobs(task_id, rows)

            detail_ok = detail_fail = detail_skipped = 0
            detail_status = "none"
            if task.get("detail_limit"):
                if detail_skip_note:
                    detail_status = "blocked"
                    log_line(log_path, f"details skipped: {detail_skip_note}")
                else:
                    detail_ok, detail_fail, detail_skipped, detail_status = save_details(
                        task_id, prefix, account_id, log_path=log_path
                    )

            db.update_task_detail(task_id, detail_ok, detail_fail, detail_status)
            if account_id:
                db.update_account(account_id, last_used_at=datetime.now())

            message = f"saved {inserted} jobs"
            if task.get("detail_limit"):
                if detail_status == "blocked" and detail_skip_note:
                    message += f", {detail_skip_note}"
                elif detail_status == "none":
                    message += ", 无列表数据可抓详情"
                else:
                    message += f", details {detail_ok}/{detail_ok + detail_fail}"
                    if detail_skipped:
                        message += f", skipped {detail_skipped}"
                    if detail_status == "blocked":
                        message += " (风控中止)"
                    elif detail_status == "partial":
                        message += " (部分失败)"

            db.update_task_status(
                task_id,
                "success",
                message,
                rows_count=inserted,
                finished=True,
            )
            log_line(log_path, f"success: {message} (rows={inserted})")
            progress_store.write_progress(
                task_id,
                phase="done",
                done=detail_ok + detail_fail,
                total=detail_ok + detail_fail + detail_skipped,
                ok=detail_ok,
                failed=detail_fail,
                skipped=detail_skipped,
                message=message,
                eta_seconds=0,
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
        except Exception as exc:
            log_line(log_path, "ERROR:\n" + traceback.format_exc())
            progress_store.write_progress(
                task_id,
                phase="failed",
                message=str(exc)[:300],
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
            db.update_task_status(
                task_id,
                "failed",
                str(exc)[:1000],
                finished=True,
            )
        finally:
            log_line(log_path, f"=== task end {task_id} ===")
            if browser_started and port:
                am.close_browser(port)
            task_queue.task_done()


@app.on_event("startup")
def startup_event():
    db.init_db()
    progress_store.cleanup_progress(7)
    threading.Thread(target=worker, daemon=True).start()


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/cities")
def cities():
    """返回 BOSS 城市中文名 -> 9 位编码码表，供前端城市选择使用。"""
    path = STATIC_DIR / "city_codes.json"
    try:
        return JSONResponse(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"city codes load error: {exc}")


# ---------------------------------------------------------------------------
# 账号管理 API
# ---------------------------------------------------------------------------

@app.get("/api/accounts")
def api_list_accounts():
    """列出账号并附带实时探活状态（探活结果不落库，仅展示）。"""
    try:
        rows = db.list_accounts()
        result = []
        for row in rows:
            probe = am.probe_account(row["account_id"])
            result.append({
                "account_id": row["account_id"],
                "label": row["label"],
                "boss_user_id": row["boss_user_id"],
                "cdp_port": row["cdp_port"],
                "status": "active" if probe["valid"] else "expired",
                "db_status": row["status"],
                "probe_code": probe["code"],
                "probe_message": probe["message"],
                "last_used_at": row["last_used_at"],
                "detail_blocked_until": row.get("detail_blocked_until"),
                "created_at": row["created_at"],
            })
            # 同步探活状态到库，供轮询选号用
            db.update_account(row["account_id"],
                              status="active" if probe["valid"] else "expired",
                              last_probe_at=datetime.now())
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/accounts/login")
def api_login_account(req: AccountLogin):
    """创建账号记录并弹出浏览器，让用户手动扫码/手机号登录。"""
    label = (req.label or "").strip() or "新账号"
    account_id = uuid.uuid4().hex[:10]
    try:
        port = am.next_port()
        db.create_account({"account_id": account_id, "label": label, "cdp_port": port})
        am.launch_browser(port, am.profile_dir(account_id))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"启动登录浏览器失败: {exc}")
    return {"account_id": account_id, "cdp_port": port,
            "message": "浏览器已打开，请在窗口内扫码或手机号登录"}


@app.get("/api/accounts/{account_id}/login-status")
def api_login_status(account_id: str):
    """前端轮询：检查浏览器登录是否完成；完成则保存凭证并置 active。"""
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    result = am.check_browser_login(account_id, account["cdp_port"])
    if result.get("done"):
        db.update_account(account_id, status="active",
                          boss_user_id=result.get("boss_user_id", ""),
                          last_probe_at=datetime.now())
    return result


@app.delete("/api/accounts/{account_id}")
def api_delete_account(account_id: str):
    account = db.get_account(account_id)
    if account:
        am.close_browser(account["cdp_port"])
    db.delete_account(account_id)
    for path in (am.cookie_file(account_id),):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    import shutil
    shutil.rmtree(am.profile_dir(account_id), ignore_errors=True)
    return {"ok": True}


@app.post("/api/accounts/{account_id}/relogin")
def api_relogin_account(account_id: str):
    """凭证失效后重新登录：关闭旧浏览器，用同一 profile 重新弹出登录窗口。"""
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    am.close_browser(account["cdp_port"])
    try:
        am.launch_browser(account["cdp_port"], am.profile_dir(account_id))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"启动浏览器失败: {exc}")
    db.update_account(account_id, status="expired")
    return {"account_id": account_id, "cdp_port": account["cdp_port"],
            "message": "浏览器已打开，请重新登录"}


@app.post("/api/open-link")
def api_open_link(req: OpenLinkRequest):
    """在带登录态的浏览器里打开职位链接。

    优先找到采集该职位的任务所用账号，用其登录浏览器打开；否则用默认 9223 浏览器。
    """
    from urllib.parse import urlparse

    url = (req.url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not (
        parsed.netloc == "www.zhipin.com" or parsed.netloc.endswith(".zhipin.com")
    ):
        raise HTTPException(status_code=400, detail="只允许打开 zhipin.com 的链接")

    account_id = ""
    if req.task_id:
        task = db.get_task(req.task_id)
        if task:
            account_id = task.get("account_id", "") or ""

    result = am.open_link(url, account_id=account_id)
    return result


@app.post("/api/tasks")
def create_task(req: TaskRequest):
    if req.mode not in ("recommend", "search"):
        raise HTTPException(status_code=400, detail="mode must be recommend or search")
    if req.mode == "search" and not req.keyword.strip():
        raise HTTPException(status_code=400, detail="search mode requires keyword")
    if req.pages < 1 or req.pages > 10:
        raise HTTPException(status_code=400, detail="pages must be 1-10")
    if req.detail_limit < -1:
        raise HTTPException(status_code=400, detail="detail_limit must be -1, 0 or a positive number")
    if req.detail_interval < 2 or req.detail_interval > 600:
        raise HTTPException(status_code=400, detail="detail_interval must be 2-600 seconds")
    if req.delay < 2:
        raise HTTPException(status_code=400, detail="delay must be >= 2 seconds")

    # 选账号：指定则校验有效性；否则轮询一个有效账号。
    account = None
    warning = ""
    if req.account_id:
        account = db.get_account(req.account_id)
        if not account:
            raise HTTPException(status_code=404, detail=f"账号不存在: {req.account_id}")
        check = am.probe_account(req.account_id)
        db.update_account(req.account_id, last_probe_at=datetime.now(),
                          status="active" if check["valid"] else "expired")
        if not check["valid"]:
            raise HTTPException(
                status_code=400,
                detail=f"账号「{account['label']}」凭证已失效(code={check['code']}: "
                       f"{check['message']})，请在账号管理里重新登录",
            )
    else:
        account = db.pick_round_robin_account()
        if not account:
            warning = "没有有效账号，已降级使用默认凭证；建议先在账号管理里添加并登录账号"

    task_id = uuid.uuid4().hex[:12]
    task = {
        "task_id": task_id,
        "mode": req.mode,
        "keyword": req.keyword.strip(),
        "city": req.city.strip(),
        "pages": req.pages,
        "page_size": req.page_size,
        "detail_limit": req.detail_limit,
        "detail_interval": req.detail_interval,
        "delay": req.delay,
        "account_id": account["account_id"] if account else "",
        "account_label": account["label"] if account else "",
        "cdp_port": account["cdp_port"] if account else 0,
        "cookie_path": str(am.cookie_file(account["account_id"])) if account else "",
    }
    try:
        db.create_task(task)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"database error: {exc}")
    progress_store.write_progress(
        task_id,
        phase="queued",
        message="排队中",
        pages=req.pages,
        done=0,
        total=0,
        ok=0,
        failed=0,
        skipped=0,
        started_at=datetime.now().isoformat(timespec="seconds"),
    )
    task_queue.put(task)
    return {"task_id": task_id, "status": "queued", "warning": warning,
            "account_label": task["account_label"]}


@app.get("/api/tasks")
def tasks():
    try:
        rows = db.list_tasks(50)
        for row in rows:
            if row.get("status") in ("queued", "running"):
                row["progress"] = progress_store.read_progress(row["task_id"])
        return rows
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str):
    try:
        task = db.get_task(task_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@app.get("/api/tasks/{task_id}/progress")
def task_progress(task_id: str):
    """返回任务实时进度（data/progress/task_<id>.json）。"""
    data = progress_store.read_progress(task_id)
    if not data:
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        data = {
            "task_id": task_id,
            "phase": task.get("status"),
            "message": task.get("message", ""),
        }
    return data


@app.get("/api/tasks/{task_id}/log")
def task_log(task_id: str):
    """返回某个任务的持久化日志（data/logs/task_<id>.log）。"""
    path = LOG_DIR / f"task_{task_id}.log"
    if not path.exists():
        raise HTTPException(status_code=404, detail="log not found")
    return JSONResponse({
        "task_id": task_id,
        "log": path.read_text(encoding="utf-8", errors="replace"),
    })


@app.get("/api/stats")
def stats():
    try:
        return db.collect_stats()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/jobs")
def jobs(task_id: str = "", keyword: str = "", city: str = "", limit: int = 50):
    try:
        return db.query_jobs(task_id, keyword, city, min(limit, 200), 0,
                             with_detail=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/details/{lid}")
def detail(lid: str):
    """返回某职位的最新详情快照与版本列表。"""
    try:
        latest = db.get_latest_detail(lid)
        if not latest:
            raise HTTPException(status_code=404, detail="detail not found")
        latest["version_count"] = db.count_detail_versions(lid)
        latest["versions"] = db.list_detail_versions(lid)
        return latest
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/export")
def export(format: str = "csv", task_id: str = "", keyword: str = "",
           city: str = "", with_details: int = 0):
    try:
        rows = db.query_all_jobs(task_id, keyword, city,
                                 with_detail=bool(with_details))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    token = uuid.uuid4().hex[:8]
    if format == "csv":
        path = EXPORT_DIR / f"boss_{token}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
            writer.writeheader()
            writer.writerows(rows)
        return FileResponse(path, filename=path.name, media_type="text/csv")

    if format == "excel":
        path = EXPORT_DIR / f"boss_{token}.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "jobs"
        if rows:
            headers = list(rows[0].keys())
            ws.append(headers)
            for row in rows:
                ws.append([row.get(h, "") for h in headers])
        wb.save(path)
        return FileResponse(
            path,
            filename=path.name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    return JSONResponse(status_code=400, content={"detail": "format must be csv or excel"})
