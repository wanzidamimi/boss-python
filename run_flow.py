import argparse
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description="刷新 stoken 后执行纯接口采集")
    parser.add_argument("--wait", type=int, default=180, help="刷新 stoken 后的等待秒数")
    parser.add_argument("rest", nargs=argparse.REMAINDER, help="透传给 job_api.py 的参数")
    args = parser.parse_args()

    # argparse.REMAINDER 会把分隔用的 "--" 一并保留，这里剔除掉，
    # 否则 job_api.py 会把它当成一个未知参数而报错。
    rest = list(args.rest)
    if rest and rest[0] == "--":
        rest = rest[1:]

    print("[flow] step 1/3: generate stoken by browser JS runtime")
    subprocess.run([sys.executable, "stoken_browser.py"], cwd=BASE, check=True)

    if args.wait > 0:
        print(f"[flow] step 2/3: wait {args.wait}s")
        time.sleep(args.wait)

    print("[flow] step 3/3: call boss api")
    subprocess.run(
        [sys.executable, "job_api.py"] + rest,
        cwd=BASE,
        check=True,
    )


if __name__ == "__main__":
    main()
