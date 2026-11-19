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

# 服务端在登录态下会把列表接口的 jobExperience 统一降级成同一个值，
# 真实经验只在职位详情接口返回；采集详情时以详情为准。
DEGRADED_EXPERIENCE = {"10年以上"}

# 可自愈的风控/令牌错误码
TOKEN_CODES = {120, 121, 122}
RISK_CODES = {35, 36, 37}
BACKOFF_SECONDS = 20
STOKEN_TIMEOUT = 180

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
    session: requests.Session, cookie_path: Path | None = None
) -> str:
    """重新触发浏览器产出 stoken（刷新目标页 + 读取浏览器 Cookie），并同步回 session。"""
    result = subprocess.run(
        [sys.executable, "stoken_browser.py"],
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
                refresh_stoken_runtime(session, cookie_path)
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


def parse_args():
    parser = argparse.ArgumentParser(description="Boss 直聘接口采集探针（自动使用最新抓包登录态）")
    parser.add_argument("--mode", choices=["recommend", "search"], default="recommend")
    parser.add_argument("--keyword", default="", help="search 模式关键词")
    parser.add_argument("--city", default="", help="城市编码，默认取抓包文件")
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--page-size", type=int, default=15)
    parser.add_argument("--detail-limit", type=int, default=0, help="额外抓取前 N 条详情")
    parser.add_argument("--capture", default="", help="指定抓包 JSON 文件")
    parser.add_argument("--cookies", default="", help="指定已保存的 Cookie JSON 文件")
    parser.add_argument("--delay", type=float, default=3.0, help="翻页基础延迟秒数")
    parser.add_argument("--prefix", default="", help="输出文件名前缀")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.pages > 10:
        print(f"[WARN] pages={args.pages} too large, clamp to 10")
        args.pages = 10
    if args.detail_limit > 3:
        print(f"[WARN] detail-limit={args.detail_limit} too large, clamp to 3")
        args.detail_limit = 3

    base_headers, cookies, captured_city, capture_path = load_latest_capture(args.capture)
    city = args.city or captured_city
    # 登录态判定改看 Cookie（bst / zp_at），不再依赖抓包里的 token 请求头
    state = "logged" if ("bst" in cookies or "zp_at" in cookies) else "unlogged"
    default_cookie_path = DATA_DIR / f"cookies_{state}.json"

    if args.cookies:
        cookie_path = Path(args.cookies)
        cookies = load_cookie_file(cookie_path)
        print(f"[cookies] loaded {cookie_path}")
    elif (
        default_cookie_path.exists()
        and default_cookie_path.stat().st_mtime > capture_path.stat().st_mtime
    ):
        cookies = load_cookie_file(default_cookie_path)
        print(f"[cookies] loaded newer {default_cookie_path}")

    session = make_session(cookies)

    print(f"[capture] {capture_path}")
    print(f"[state] {state}  [mode] {args.mode}  [city] {city}  [pages] {args.pages}")

    all_rows = []
    for page in range(1, args.pages + 1):
        if args.mode == "recommend":
            zp_data = with_token_refresh(
                session,
                base_headers,
                fetch_recommend,
                city,
                page,
                args.page_size,
                cookie_path=default_cookie_path,
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
                cookie_path=default_cookie_path,
            )

        jobs = zp_data.get("jobList", []) or []
        print(f"[page {page}] got {len(jobs)} jobs, hasMore={zp_data.get('hasMore')}")

        for index, job in enumerate(jobs):
            detail = None
            if index < args.detail_limit:
                detail = with_token_refresh(
                    session,
                    base_headers,
                    fetch_detail,
                    job.get("securityId", ""),
                    job.get("lid", ""),
                    cookie_path=default_cookie_path,
                )
                time.sleep(random.uniform(3.0, 6.0))
            all_rows.append(flatten_job(job, detail))

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

    save_cookie_file(session, default_cookie_path)
    prefix = args.prefix or f"boss_{args.mode}_{state}"
    json_path, csv_path = save_rows(all_rows, prefix)
    print(f"\n[OK] rows={len(all_rows)}")
    print(f"[OK] cookies: {default_cookie_path}")
    print(f"[OK] json: {json_path}")
    print(f"[OK] csv:  {csv_path}")

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
