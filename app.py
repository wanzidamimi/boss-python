import csv
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from openpyxl import Workbook
from pydantic import BaseModel

import account_manager as am
import db

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
EXPORT_DIR = DATA_DIR / "exports"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

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
    detail_limit: int = 0
    delay: float = 3.0
    account_id: str = ""   # 指定账号；留空则轮询一个有效账号


class AccountLogin(BaseModel):
    label: str = ""


class OpenLinkRequest(BaseModel):
    url: str
    task_id: str = ""


def run_cmd(args, timeout):
    result = subprocess.run(
        [sys.executable] + args,
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "unknown error")[-3000:])
    return result.stdout


def worker():
    global last_task_started
    while True:
        task = task_queue.get()
        task_id = task["task_id"]
        account_id = task.get("account_id", "")
        port = task.get("cdp_port", 0)
        cookie_path = task.get("cookie_path", "")
        browser_started = False
        try:
            with queue_lock:
                elapsed = time.time() - last_task_started
                if elapsed < MIN_TASK_INTERVAL:
                    time.sleep(MIN_TASK_INTERVAL - elapsed)
                last_task_started = time.time()

            # 执行前二次探活：失效账号不执行任务
            if account_id:
                check = am.probe_account(account_id)
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
                am.launch_browser(port, am.profile_dir(account_id))
                browser_started = True
                time.sleep(am.PAGE_SETTLE_SECONDS)

            db.update_task_status(task_id, "running", "refreshing stoken")
            stoken_cmd = ["stoken_browser.py"]
            if port:
                stoken_cmd += ["--port", str(port)]
            if cookie_path:
                stoken_cmd += ["--cookies", cookie_path]
            run_cmd(stoken_cmd, timeout=180)

            if STOKEN_WAIT > 0:
                time.sleep(STOKEN_WAIT)

            db.update_task_status(task_id, "running", "calling boss api")
            prefix = f"task_{task_id}"
            cmd = [
                "job_api.py",
                "--mode", task["mode"],
                "--pages", str(task["pages"]),
                "--page-size", str(task["page_size"]),
                "--detail-limit", str(task["detail_limit"]),
                "--delay", str(task["delay"]),
                "--prefix", prefix,
            ]
            if task["mode"] == "search":
                cmd.extend(["--keyword", task["keyword"]])
            if task["city"]:
                cmd.extend(["--city", task["city"]])
            if cookie_path:
                cmd += ["--cookies", cookie_path]
            if port:
                cmd += ["--port", str(port)]

            run_cmd(cmd, timeout=1800)

            candidates = sorted(
                DATA_DIR.glob(f"{prefix}_*.json"),
                key=lambda p: p.stat().st_mtime,
            )
            if not candidates:
                raise RuntimeError("job_api.py produced no JSON output")

            rows = json.loads(candidates[-1].read_text(encoding="utf-8"))
            inserted = db.insert_jobs(task_id, rows)
            if account_id:
                db.update_account(account_id, last_used_at=datetime.now())
            db.update_task_status(
                task_id,
                "success",
                f"saved {inserted} jobs",
                rows_count=inserted,
                finished=True,
            )
        except Exception as exc:
            db.update_task_status(
                task_id,
                "failed",
                str(exc)[:1000],
                finished=True,
            )
        finally:
            if browser_started and port:
                am.close_browser(port)
            task_queue.task_done()


@app.on_event("startup")
def startup_event():
    db.init_db()
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
    if req.detail_limit < 0 or req.detail_limit > 3:
        raise HTTPException(status_code=400, detail="detail_limit must be 0-3")
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
    task_queue.put(task)
    return {"task_id": task_id, "status": "queued", "warning": warning,
            "account_label": task["account_label"]}


@app.get("/api/tasks")
def tasks():
    try:
        return db.list_tasks(50)
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


@app.get("/api/stats")
def stats():
    try:
        return db.collect_stats()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/jobs")
def jobs(task_id: str = "", keyword: str = "", city: str = "", limit: int = 50):
    try:
        return db.query_jobs(task_id, keyword, city, min(limit, 200), 0)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/export")
def export(format: str = "csv", task_id: str = "", keyword: str = "", city: str = ""):
    try:
        rows = db.query_all_jobs(task_id, keyword, city)
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
