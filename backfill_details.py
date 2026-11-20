#!/usr/bin/env python3
"""回填职位详情（走页面 axios 引擎，自动轮换 stoken）。

用法：
  python backfill_details.py --dry-run                 # 只看还剩多少条
  python backfill_details.py --limit 20 --interval 3   # 抓 20 条
  python backfill_details.py --task-id <id> --force    # 指定任务并强制重采

原理：详情请求交给页面自身的 axios 实例，响应拦截器会用服务端下发的
__zp_sseed__/__zp_sname__/__zp_sts__ 重新生成 __zp_stoken__，因此不再受
"每令牌约 5 条"的限制，间隔可以很短（默认 3 秒）。
"""

import argparse
import random
import time
from datetime import datetime, timedelta

import account_manager as am
import db
import job_api as api
from browser_detail import BrowserDetail
from detail_dedupe import DetailDedupe
from detail_hash import flatten_detail

RISK_CODES = api.RISK_CODES


def log_request(account_id, task_id, lid, code, error=""):
    try:
        db.log_detail_requests([{
            "account_id": account_id,
            "task_id": task_id,
            "lid": lid,
            "requested_at": datetime.now(),
            "api_code": code,
            "ok": code == 0,
            "note": error[:255],
        }])
    except Exception as exc:
        print(f"[backfill] log failed: {exc}", flush=True)


def parse_args():
    parser = argparse.ArgumentParser(description="回填职位详情")
    parser.add_argument("--limit", type=int, default=20, help="本次最多处理多少条")
    parser.add_argument("--interval", type=float, default=20.0, help="详情间隔秒数")
    parser.add_argument("--account-id", default="", help="指定账号；默认轮询有效账号")
    parser.add_argument("--task-id", default="", help="只回填该任务的职位")
    parser.add_argument("--force", action="store_true", help="已有详情也重新采集")
    parser.add_argument("--dry-run", action="store_true", help="只统计待回填数量")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.dry_run:
        jobs = db.select_jobs_for_detail(args.limit, args.task_id, args.force)
        print(f"[dry-run] 可回填 {len(jobs)} 条（剩余未采集 {db.count_jobs_without_detail()} 条）")
        for job in jobs[:20]:
            print(f"  {job['id']:>4} {job['lid']:<28} {(job['job_name'] or '')[:30]}")
        return

    account = (db.get_account(args.account_id) if args.account_id
               else db.pick_round_robin_account())
    if not account:
        raise SystemExit("没有可用账号，请先在 Web 端登录账号")

    account_id = account["account_id"]
    cookie_path = am.cookie_file(account_id)
    if not cookie_path.exists():
        raise SystemExit(f"账号凭证文件不存在: {cookie_path}")
    port = int(account["cdp_port"])

    if db.is_account_detail_blocked(account_id):
        until = db.get_account_detail_block(account_id)
        raise SystemExit(f"账号详情处于冷却期（至 {until}），请稍后再试")

    jobs = db.select_jobs_for_detail(args.limit, args.task_id, args.force)
    if not jobs:
        print("[backfill] 没有需要回填的职位")
        return

    launched = False
    if am._port_free(port):
        print(f"[backfill] launching browser on {port} ...", flush=True)
        am.launch_browser(port, am.profile_dir(account_id))
        launched = True
        time.sleep(am.PAGE_SETTLE_SECONDS + 2)

    print(f"[backfill] account={account_id} port={port} jobs={len(jobs)} "
          f"interval={args.interval}s", flush=True)

    try:
        engine = BrowserDetail(port)
        engine.ensure_ready()
        print("[backfill] detail engine ready", flush=True)

        dedupe = DetailDedupe.from_env()
        pending = []
        skipped = 0
        for job in jobs:
            encrypt_id = job.get("encrypt_job_id", "")
            if dedupe.should_fetch(encrypt_id, db.has_detail_by_encrypt_id):
                pending.append(job)
            else:
                skipped += 1
        if skipped:
            print(f"[backfill] skipped {skipped} already collected", flush=True)
        jobs = pending
        if not jobs:
            print("[backfill] 全部已采集，无需抓取")
            return

        ok = fail = 0
        consecutive_risk = 0
        aborted = ""

        for index, job in enumerate(jobs):
            if index > 0:
                wait = args.interval * (1 + random.uniform(-0.2, 0.2))
                print(f"[backfill] {index + 1}/{len(jobs)} wait {wait:.0f}s", flush=True)
                time.sleep(wait)

            code, zp, error = engine.fetch_job(job)
            log_request(account_id, job["task_id"], job["lid"], code, error)

            if code == 0 and zp:
                try:
                    row = flatten_detail(
                        zp, job["lid"], job.get("security_id", "")
                    )
                    action = db.insert_detail_snapshot(
                        job["task_id"], row, datetime.now()
                    )
                    dedupe.mark(job.get("encrypt_job_id", ""))
                    ok += 1
                    consecutive_risk = 0
                    print(f"[backfill] ok {job['lid']} -> {action}", flush=True)
                except Exception as exc:
                    fail += 1
                    print(f"[backfill] save failed {job['lid']}: {exc}", flush=True)
            else:
                fail += 1
                print(f"[backfill] fail {job['lid']} code={code} {error}", flush=True)
                if code in RISK_CODES:
                    consecutive_risk += 1
                    if consecutive_risk >= 2:
                        aborted = f"code={code}: {error}"
                        print(f"[backfill] aborted: {aborted}", flush=True)
                        break

        if aborted:
            db.set_account_detail_block(account_id, datetime.now() + timedelta(minutes=30))
        print(f"[backfill] done ok={ok} fail={fail} skipped={skipped} "
              f"aborted={aborted or '-'}")
    finally:
        if launched:
            am.close_browser(port)
            print("[backfill] browser closed", flush=True)


if __name__ == "__main__":
    main()
