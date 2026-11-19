"""多账号凭证管理：启动受控浏览器、人工登录后抓取 Cookie、探活、凭证落盘。

设计要点：
- 登录永远由用户在弹出的浏览器里手动完成（扫码/手机号），本模块不绕过任何登录验证。
- 每个账号一个独立 Chrome profile + 独立 CDP 端口，避免登录态串号。
- 凭证（Cookie）存 data/accounts/<id>.json，profile 存 data/profiles/<id>/，均被 gitignore 排除。
- 用完关闭浏览器进程，保留 profile 以维持登录态。
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import requests
import websockets

from config import BASE_DIR, DATA_DIR, TARGET_URL
from job_api import BASE, new_trace_id, now_ms

ACCOUNTS_DIR = DATA_DIR / "accounts"
PROFILES_DIR = DATA_DIR / "profiles"
ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
PROFILES_DIR.mkdir(parents=True, exist_ok=True)

CHROME_PATH = os.getenv(
    "BOSS_CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe"
)
PORT_BASE = int(os.getenv("BOSS_CDP_PORT_BASE", "9300"))
LOGIN_TIMEOUT = 300
PAGE_SETTLE_SECONDS = 8

USER_INFO_URL = f"{BASE}/wapi/zpuser/wap/getUserInfo.json"
LOGIN_FAIL_CODES = {7, 17, 38}
KEEP_COOKIES = {
    "zp_at", "bst", "wt2", "wt2_r", "__g", "isOHPC", "ab_guid",
    "__zp_stoken__", "__zp_sseed__", "__zp_sname__", "__zp_sts__", "__a", "__c",
}


def cookie_file(account_id: str) -> Path:
    return ACCOUNTS_DIR / f"{account_id}.json"


def profile_dir(account_id: str) -> Path:
    return PROFILES_DIR / account_id


def next_port() -> int:
    """分配一个未被占用的 CDP 端口。"""
    import socket
    import db
    used = {int(a["cdp_port"]) for a in db.list_accounts()}
    port = PORT_BASE
    while port in used or not _port_free(port):
        port += 1
    return port


def _port_free(port: int) -> bool:
    import socket
    s = socket.socket()
    s.settimeout(0.2)
    try:
        return s.connect_ex(("127.0.0.1", port)) != 0
    finally:
        s.close()


def launch_browser(port: int, prof_dir: Path, url: str = TARGET_URL) -> None:
    """启动一个带 CDP 的独立浏览器窗口（可见），供用户手动登录。"""
    prof_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        CHROME_PATH,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={prof_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-allow-origins=*",
        url,
    ]
    if not Path(CHROME_PATH).exists():
        raise RuntimeError(f"Chrome 不存在: {CHROME_PATH}（可用 BOSS_CHROME_PATH 指定）")
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs
    )


def cdp_cookies(port: int) -> dict:
    """通过 CDP 读取指定端口浏览器里 zhipin.com 的 Cookie。"""
    targets = json.load(
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5)
    )
    page = next(
        (t for t in targets if t.get("type") == "page" and "zhipin.com" in t.get("url", "")),
        None,
    )
    if not page:
        page = next((t for t in targets if t.get("type") == "page"), None)
    if not page:
        raise RuntimeError(f"CDP {port} 无页面目标")
    return asyncio.run(_ws_get_cookies(page["webSocketDebuggerUrl"]))


async def _ws_get_cookies(ws_url: str) -> dict:
    async with websockets.connect(
        ws_url, max_size=20_000_000, ping_interval=None
    ) as ws:
        await ws.send(json.dumps({
            "id": 1,
            "method": "Network.getCookies",
            "params": {"urls": ["https://www.zhipin.com/"]},
        }))
        while True:
            raw = json.loads(await ws.recv())
            if raw.get("id") == 1:
                cookies = raw.get("result", {}).get("cookies", [])
                return {c["name"]: c["value"] for c in cookies}


def probe_login(cookies: dict) -> tuple[bool, int, str, str]:
    """探登录态。返回 (是否有效, code, message, boss_user_id)。"""
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
        "Referer": TARGET_URL,
        "traceId": new_trace_id(),
    }
    try:
        resp = session.get(
            USER_INFO_URL, params={"_": now_ms()}, headers=headers, timeout=20
        )
        payload = resp.json()
    except Exception as exc:
        return False, -1, str(exc)[:200], ""
    code = int(payload.get("code", -1))
    message = str(payload.get("message", ""))
    data = payload.get("zpData") or {}
    user_id = str(data.get("userId", ""))
    valid = code == 0 and bool(user_id) and code not in LOGIN_FAIL_CODES
    return valid, code, message, user_id


def load_account_cookies(account_id: str) -> dict:
    path = cookie_file(account_id)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {item["name"]: item["value"] for item in data.get("cookies", [])}


def save_account_cookies(account_id: str, cookies: dict, port: int, user_id: str = "") -> None:
    """把登录态写入账号凭证文件（含 CDP 端口，供 stoken 刷新脚本读取）。"""
    kept = {k: v for k, v in cookies.items() if k in KEEP_COOKIES}
    payload = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "account_id": account_id,
        "cdp_port": port,
        "boss_user_id": user_id,
        "cookies": [
            {"name": name, "value": value, "domain": ".zhipin.com", "path": "/"}
            for name, value in kept.items()
        ],
    }
    cookie_file(account_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def wait_for_login(account_id: str, port: int, timeout: int = LOGIN_TIMEOUT) -> dict:
    """轮询等待用户在浏览器完成登录；成功后抓 Cookie 存盘并返回结果。"""
    deadline = time.time() + timeout
    last = {"valid": False, "code": -1, "message": "等待登录", "boss_user_id": ""}
    while time.time() < deadline:
        time.sleep(3)
        try:
            cookies = cdp_cookies(port)
        except Exception as exc:
            last = {"valid": False, "code": -1, "message": f"读取浏览器失败: {exc}"[:200],
                    "boss_user_id": ""}
            continue
        valid, code, message, user_id = probe_login(cookies)
        last = {"valid": valid, "code": code, "message": message,
                "boss_user_id": user_id}
        if valid:
            save_account_cookies(account_id, cookies, port, user_id)
            return {"ok": True, "boss_user_id": user_id, "cookie_count": len(cookies)}
    return {"ok": False, **last}


def check_browser_login(account_id: str, port: int) -> dict:
    """检查浏览器中用户是否已完成登录；成功则抓 Cookie 存盘。供前端轮询调用。"""
    try:
        cookies = cdp_cookies(port)
    except Exception as exc:
        return {"ok": False, "done": False, "message": f"等待浏览器: {exc}"[:160]}
    valid, code, message, user_id = probe_login(cookies)
    if not valid:
        return {"ok": False, "done": False, "code": code, "message": f"等待登录(code={code})"}
    save_account_cookies(account_id, cookies, port, user_id)
    return {"ok": True, "done": True, "boss_user_id": user_id,
            "cookie_count": len(cookies), "message": "登录成功"}


def probe_account(account_id: str) -> dict:
    """用已保存的凭证探活，返回探活结果。"""
    cookies = load_account_cookies(account_id)
    if not cookies:
        return {"valid": False, "code": -1, "message": "无凭证文件"}
    valid, code, message, user_id = probe_login(cookies)
    return {"valid": valid, "code": code, "message": message, "boss_user_id": user_id}


def close_browser(port: int) -> None:
    """通过 CDP 关闭浏览器（Browser.close），失败则按端口 kill 进程。"""
    try:
        ver = json.load(
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3)
        )
        ws_url = ver.get("webSocketDebuggerUrl")
        if ws_url:
            asyncio.run(_ws_close(ws_url))
            return
    except Exception:
        pass
    _kill_by_port(port)


async def _ws_close(ws_url: str) -> None:
    async with websockets.connect(ws_url, max_size=1_000_000, ping_interval=None) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
        try:
            await asyncio.wait_for(ws.recv(), timeout=3)
        except Exception:
            pass


def _kill_by_port(port: int) -> None:
    """按 --remote-debugging-port=<port> 匹配并结束进程（Windows）。"""
    if sys.platform != "win32":
        return
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='chrome.exe'", "get",
             "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        for line in out.splitlines():
            if f"--remote-debugging-port={port}" in line:
                parts = line.strip().split(",")
                if parts and parts[-1].strip().isdigit():
                    subprocess.run(["taskkill", "/F", "/PID", parts[-1].strip()],
                                   capture_output=True)
    except Exception:
        pass
