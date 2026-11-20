import argparse
import csv
import json
import random
import secrets
import string
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from config import BASE_DIR, CAPTURE_DIR, DATA_DIR
from detail_hash import flatten_detail
import progress_store

# 服务端在登录态下会把列表接口的 jobExperience 统一降级成同一个值，
# 真实经验只在职位详情接口返回；采集详情时以详情为准。
DEGRADED_EXPERIENCE = {"10年以上"}

# 可自愈的风控/令牌错误码
TOKEN_CODES = {120, 121, 122}
RISK_CODES = {35, 36, 37}
BACKOFF_SECONDS = 20
STOKEN_TIMEOUT = 180

# 详情阶段的节奏（浏览器引擎下由页面自动轮换 stoken，无条数上限）
DETAIL_REFRESH_PAUSE = 30      # 主动刷新令牌后的缓冲（仅 requests 引擎）
DETAIL_RISK_WAIT = 60          # 命中风控后刷新令牌再重试的等待
DETAIL_DEFAULT_INTERVAL = 20.0

BASE = "https://www.zhipin.com"
RECOMMEND_URL = f"{BASE}/wapi/zpgeek/pc/recommend/job/list.json"
SEARCH_URL = f"{BASE}/wapi/zpgeek/search/joblist.json"
DETAIL_URL = f"{BASE}/wapi/zpgeek/job/detail.json"

ERROR_HINT = {
    7: "登录状态失效，需要重新扫码",
    17: "账号或环境异常，需要更新登录态",
    31: "IP 被封禁",
    32: "账号被封禁",
    35: "IP 灰度验证",
    36: "账号灰度验证",
    37: "触发安全校验",
    38: "触发反爬登录",
    120: "zpToken 需要刷新",
    121: "zpToken 无效",
    122: "zpToken 过期，需要重试",
}


class BossApiError(RuntimeError):
    def __init__(self, code: int, message: str):
        hint = ERROR_HINT.get(code, "")
        text = f"Boss API error: code={code}, message={message}"
        if hint:
            text += f", hint={hint}"
        super().__init__(text)
        self.code = code
        self.message = message


def load_latest_capture(capture_path: str | None = None):
    if capture_path:
        path = Path(capture_path)
    else:
        candidates = sorted(
            CAPTURE_DIR.glob("boss_*.json"),
            key=lambda p: p.stat().st_mtime,
        )
        if not candidates:
            raise FileNotFoundError(f"No capture file found in {CAPTURE_DIR}")
        path = candidates[-1]

    capture = json.loads(path.read_text(encoding="utf-8"))
    request_meta = None
    for item in capture.get("requests", []):
        url = item.get("url", "")
        if "recommend/job/list.json" in url or "search/joblist.json" in url:
            request_meta = item.get("request", {})
            break

    if not request_meta:
        raise RuntimeError(f"No job list request found in capture: {path}")

    cookies = {
        item["name"]: item["value"]
        for item in capture.get("cookies", [])
        if "name" in item and "value" in item
    }
    headers = dict(request_meta.get("headers") or {})
    headers.pop("Content-Length", None)
    headers.pop("content-length", None)
    headers.pop("Host", None)
    headers.pop("host", None)
    # 抓包里的 token / zp_token 是录制当时的静态值，服务端滚动 seed/sts 后即失效，
    # 带着它们请求会被判定"环境异常"(code=37)。这里一律丢弃，改由 Cookie 动态派生。
    headers.pop("token", None)
    headers.pop("zp_token", None)

    captured_url = request_meta.get("url", "")
    query = parse_qs(urlparse(captured_url).query)
    captured_city = query.get("city", ["101120200"])[0]

    return headers, cookies, captured_city, path


def new_trace_id() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "F-" + "".join(secrets.choice(alphabet) for _ in range(24))


def now_ms() -> int:
    return int(time.time() * 1000)


def make_session(cookies: dict) -> requests.Session:
    session = requests.Session()
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=".zhipin.com", path="/")
    return session


def apply_auth_headers(headers: dict, session: requests.Session) -> dict:
    """按当前 Cookie 派生鉴权请求头。

    zp_token 实为 Cookie `bst` 的副本，两者必须一致；陈旧值会导致 code=37。
    """
    merged = dict(headers)
    merged.pop("token", None)
    merged.pop("zp_token", None)
    bst = session.cookies.get("bst")
    if bst:
        merged["zp_token"] = bst
    return merged


def dump_cookies(session: requests.Session) -> list[dict]:
    return [
        {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path,
        }
        for cookie in session.cookies
    ]


def load_cookie_file(path: Path) -> dict:
    items = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(items, dict):
        items = items.get("cookies", [])
    return {item["name"]: item["value"] for item in items if "name" in item}


def save_cookie_file(session: requests.Session, path: Path):
    payload = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "cookies": dump_cookies(session),
    }
    # 保留 stoken_browser 写入的 stoken_generated_at 等顶层字段，
    # 否则稳定性监控读不到令牌生成时间，token_age_min 会永远显示 0。
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            for key, value in old.items():
                if key not in payload:
                    payload[key] = value
        except (ValueError, OSError):
            pass
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def refresh_zp_token(session: requests.Session, base_headers: dict) -> dict:
    headers = {
        key: value
        for key, value in base_headers.items()
        if key.lower() not in ("token", "zp_token", "traceid", "content-length")
    }
    headers["traceId"] = new_trace_id()
    response = session.post(
        f"{BASE}/wapi/zppassport/set/zpToken",
        headers=headers,
        timeout=20,
    )
    response.raise_for_status()
    payload = check_payload(response.json())
    # set/zpToken 不在响应体里返回新令牌，而是通过 Set-Cookie 下发新的 bst；
    # 请求头 zp_token 必须与之保持一致，否则服务端判定"环境异常"(code=37)。
    new_bst = session.cookies.get("bst")
    if new_bst:
        base_headers["zp_token"] = new_bst
        print(f"[refresh] zp_token synced from bst cookie, len={len(new_bst)}")
    return payload


def reload_session_cookies(session: requests.Session, path: Path | None = None) -> None:
    """把磁盘上最新的 Cookie 同步进当前 session（stoken 刷新后必须调用）。"""
    cookie_path = path or (DATA_DIR / "cookies_logged.json")
    if not cookie_path.exists():
        return
    for name, value in load_cookie_file(cookie_path).items():
        session.cookies.set(name, value, domain=".zhipin.com", path="/")


def refresh_stoken_runtime(
    session: requests.Session, cookie_path: Path | None = None, port: int = 0
) -> str:
    """重新触发浏览器产出 stoken（刷新目标页 + 读取浏览器 Cookie），并同步回 session。"""
    cmd = [sys.executable, "stoken_browser.py"]
    if port:
        cmd += ["--port", str(port)]
    if cookie_path:
        cmd += ["--cookies", str(cookie_path)]
    result = subprocess.run(
        cmd,
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=STOKEN_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout)[-2000:])
    reload_session_cookies(session, cookie_path)
    return result.stdout.strip()


def with_token_refresh(
    session: requests.Session,
    base_headers: dict,
    func,
    *args,
    retries: int = 1,
    cookie_path: Path | None = None,
    port: int = 0,
    **kwargs,
):
    try:
        return func(session, base_headers, *args, **kwargs)
    except BossApiError as exc:
        if exc.code not in (TOKEN_CODES | RISK_CODES) or retries <= 0:
            raise
        print(f"[refresh] code={exc.code}, message={exc.message}")
        if exc.code in TOKEN_CODES:
            refresh_zp_token(session, base_headers)
            time.sleep(2)
        else:
            # 环境类风控：stoken 与服务端期望失配，重新触发浏览器产令牌并退避
            try:
                refresh_stoken_runtime(session, cookie_path, port)
            except Exception as refresh_exc:
                print(f"[refresh] stoken refresh failed: {refresh_exc}")
            time.sleep(BACKOFF_SECONDS)
        return with_token_refresh(
            session,
            base_headers,
            func,
            *args,
            retries=retries - 1,
            cookie_path=cookie_path,
            port=port,
            **kwargs,
        )


def check_payload(payload: dict) -> dict:
    code = payload.get("code")
    if code != 0:
        raise BossApiError(int(code or -1), str(payload.get("message", "")))
    return payload.get("zpData", {}) or {}


def fetch_recommend(
    session: requests.Session,
    base_headers: dict,
    city: str,
    page: int,
    page_size: int,
) -> dict:
    headers = apply_auth_headers(base_headers, session)
    headers["traceId"] = new_trace_id()
    params = {
        "page": page,
        "pageSize": page_size,
        "city": city,
        "encryptExpectId": "",
        "mixExpectType": "",
        "expectInfo": "",
        "jobType": "",
        "salary": "",
        "experience": "",
        "degree": "",
        "industry": "",
        "scale": "",
        "_": now_ms(),
    }
    response = session.get(
        RECOMMEND_URL,
        params=params,
        headers=headers,
        timeout=20,
    )
    response.raise_for_status()
    return check_payload(response.json())


def fetch_search(
    session: requests.Session,
    base_headers: dict,
    keyword: str,
    city: str,
    page: int,
    page_size: int,
) -> dict:
    headers = apply_auth_headers(base_headers, session)
    headers["traceId"] = new_trace_id()
    headers["Content-Type"] = "application/x-www-form-urlencoded"

    form = {
        "page": page,
        "pageSize": page_size,
        "city": city,
        "expectInfo": "",
        "query": keyword,
        "multiSubway": "",
        "multiBusinessDistrict": "",
        "position": "",
        "jobType": "",
        "salary": "",
        "experience": "",
        "degree": "",
        "industry": "",
        "scale": "",
        "stage": "",
        "scene": "1",
        "encryptExpectId": "",
    }
    response = session.post(
        SEARCH_URL,
        params={"_": now_ms()},
        data=form,
        headers=headers,
        timeout=20,
    )
    response.raise_for_status()
    return check_payload(response.json())


def fetch_detail(
    session: requests.Session,
    base_headers: dict,
    security_id: str,
    lid: str,
) -> dict:
    headers = apply_auth_headers(base_headers, session)
    headers["traceId"] = new_trace_id()
    response = session.get(
        DETAIL_URL,
        params={
            "securityId": security_id,
            "lid": lid,
            "_": now_ms(),
        },
        headers=headers,
        timeout=20,
    )
    response.raise_for_status()
    return check_payload(response.json())


def flatten_job(job: dict, detail: dict | None = None) -> dict:
    job_info = (detail or {}).get("jobInfo", {})
    boss_info = (detail or {}).get("bossInfo", {})
    brand_info = (detail or {}).get("brandComInfo", {})
    encrypt_job_id = job.get("encryptJobId", "")
    security_id = job.get("securityId", "")
    lid = job.get("lid", "")
    detail_url = (
        f"{BASE}/job_detail/{encrypt_job_id}.html"
        f"?lid={lid}&securityId={security_id}"
        if encrypt_job_id
        else ""
    )
    return {
        "jobName": job_info.get("jobName") or job.get("jobName", ""),
        # 详情接口字段可信度高于列表接口：登录态下列表的经验/学历会被服务端统一降级
        "salaryDesc": job_info.get("salaryDesc") or job.get("salaryDesc", ""),
        "jobExperience": job_info.get("experienceName") or job.get("jobExperience", ""),
        "jobDegree": job_info.get("degreeName") or job.get("jobDegree", ""),
        "cityName": job.get("cityName") or job_info.get("locationName", ""),
        "areaDistrict": job.get("areaDistrict", ""),
        "businessDistrict": job.get("businessDistrict", ""),
        "brandName": job.get("brandName") or brand_info.get("brandName", ""),
        "brandIndustry": job.get("brandIndustry") or brand_info.get("industryName", ""),
        "brandScaleName": job.get("brandScaleName") or brand_info.get("scaleName", ""),
        "bossName": job.get("bossName") or boss_info.get("name", ""),
        "bossTitle": job.get("bossTitle") or boss_info.get("title", ""),
        "skills": "|".join(job.get("skills") or job_info.get("showSkills") or []),
        "jobLabels": "|".join(job.get("jobLabels") or []),
        "postDescription": job_info.get("postDescription", ""),
        "address": job_info.get("address", ""),
        "encryptJobId": encrypt_job_id,
        "lid": lid,
        "securityId": security_id,
        "detailUrl": detail_url,
    }


def save_rows(rows: list[dict], prefix: str) -> tuple[Path, Path]:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = DATA_DIR / f"{prefix}_{ts}.json"
    csv_path = DATA_DIR / f"{prefix}_{ts}.csv"

    json_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if rows:
        fieldnames = list(rows[0].keys())
        with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    return json_path, csv_path


def _fetch_detail_robust(session, base_headers, security_id, lid, cookie_path, port):
    """抓一条详情；命中风控码时刷新令牌重试一次。

    返回 (api_code, zp_data, error, refreshed)。
    - refreshed 表示本次是否触发过令牌刷新（用于统计与配额窗口重置）
    """
    try:
        return 0, fetch_detail(session, base_headers, security_id, lid), "", False
    except BossApiError as exc:
        if exc.code not in RISK_CODES:
            return exc.code, None, exc.message, False
        print(f"[detail] risk code={exc.code}, refreshing stoken ...")
        try:
            refresh_stoken_runtime(session, cookie_path, port)
            time.sleep(DETAIL_RISK_WAIT)
            return 0, fetch_detail(session, base_headers, security_id, lid), "", True
        except BossApiError as exc2:
            return exc2.code, None, exc2.message, True
        except Exception as exc2:
            return -1, None, str(exc2)[:200], True
    except Exception as exc:
        return -1, None, str(exc)[:200], False


def collect_details(session, base_headers, jobs, cookie_path, port, prefix, args,
                    report=None, note=None):
    """按间隔逐条抓详情，每满 N 条主动刷新令牌，遇风控连续失败则中止。

    engine=browser 时走页面自身的 axios 实例（拦截器会自动轮换 stoken），
    engine=requests 时退回纯接口 + 每 N 条刷新令牌。
    返回详情信封（含每条请求的 code / 时间 / zpData），由调用方入库。
    """
    interval = max(1.0, float(args.detail_interval))
    jitter = min(max(float(args.detail_jitter), 0.0), 0.5)
    refresh_every = max(0, int(args.detail_refresh_every))
    items = []
    ok = failed = refreshes = 0
    aborted = ""
    consecutive_risk = 0
    consecutive_engine_error = 0

    engine = None
    if args.detail_engine in ("auto", "browser") and port:
        try:
            import browser_detail
            engine = browser_detail.BrowserDetail(port)
            engine.ensure_ready()
            print("[detail] engine=browser (page axios)", flush=True)
        except Exception as exc:
            if args.detail_engine == "browser":
                raise
            print(f"[detail] browser engine unavailable ({exc}), fallback to requests",
                  flush=True)
            engine = None

    # 布隆去重：已采集过的职位直接跳过（阳性再查 MySQL 确认，误判不漏采）
    dedupe = None
    confirm = None
    if getattr(args, "skip_collected", True):
        try:
            import db
            import detail_dedupe
            dedupe = detail_dedupe.DetailDedupe.from_env()
            confirm = db.has_detail_by_encrypt_id
        except Exception as exc:
            print(f"[detail] dedupe disabled: {exc}", flush=True)

    pending = list(jobs)
    skipped = []
    if dedupe is not None:
        pending = []
        for job in jobs:
            encrypt_id = job.get("encryptJobId") or job.get("encryptId") or ""
            if dedupe.should_fetch(encrypt_id, confirm):
                pending.append(job)
            else:
                skipped.append(encrypt_id)
        if skipped:
            print(f"[detail] skipped {len(skipped)} already collected", flush=True)

    for index, job in enumerate(pending):
        if index > 0:
            wait = interval * (1 + random.uniform(-jitter, jitter))
            print(f"[detail] {index + 1}/{len(pending)} wait {wait:.0f}s", flush=True)
            time.sleep(wait)
        lid = job.get("lid", "")
        security_id = job.get("securityId", "")
        encrypt_id = job.get("encryptJobId") or job.get("encryptId") or ""
        requested_at = datetime.now()
        if engine is not None:
            code, zp_data, error = engine.fetch_job(job)
            refreshed = False
        else:
            code, zp_data, error, refreshed = _fetch_detail_robust(
                session, base_headers, security_id, lid, cookie_path, port
            )
        if refreshed:
            refreshes += 1

        if code == 0 and zp_data is not None:
            ok += 1
            consecutive_risk = 0
            consecutive_engine_error = 0
            items.append({
                "lid": lid,
                "security_id": security_id,
                "requested_at": requested_at.isoformat(timespec="seconds"),
                "api_code": 0,
                "error": "",
                "zpData": zp_data,
            })
            print(f"[detail] ok lid={lid} desc={len((zp_data.get('jobInfo') or {}).get('postDescription') or '')}",
                  flush=True)
            if dedupe is not None:
                dedupe.mark(encrypt_id)
            # 主动续令牌：仅 requests 引擎需要（browser 引擎由页面拦截器自动轮换）
            if (
                engine is None
                and refresh_every
                and not refreshed
                and ok % refresh_every == 0
                and index < len(pending) - 1
            ):
                try:
                    refresh_stoken_runtime(session, cookie_path, port)
                    refreshes += 1
                    print(f"[detail] stoken refreshed after {ok} items", flush=True)
                    time.sleep(DETAIL_REFRESH_PAUSE)
                except Exception as exc:
                    print(f"[detail] stoken refresh failed: {exc}", flush=True)
        else:
            failed += 1
            items.append({
                "lid": lid,
                "security_id": security_id,
                "requested_at": requested_at.isoformat(timespec="seconds"),
                "api_code": code,
                "error": error,
                "zpData": None,
            })
            print(f"[detail] fail lid={lid} code={code} {error}", flush=True)
            if note is not None:
                note(f"详情失败 {lid} code={code}")
            if code in RISK_CODES:
                consecutive_risk += 1
                if consecutive_risk >= 2:
                    aborted = f"code={code}: {error}"
                    print(f"[detail] aborted: {aborted}", flush=True)
                    break
            elif code == -1 and (
                "上下文" in error or "undefined" in error or "MISSING" in error
            ):
                # 页面上下文反复重建仍失败：快速中止，避免空转几十分钟
                consecutive_engine_error += 1
                if consecutive_engine_error >= 3:
                    aborted = f"engine-lost: {error}"
                    print(f"[detail] aborted: {aborted}", flush=True)
                    break
            else:
                consecutive_engine_error = 0

        if report is not None:
            done = ok + failed
            current_name = lid
            if code == 0 and zp_data is not None:
                current_name = (zp_data.get("jobInfo") or {}).get("jobName") or lid
            report(
                phase="detail",
                done=done,
                total=len(pending),
                ok=ok,
                failed=failed,
                skipped=len(skipped),
                current=current_name,
                eta_seconds=int(max(0, len(pending) - done) * interval),
            )

    envelope = {
        "requested": len(jobs),
        "ok": ok,
        "failed": failed,
        "skipped": len(skipped),
        "aborted": aborted,
        "stoken_refreshes": refreshes,
        "items": items,
    }
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = DATA_DIR / f"{prefix}_details_{ts}.json"
    path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[detail] saved {path} ok={ok} failed={failed} skipped={len(skipped)} "
          f"refreshes={refreshes}", flush=True)
    return envelope, path


def parse_args():
    parser = argparse.ArgumentParser(description="Boss 直聘接口采集探针（自动使用最新抓包登录态）")
    parser.add_argument("--mode", choices=["recommend", "search"], default="recommend")
    parser.add_argument("--keyword", default="", help="search 模式关键词")
    parser.add_argument("--city", default="", help="城市编码，默认取抓包文件")
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--page-size", type=int, default=15)
    parser.add_argument("--detail-limit", type=int, default=-1,
                        help="详情条数：-1=全部（默认），0=不抓，N=前 N 条")
    parser.add_argument("--detail-interval", type=float, default=DETAIL_DEFAULT_INTERVAL,
                        help="详情请求间隔秒数（默认 20）")
    parser.add_argument("--detail-jitter", type=float, default=0.2,
                        help="间隔随机抖动比例（0-0.5，默认 0.2）")
    parser.add_argument("--detail-refresh-every", type=int, default=5,
                        help="每成功 N 条主动刷新令牌（仅 requests 引擎，默认 5）")
    parser.add_argument("--detail-engine", choices=["auto", "browser", "requests"],
                        default="auto",
                        help="详情请求引擎：browser=页面 axios（推荐），requests=纯接口")
    parser.add_argument("--skip-collected", dest="skip_collected",
                        action="store_true", default=True,
                        help="布隆过滤器跳过已采集职位（默认开）")
    parser.add_argument("--no-skip-collected", dest="skip_collected",
                        action="store_false",
                        help="关闭布隆去重，全部重新采集")
    parser.add_argument("--capture", default="", help="指定抓包 JSON 文件")
    parser.add_argument("--cookies", default="", help="指定已保存的 Cookie JSON 文件")
    parser.add_argument("--port", type=int, default=0, help="CDP 端口；不传则从 Cookie 文件读取")
    parser.add_argument("--delay", type=float, default=3.0, help="翻页基础延迟秒数")
    parser.add_argument("--prefix", default="", help="输出文件名前缀")
    parser.add_argument("--task-id", default="", help="任务ID，用于写进度文件")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.pages > 10:
        print(f"[WARN] pages={args.pages} too large, clamp to 10")
        args.pages = 10
    if args.detail_limit < -1:
        print(f"[WARN] detail-limit={args.detail_limit} invalid, treat as all (-1)")
        args.detail_limit = -1

    task_id = args.task_id
    events = []
    started_at = datetime.now().isoformat(timespec="seconds")

    def note(message: str) -> None:
        events.append(message)
        if len(events) > 12:
            del events[:-12]

    def report(**fields) -> None:
        if not task_id:
            return
        try:
            progress_store.write_progress(
                task_id, started_at=started_at, events=list(events), **fields
            )
        except Exception as exc:
            print(f"[progress] write failed: {exc}", flush=True)

    report(phase="list", page=0, pages=args.pages, done=0, total=0,
           ok=0, failed=0, skipped=0)

    base_headers, cookies, captured_city, capture_path = load_latest_capture(args.capture)
    city = args.city or captured_city
    # 登录态判定改看 Cookie（bst / zp_at），不再依赖抓包里的 token 请求头
    state = "logged" if ("bst" in cookies or "zp_at" in cookies) else "unlogged"
    default_cookie_path = DATA_DIR / f"cookies_{state}.json"

    # 多账号关键：--cookies 指定时，刷新/重试/保存全程用同一个文件，避免串号。
    effective_cookie_path = default_cookie_path
    if args.cookies:
        effective_cookie_path = Path(args.cookies)
        cookies = load_cookie_file(effective_cookie_path)
        print(f"[cookies] loaded {effective_cookie_path}")
    elif (
        default_cookie_path.exists()
        and default_cookie_path.stat().st_mtime > capture_path.stat().st_mtime
    ):
        cookies = load_cookie_file(default_cookie_path)
        print(f"[cookies] loaded newer {default_cookie_path}")

    # CDP 端口：显式参数 > Cookie 文件内记录的 cdp_port > 0（用 stoken_browser 默认）
    port = args.port
    if not port and effective_cookie_path.exists():
        try:
            port = int(
                json.loads(effective_cookie_path.read_text(encoding="utf-8")).get("cdp_port") or 0
            )
        except (ValueError, OSError):
            port = 0

    session = make_session(cookies)

    print(f"[capture] {capture_path}")
    print(f"[state] {state}  [mode] {args.mode}  [city] {city}  [pages] {args.pages}")
    print(f"[cookies] effective={effective_cookie_path}  [cdp_port] {port}")

    all_rows = []
    all_jobs = []
    for page in range(1, args.pages + 1):
        if args.mode == "recommend":
            zp_data = with_token_refresh(
                session,
                base_headers,
                fetch_recommend,
                city,
                page,
                args.page_size,
                cookie_path=effective_cookie_path,
                port=port,
            )
        else:
            if not args.keyword:
                raise SystemExit("search 模式需要 --keyword")
            zp_data = with_token_refresh(
                session,
                base_headers,
                fetch_search,
                args.keyword,
                city,
                page,
                args.page_size,
                cookie_path=effective_cookie_path,
                port=port,
            )

        jobs = zp_data.get("jobList", []) or []
        print(f"[page {page}] got {len(jobs)} jobs, hasMore={zp_data.get('hasMore')}")

        for job in jobs:
            all_jobs.append(job)
            all_rows.append(flatten_job(job))

        note(f"列表第 {page} 页：{len(jobs)} 条")
        report(phase="list", page=page, pages=args.pages, jobs=len(all_jobs))

        if page < args.pages:
            time.sleep(args.delay + random.uniform(0, args.delay))

    degraded = [
        row for row in all_rows if row.get("jobExperience") in DEGRADED_EXPERIENCE
    ]
    if degraded:
        print(
            f"[WARN] {len(degraded)}/{len(all_rows)} 条经验字段命中服务端降级值 "
            f"{sorted(DEGRADED_EXPERIENCE)}，建议用 --detail-limit 抓详情获取真实经验"
        )

    prefix = args.prefix or f"boss_{args.mode}_{state}"
    detail_envelope = None
    if args.detail_limit and all_jobs:
        targets = all_jobs if args.detail_limit < 0 else all_jobs[: args.detail_limit]
        print(f"\n[detail] start: {len(targets)} items, interval={args.detail_interval}s")
        report(phase="detail", page=args.pages, pages=args.pages, done=0,
               total=len(targets), ok=0, failed=0, skipped=0, current="",
               eta_seconds=int(len(targets) * float(args.detail_interval)))
        detail_envelope, _ = collect_details(
            session,
            base_headers,
            targets,
            effective_cookie_path,
            port,
            prefix,
            args,
            report=report,
            note=note,
        )
        if detail_envelope is not None:
            ok_n = detail_envelope["ok"]
            fail_n = detail_envelope["failed"]
            skip_n = detail_envelope.get("skipped", 0)
            if detail_envelope.get("aborted"):
                report(phase="aborted", message=detail_envelope["aborted"],
                       done=ok_n + fail_n, total=ok_n + fail_n + skip_n,
                       ok=ok_n, failed=fail_n, skipped=skip_n, eta_seconds=0)
            else:
                report(phase="done", done=ok_n + fail_n,
                       total=ok_n + fail_n + skip_n, ok=ok_n, failed=fail_n,
                       skipped=skip_n, eta_seconds=0,
                       finished_at=datetime.now().isoformat(timespec="seconds"))
    else:
        report(phase="done", message="仅采集列表", done=0, total=0, ok=0,
               failed=0, skipped=0, eta_seconds=0,
               finished_at=datetime.now().isoformat(timespec="seconds"))

    save_cookie_file(session, effective_cookie_path)
    json_path, csv_path = save_rows(all_rows, prefix)
    print(f"\n[OK] rows={len(all_rows)}")
    print(f"[OK] cookies: {default_cookie_path}")
    print(f"[OK] json: {json_path}")
    print(f"[OK] csv:  {csv_path}")

    if detail_envelope is not None:
        print(
            f"[OK] details: ok={detail_envelope['ok']} failed={detail_envelope['failed']} "
            f"refreshes={detail_envelope['stoken_refreshes']} "
            f"aborted={detail_envelope['aborted'] or '-'}"
        )

    if all_rows:
        first = all_rows[0]
        print("\n[sample]")
        for key in (
            "jobName",
            "salaryDesc",
            "jobExperience",
            "jobDegree",
            "cityName",
            "brandName",
            "bossName",
        ):
            print(f"  {key}: {first.get(key)}")


if __name__ == "__main__":
    main()
