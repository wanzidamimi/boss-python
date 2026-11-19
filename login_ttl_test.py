"""登录态有效期实验：冻结一份登录 Cookie，定期探 getUserInfo 直到失效。

与 stability_test.py 的区别：
- stability_test 探的是 stoken（search 接口），且失败会自动刷新
- 本实验探的是登录态（getUserInfo 接口），全程使用冻结快照，绝不刷新
"""

import argparse
import asyncio
import csv
import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import requests
import websockets

from config import CDP_DEBUG_PORT, DATA_DIR
from job_api import BASE, load_cookie_file, new_trace_id, now_ms

SNAPSHOT = DATA_DIR / "login_snapshot.json"
LOG = DATA_DIR / "login_ttl_log.csv"
SUMMARY = DATA_DIR / "login_ttl_summary.json"
USER_INFO_URL = f"{BASE}/wapi/zpuser/wap/getUserInfo.json"
LOGIN_FAIL_CODES = {7, 17, 38}

KEEP_COOKIES = {
    "zp_at", "bst", "wt2", "wt2_r", "__g", "isOHPC", "ab_guid",
    "__zp_stoken__", "__zp_sseed__", "__zp_sname__", "__zp_sts__", "__a", "__c",
}


async def browser_cookie_details():
    """从 CDP 读取浏览器当前 Cookie 及其过期时间。"""
    targets = json.load(
        urllib.request.urlopen(
            f"http://127.0.0.1:{CDP_DEBUG_PORT}/json/list", timeout=5
        )
    )
    page = next(
        t for t in targets
        if t.get("type") == "page" and "zhipin.com" in t.get("url", "")
    )
    async with websockets.connect(
        page["webSocketDebuggerUrl"], max_size=20_000_000, ping_interval=None
    ) as ws:
        msg_id = 1
        await ws.send(json.dumps({
            "id": msg_id,
            "method": "Network.getCookies",
            "params": {"urls": ["https://www.zhipin.com/"]},
        }))
        while True:
            raw = json.loads(await ws.recv())
            if raw.get("id") == msg_id:
                return raw.get("result", {}).get("cookies", [])


def make_snapshot():
    """冻结当前登录 Cookie；实验全程只用这份，不再从浏览器刷新。"""
    details = asyncio.run(browser_cookie_details())
    cookies = {
        c["name"]: c["value"]
        for c in details
        if c["name"] in KEEP_COOKIES
    }
    now = datetime.now()
    now_ts = now.timestamp()
    expiries = {}
    for c in details:
        if c["name"] not in KEEP_COOKIES:
            continue
        expires = c.get("expires")
        if expires and expires > 0:
            expiries[c["name"]] = {
                "expires_at": datetime.fromtimestamp(expires).isoformat(timespec="seconds"),
                "nominal_minutes": round((expires - now_ts) / 60, 1),
            }
        else:
            expiries[c["name"]] = {
                "expires_at": None,
                "nominal_minutes": None,
                "note": "session cookie（浏览器关闭即失效）",
            }
    snapshot = {
        "created_at": now.isoformat(timespec="seconds"),
        "cookies": cookies,
        "nominal_expiries": expiries,
        "note": "冻结快照，实验期间不要重新登录或刷新；失效后需删掉本文件重新开始",
    }
    SNAPSHOT.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return snapshot


def probe(cookies):
    """用冻结 Cookie 探登录态；注意这里不校验也不刷新 stoken。"""
    session = requests.Session()
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=".zhipin.com", path="/")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.zhipin.com/web/geek/jobs",
        "traceId": new_trace_id(),
    }
    response = session.get(
        USER_INFO_URL, params={"_": now_ms()}, headers=headers, timeout=20
    )
    payload = response.json()
    code = int(payload.get("code", -1))
    data = payload.get("zpData") or {}
    logged_in = code == 0 and bool(data.get("userId"))
    return code, str(payload.get("message", "")), logged_in, bool(data.get("userId"))


def write_summary(snapshot, total, failures, first_failure, observed_hours, finished=False):
    now = datetime.now().isoformat(timespec="seconds")
    summary = {
        "updated_at": now,
        "running": not finished,
        "snapshot_created_at": snapshot["created_at"],
        "total_probes": total,
        "failures": failures,
        "first_failure_at": first_failure,
        "observed_ttl_hours": observed_hours,
        "nominal_expiries": snapshot["nominal_expiries"],
    }
    if finished:
        summary["finished_at"] = now
    SUMMARY.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description="登录态有效期实验（冻结 Cookie，不刷新）")
    parser.add_argument("--days", type=float, default=14, help="总运行天数，默认 14 天")
    parser.add_argument("--interval-min", type=float, default=120, help="探测间隔分钟，默认 120")
    parser.add_argument("--fresh", action="store_true", help="强制重新冻结 Cookie 快照")
    args = parser.parse_args()

    if args.fresh or not SNAPSHOT.exists():
        snapshot = make_snapshot()
        print(f"[snapshot] created at {snapshot['created_at']}")
        for name, info in snapshot["nominal_expiries"].items():
            if info["nominal_minutes"] is not None:
                print(f"  {name:18s} nominal {info['nominal_minutes']:>9} min")
    else:
        snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        print(f"[snapshot] reuse frozen snapshot from {snapshot['created_at']}")
        print("[snapshot] 如需重新登录后再测，请加 --fresh")

    start = datetime.fromisoformat(snapshot["created_at"])
    deadline = start.timestamp() + args.days * 86400
    write_header = not LOG.exists()
    log_file = LOG.open("a", encoding="utf-8-sig", newline="")
    writer = csv.writer(log_file)
    if write_header:
        writer.writerow([
            "probe_at", "elapsed_hours", "api_code", "api_message",
            "logged_in", "has_user_id", "note",
        ])

    total = 0
    failures = 0
    first_failure = None
    observed_hours = None
    print(f"[monitor] {args.days} days, every {args.interval_min} min")

    while time.time() < deadline:
        total += 1
        probe_at = datetime.now()
        elapsed = round((probe_at - start).total_seconds() / 3600, 2)
        note = ""
        try:
            code, message, logged_in, has_uid = probe(snapshot["cookies"])
        except Exception as exc:
            code, message, logged_in, has_uid = -1, str(exc)[:200], False, False
            note = "network error"

        # 判定登录失效：明确要求重新登录的错误码，或成功码但拿不到 userId
        expired = code in LOGIN_FAIL_CODES or (code == 0 and not has_uid)
        if not expired:
            expired = code not in (0,)

        if expired:
            failures += 1
            if first_failure is None:
                first_failure = probe_at.isoformat(timespec="seconds")
                observed_hours = elapsed
                note = (note + " LOGIN_EXPIRED").strip()
                print(f"[EXPIRED] code={code} msg={message} after {elapsed}h")

        writer.writerow([
            probe_at.isoformat(timespec="seconds"), elapsed, code,
            message[:200], int(logged_in), int(has_uid), note,
        ])
        log_file.flush()
        write_summary(snapshot, total, failures, first_failure, observed_hours)

        print(
            f"[probe] #{total} +{elapsed:>6.2f}h code={code:<4} "
            f"logged_in={logged_in} msg={message[:40]}"
        )

        remaining = deadline - time.time()
        if remaining <= 0 or first_failure:
            break
        time.sleep(min(args.interval_min * 60, remaining))

    log_file.close()
    summary = write_summary(
        snapshot, total, failures, first_failure, observed_hours, finished=bool(first_failure)
    )
    if not first_failure:
        print(f"[done] 未观察到失效，已连续探测 {total} 次 / {args.days} 天")
    print("[summary]", json.dumps(summary, ensure_ascii=False, indent=2)[:800])


if __name__ == "__main__":
    main()
