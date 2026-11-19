import argparse
import csv
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

import db
from job_api import (
    SEARCH_URL,
    apply_auth_headers,
    load_cookie_file,
    load_latest_capture,
    make_session,
    new_trace_id,
    now_ms,
)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
USER_INFO_URL = "https://www.zhipin.com/wapi/zpuser/wap/getUserInfo.json"
COOKIE_PATH = DATA_DIR / "cookies_logged.json"


def read_token_state():
    if not COOKIE_PATH.exists():
        return "", 0.0
    data = json.loads(COOKIE_PATH.read_text(encoding="utf-8"))
    cookies = {item["name"]: item["value"] for item in data.get("cookies", [])}
    token = cookies.get("__zp_stoken__", "")
    generated_at = data.get("stoken_generated_at")
    age_min = 0.0
    if generated_at:
        try:
            age_min = (
                datetime.now() - datetime.fromisoformat(generated_at)
            ).total_seconds() / 60
        except ValueError:
            age_min = 0.0
    return token, age_min


def probe(session, base_headers):
    """用真正校验 stoken 的 search 接口做探针。

    之前用 getUserInfo.json 是无效的：实测把 __zp_stoken__ 改成 BROKEN_TOKEN_123
    仍返回 code=0，说明该接口只校验登录态、不校验 stoken，永远探不到令牌失效。
    """
    headers = apply_auth_headers(base_headers, session)
    headers["traceId"] = new_trace_id()
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    form = {
        "page": 1,
        "pageSize": 15,
        "city": "101120200",
        "query": "爬虫",
        "scene": "1",
    }
    response = session.post(
        SEARCH_URL,
        params={"_": now_ms()},
        data=form,
        headers=headers,
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    return int(payload.get("code", -1)), str(payload.get("message", ""))


def refresh_stoken():
    result = subprocess.run(
        [sys.executable, "stoken_browser.py"],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout)[-2000:])
    return result.stdout.strip()


def write_summary(total, failures, first_failure, ttl_observed, finished=False, token_events=None):
    """每次探针后都刷新汇总，避免 24h 结束前看到的都是上次运行的旧数据。"""
    now = datetime.now().isoformat(timespec="seconds")
    summary = {
        "updated_at": now,
        "running": not finished,
        "total_probes": total,
        "failures": failures,
        "first_failure_at": first_failure,
        "observed_ttl_min": ttl_observed,
        "token_events": token_events or [],
        "cookie_nominal_ttl_min": 3693,
        "login_session_nominal_ttl_min": 16490,
    }
    if finished:
        summary["finished_at"] = now
    (DATA_DIR / "stability_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description="Boss 采集链路稳定性/Token 过期观测")
    parser.add_argument("--hours", type=float, default=24, help="总运行时长(小时)")
    parser.add_argument("--interval-min", type=float, default=30, help="探测间隔(分钟)")
    parser.add_argument("--auto-refresh", action="store_true", help="失败时自动刷新 stoken")
    parser.add_argument("--csv", default="data/stability_log.csv")
    args = parser.parse_args()

    base_headers, cookies, _, capture_path = load_latest_capture()
    # 优先用浏览器产出的最新 Cookie，抓包快照往往已经滚动过期
    if COOKIE_PATH.exists() and COOKIE_PATH.stat().st_mtime > capture_path.stat().st_mtime:
        cookies = load_cookie_file(COOKIE_PATH)
        print(f"[monitor] loaded newer cookies: {COOKIE_PATH}")
    session = make_session(cookies)

    csv_path = BASE_DIR / args.csv
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not csv_path.exists()
    log_file = csv_path.open("a", encoding="utf-8-sig", newline="")
    writer = csv.writer(log_file)
    if write_header:
        writer.writerow(
            [
                "probe_at",
                "api_code",
                "api_message",
                "token_age_min",
                "token_len",
                "refreshed",
                "ok",
                "note",
            ]
        )

    deadline = time.time() + args.hours * 3600
    total = 0
    failures = 0
    first_failure = None
    ttl_observed = None
    print(f"[monitor] start, {args.hours}h, interval {args.interval_min}min")
    print(f"[monitor] capture={capture_path}")

    token_events = []
    while time.time() < deadline:
        total += 1
        token, age_min = read_token_state()
        refreshed = False
        note = ""
        try:
            code, message = probe(session, base_headers)
        except Exception as exc:
            code, message = -1, str(exc)[:200]

        # 关键：刷新前先把失败事件记下来，否则重探测成功后 code 被覆盖为 0，
        # 汇总会显示 failures=0，真实的 token 失效点就被掩盖了。
        if code != 0:
            event = {
                "probe_at": datetime.now().isoformat(timespec="seconds"),
                "code": code,
                "message": message[:200],
                "token_age_min": round(age_min, 1),
                "recovered": False,
            }
            token_events.append(event)
            if first_failure is None:
                first_failure = event["probe_at"]
                ttl_observed = event["token_age_min"]

        if code != 0 and args.auto_refresh:
            try:
                refresh_stoken()
                refreshed = True
                session = make_session(
                    {
                        item["name"]: item["value"]
                        for item in json.loads(
                            COOKIE_PATH.read_text(encoding="utf-8")
                        )["cookies"]
                    }
                )
                code, message = probe(session, base_headers)
                token, age_min = read_token_state()
                note = "auto-refreshed"
                token_events[-1]["recovered"] = code == 0
            except Exception as exc:
                note = f"refresh failed: {exc}"[:200]

        ok = code == 0
        if not ok:
            failures += 1

        probe_at = datetime.now().isoformat(timespec="seconds")
        writer.writerow(
            [
                probe_at,
                code,
                message[:200],
                round(age_min, 1),
                len(token),
                int(refreshed),
                int(ok),
                note,
            ]
        )
        log_file.flush()
        write_summary(total, failures, first_failure, ttl_observed, token_events=token_events)

        try:
            db.insert_health_check(
                datetime.now(),
                code,
                message[:200],
                round(age_min, 1),
                len(token),
                refreshed,
                ok,
                note,
            )
        except Exception as exc:
            print(f"[monitor] db write failed: {exc}")

        print(
            f"[monitor] #{total} {probe_at} code={code} "
            f"token_age={round(age_min,1)}min ok={ok}"
        )

        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(args.interval_min * 60, remaining))

    log_file.close()
    summary = write_summary(
        total, failures, first_failure, ttl_observed,
        finished=True, token_events=token_events,
    )
    print("\n[summary]", json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
