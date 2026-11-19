import asyncio
import json
from datetime import datetime

import requests
import websockets

from config import CDP_DEBUG_PORT, DATA_DIR, TARGET_URL

COOKIE_PATH = DATA_DIR / "cookies_logged.json"
PAGE_SETTLE_SECONDS = 10
STOKEN_COOKIE = "__zp_stoken__"


def load_cookies():
    data = json.loads(COOKIE_PATH.read_text(encoding="utf-8"))
    return {item["name"]: item["value"] for item in data["cookies"]}


def save_browser_cookies(browser_cookies):
    """把浏览器当前产出的完整 Cookie 落盘，供 requests 直接使用。"""
    data = json.loads(COOKIE_PATH.read_text(encoding="utf-8"))
    items = {item["name"]: item for item in data["cookies"]}
    for name, value in browser_cookies.items():
        if name in items:
            items[name]["value"] = value
        else:
            items[name] = {
                "name": name,
                "value": value,
                "domain": ".zhipin.com",
                "path": "/",
            }
    data["cookies"] = list(items.values())
    data["stoken_generated_at"] = datetime.now().isoformat(timespec="seconds")
    COOKIE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _pick_page_target():
    targets = requests.get(
        f"http://127.0.0.1:{CDP_DEBUG_PORT}/json/list", timeout=5
    ).json()
    pages = [t for t in targets if t.get("type") == "page"]
    for target in pages:
        if "zhipin.com" in target.get("url", ""):
            return target
    return pages[0] if pages else None


async def refresh_browser_cookies():
    """重新加载目标页，读取浏览器自产的 stoken 与配套 Cookie。"""
    target = _pick_page_target()
    if not target:
        raise RuntimeError(f"no chrome page target on CDP port {CDP_DEBUG_PORT}")

    async with websockets.connect(
        target["webSocketDebuggerUrl"],
        max_size=100_000_000,
        ping_interval=None,
    ) as ws:
        next_id = 0
        pending = {}

        async def call(method, params=None, timeout=60):
            nonlocal next_id
            next_id += 1
            msg_id = next_id
            fut = asyncio.get_running_loop().create_future()
            pending[msg_id] = fut
            await ws.send(
                json.dumps(
                    {
                        "id": msg_id,
                        "method": method,
                        "params": params or {},
                    }
                )
            )
            result = await asyncio.wait_for(fut, timeout=timeout)
            if "error" in result:
                raise RuntimeError(f"{method}: {result['error']}")
            return result.get("result", {})

        async def reader():
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("id") in pending:
                    fut = pending.pop(msg["id"])
                    if not fut.done():
                        fut.set_result(msg)

        reader_task = asyncio.create_task(reader())
        await call("Page.enable")
        await call("Network.enable")

        # 重新加载目标页，触发官方 security-js 自行产出自洽的 stoken
        await call("Page.navigate", {"url": TARGET_URL})
        await asyncio.sleep(PAGE_SETTLE_SECONDS)

        result = await call(
            "Network.getCookies", {"urls": ["https://www.zhipin.com/"]}
        )
        browser_cookies = {
            item["name"]: item["value"] for item in result.get("cookies", [])
        }
        reader_task.cancel()
        return browser_cookies


def main():
    browser_cookies = asyncio.run(refresh_browser_cookies())
    stoken = browser_cookies.get(STOKEN_COOKIE, "")
    if not stoken:
        raise RuntimeError(
            "browser did not expose __zp_stoken__, page may be blocked"
        )
    save_browser_cookies(browser_cookies)
    print(f"[stoken] browser-issued token len={len(stoken)} prefix={stoken[:8]}")
    print(f"[stoken] cookies synced: {len(browser_cookies)} items")


if __name__ == "__main__":
    main()
