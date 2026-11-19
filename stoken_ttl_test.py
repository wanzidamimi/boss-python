"""冻结 stoken 实验：冻结一份真实浏览器 Cookie，用校验 stoken 的 search 接口探到失效为止。

与 stability_test 的关键区别：
- stability_test 每次失败都自动刷新 stoken（测的是"有自愈时能否长期运行"）
- 本实验**绝不刷新**，测的是 stoken 从生成到自然失效的真实时长

注意：本实验会持续 1~3 天，期间不要运行 stoken_browser.py / job_api.py / Web 任务，
否则会刷新 stoken 导致实验作废。
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
from job_api import BASE, SEARCH_URL, apply_auth_headers, load_latest_capture

SNAPSHOT = DATA_DIR / "stoken_frozen_snapshot.json"
LOG = DATA_DIR / "stoken_ttl_log.csv"
SUMMARY = DATA_DIR / "stoken_ttl_summary.json"
CITY = "101120200"


async def browser_cookies():
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
        await ws.send(json.dumps({
            "id": 1,
            "method": "Network.getCookies",
            "params": {"urls": ["https://www.zhipin.com/"]},
        }))
        while True:
            raw = json.loads(await ws.recv())
            if raw.get("id") == 1:
                return raw.get("result", {}).get("cookies", [])


def make_snapshot():
    """冻结浏览器当前 Cookie；尤其锁死 __zp_stoken__ / bst，全程绝不更新。"""
    details = asyncio.run(browser_cookies())
    cookies = {c["name"]: c["value"] for c in details}
    now = datetime.now()
    now_ts = now.timestamp()
    expiries = {}
    for c in details:
        if c["name"] in ("__zp_stoken__", "bst", "zp_at", "wt2"):
            expires = c.get("expires")
            expiries[c["name"]] = {
                "expires_at": (
                    datetime.fromtimestamp(expires).isoformat(timespec="seconds")
                    if expires and expires > 0 else None
                ),
                "nominal_minutes": (
                    round((expires - now_ts) / 60, 1)
                    if expires and expires > 0 else None
                ),
            }
    snapshot = {
        "created_at": now.isoformat(timespec="seconds"),
        "cookies": cookies,
        "nominal_expiries": expiries,
        "stoken_len": len(cookies.get("__zp_stoken__", "")),
        "note": "冻结快照，实验期间禁止刷新；失效后加 --fresh 重新开始",
    }
    SNAPSHOT.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return snapshot


def probe(snapshot, base_headers):
    """用冻结 Cookie 打 search 接口；该接口真正校验 stoken。"""
    session = requests.Session()
    for name, value in snapshot["cookies"].items():
        session.cookies.set(name, value, domain=".zhipin.com", path="/")
    headers = apply_auth_headers(base_headers, session)
    headers["traceId"] = "F-" + __import__("secrets").token_hex(12)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    form = {
        "page": 1, "pageSize": 15, "city": CITY,
        "query": "爬虫", "scene": "1",
    }
    response = session.post(
        SEARCH_URL,
        params={"_": int(time.time() * 1000)},
        data=form,
        headers=headers,
        timeout=20,
    )
    payload = response.json()
    code = int(payload.get("code", -1))
    message = str(payload.get("message", ""))
    jobs = (payload.get("zpData") or {}).get("jobList") or []
    return code, message, len(jobs)


def write_summary(snapshot, total, failures, first_failure, observed_min, finished=False):
    now = datetime.now().isoformat(timespec="seconds")
    summary = {
        "updated_at": now,
        "running": not finished,
        "snapshot_created_at": snapshot["created_at"],
        "stoken_len": snapshot["stoken_len"],
        "total_probes": total,
        "failures": failures,
        "first_failure_at": first_failure,
        "observed_ttl_min": observed_min,
        "nominal_expiries": snapshot["nominal_expiries"],
    }
    if finished:
        summary["finished_at"] = now
    SUMMARY.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description="冻结 stoken 自然过期实验（绝不刷新）")
    parser.add_argument("--days", type=float, default=3, help="最长运行天数，默认 3")
    parser.add_argument("--interval-min", type=float, default=30, help="探测间隔分钟，默认 30")
    parser.add_argument("--fresh", action="store_true", help="强制重新冻结 Cookie 快照")
    args = parser.parse_args()

    base_headers, _, _, _ = load_latest_capture()

    if args.fresh or not SNAPSHOT.exists():
        snapshot = make_snapshot()
        print(f"[snapshot] created at {snapshot['created_at']}")
        for k, v in snapshot["nominal_expiries"].items():
            print(f"  {k:18s} nominal={v['nominal_minutes']} min")
    else:
        snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        print(f"[snapshot] reuse frozen snapshot from {snapshot['created_at']}")
        print("[snapshot] 禁止刷新 stoken；如需重新开始请加 --fresh")

    start = datetime.fromisoformat(snapshot["created_at"])
    deadline = start.timestamp() + args.days * 86400
    write_header = not LOG.exists()
    log_file = LOG.open("a", encoding="utf-8-sig", newline="")
    writer = csv.writer(log_file)
    if write_header:
        writer.writerow([
            "probe_at", "elapsed_min", "api_code", "api_message",
            "jobs", "stoken_alive", "note",
        ])

    total = 0
    failures = 0
    first_failure = None
    observed_min = None
    print(f"[monitor] {args.days} days, every {args.interval_min}min, NO refresh")

    while time.time() < deadline:
        total += 1
        probe_at = datetime.now()
        elapsed = round((probe_at - start).total_seconds() / 60, 1)
        try:
            code, message, jobs = probe(snapshot, base_headers)
        except Exception as exc:
            code, message, jobs = -1, str(exc)[:200], 0

        alive = code == 0
        note = ""
        if not alive:
            failures += 1
            if first_failure is None:
                first_failure = probe_at.isoformat(timespec="seconds")
                observed_min = elapsed
                note = "STOKEN_EXPIRED"
                print(f"[EXPIRED] code={code} msg={message} after {elapsed}min")

        writer.writerow([
            probe_at.isoformat(timespec="seconds"), elapsed, code,
            message[:200], jobs, int(alive), note,
        ])
        log_file.flush()
        write_summary(snapshot, total, failures, first_failure, observed_min)

        print(
            f"[probe] #{total} +{elapsed:>7.1f}min code={code:<4} "
            f"alive={alive} jobs={jobs} msg={message[:36]}"
        )

        # 测到失效即停止：已经拿到自然过期时间，且后续探测无意义
        if first_failure:
            break

        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(args.interval_min * 60, remaining))

    log_file.close()
    summary = write_summary(
        snapshot, total, failures, first_failure, observed_min,
        finished=bool(first_failure),
    )
    if not first_failure:
        print(f"[done] 未观察到失效，已连续探测 {total} 次 / {args.days} 天")
    print("[summary]", json.dumps(summary, ensure_ascii=False, indent=2)[:700])


if __name__ == "__main__":
    main()

