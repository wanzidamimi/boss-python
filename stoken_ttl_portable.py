#!/usr/bin/env python3
"""冻结 stoken 自然过期实验（自包含便携版）。

只依赖 requests，不依赖本项目的任何其它文件。
把本文件 + stoken_frozen_snapshot.json 拷到任意设备即可运行。

用法：
  pip install requests
  python stoken_ttl_portable.py --days 3 --interval-min 30

注意：
  1. 必须在与冻结快照相同的出口 IP 下运行（比如家里同一 WiFi），
     IP 变了会立刻返回 code=37，测出来的就不是 token 自然过期。
  2. 运行期间不要刷新 stoken、不要跑采集任务。
"""

import argparse
import csv
import json
import secrets
import time
from datetime import datetime
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
SNAPSHOT = BASE / "stoken_frozen_snapshot.json"
LOG = BASE / "stoken_ttl_log.csv"
SUMMARY = BASE / "stoken_ttl_summary.json"

SEARCH_URL = "https://www.zhipin.com/wapi/zpgeek/search/joblist.json"
CITY = "101120200"
KEYWORD = "爬虫"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)


def load_snapshot():
    if not SNAPSHOT.exists():
        raise SystemExit(f"找不到冻结快照：{SNAPSHOT}")
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    if not data.get("cookies", {}).get("__zp_stoken__"):
        raise SystemExit("快照里没有 __zp_stoken__，请在原设备重新冻结")
    return data


def probe(snapshot):
    session = requests.Session()
    for name, value in snapshot["cookies"].items():
        session.cookies.set(name, value, domain=".zhipin.com", path="/")

    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.zhipin.com/web/geek/jobs",
        "Content-Type": "application/x-www-form-urlencoded",
        "traceId": "F-" + secrets.token_hex(12),
    }
    # zp_token 必须与 Cookie 里的 bst 一致（站点真实对应关系）
    bst = session.cookies.get("bst")
    if bst:
        headers["zp_token"] = bst

    form = {
        "page": 1,
        "pageSize": 15,
        "city": CITY,
        "query": KEYWORD,
        "scene": "1",
    }
    resp = session.post(
        SEARCH_URL,
        params={"_": int(time.time() * 1000)},
        data=form,
        headers=headers,
        timeout=20,
    )
    payload = resp.json()
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
        "stoken_len": snapshot.get("stoken_len"),
        "total_probes": total,
        "failures": failures,
        "first_failure_at": first_failure,
        "observed_ttl_min": observed_min,
        "nominal_expiries": snapshot.get("nominal_expiries", {}),
    }
    if finished:
        summary["finished_at"] = now
    SUMMARY.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description="冻结 stoken 自然过期实验（便携版）")
    parser.add_argument("--days", type=float, default=3, help="最长运行天数，默认 3")
    parser.add_argument("--interval-min", type=float, default=30, help="探测间隔分钟，默认 30")
    args = parser.parse_args()

    snapshot = load_snapshot()
    start = datetime.fromisoformat(snapshot["created_at"])
    deadline = start.timestamp() + args.days * 86400

    print(f"[snapshot] frozen at {snapshot['created_at']}")
    print(f"[config] {args.days} days, every {args.interval_min}min, NO refresh")
    print(f"[files] log={LOG.name} summary={SUMMARY.name}")

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

    while time.time() < deadline:
        total += 1
        probe_at = datetime.now()
        elapsed = round((probe_at - start).total_seconds() / 60, 1)
        try:
            code, message, jobs = probe(snapshot)
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
            f"alive={alive} jobs={jobs} msg={message[:36]}",
            flush=True,
        )

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
        print(f"[done] 未观察到失效，连续探测 {total} 次")
    print("[summary]", json.dumps(summary, ensure_ascii=False)[:500], flush=True)


if __name__ == "__main__":
    main()
