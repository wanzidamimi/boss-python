import json
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def run(cmd):
    print(f"\n$ {' '.join(cmd)}")
    subprocess.run(cmd, cwd=BASE, check=True)


def load(path):
    return json.loads((BASE / path).read_text(encoding="utf-8"))


def test_prefix_match():
    run([sys.executable, "compare_stoken.py"])
    run(["node", "js_probe.js"])

    browser = load("data/browser_stoken.json")
    generated = load("data/generated_stoken.json")

    browser_prefix = browser["token"][:7]
    node_prefix = generated["token"][:7]
    browser_len = len(browser["token"])
    node_len = len(generated["token"])

    print(f"\n[RED/GREEN] browser prefix = {browser_prefix}, len={browser_len}")
    print(f"[RED/GREEN] node prefix    = {node_prefix}, len={node_len}")

    assert browser_prefix == node_prefix, (
        f"RED: fingerprint prefix mismatch: browser={browser_prefix}, node={node_prefix}"
    )
    assert abs(browser_len - node_len) <= 20, (
        f"RED: token length mismatch: browser={browser_len}, node={node_len}"
    )
    print("GREEN: stoken fingerprint and length matched")


if __name__ == "__main__":
    test_prefix_match()
