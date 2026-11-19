import asyncio
import base64
import json
from datetime import datetime
from pathlib import Path

import requests
import websockets

from config import CAPTURE_DIR, CDP_DEBUG_PORT, INTERESTING_URL_PARTS, TARGET_URL


class CDPClient:
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.ws = None
        self.next_id = 0
        self.pending = {}
        self.reader_task = None

    async def connect(self, on_event):
        self.ws = await websockets.connect(
            self.ws_url,
            max_size=50_000_000,
            ping_interval=None,
        )
        self.reader_task = asyncio.create_task(self._read_loop(on_event))

    async def _read_loop(self, on_event):
        async for raw in self.ws:
            try:
                msg = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue

            msg_id = msg.get("id")
            if msg_id is not None and msg_id in self.pending:
                future = self.pending.pop(msg_id)
                if not future.done():
                    future.set_result(msg)
                continue

            method = msg.get("method")
            if method:
                on_event(method, msg.get("params", {}))

    async def call(self, method: str, params: dict | None = None, timeout: float = 20.0):
        self.next_id += 1
        msg_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[msg_id] = future
        await self.ws.send(
            json.dumps(
                {
                    "id": msg_id,
                    "method": method,
                    "params": params or {},
                }
            )
        )
        result = await asyncio.wait_for(future, timeout=timeout)
        if "error" in result:
            raise RuntimeError(f"CDP {method} failed: {result['error']}")
        return result.get("result", {})

    async def close(self):
        if self.reader_task:
            self.reader_task.cancel()
        if self.ws:
            await self.ws.close()


def find_page_target() -> dict:
    url = f"http://127.0.0.1:{CDP_DEBUG_PORT}/json/list"
    targets = requests.get(url, timeout=5).json()
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        raise RuntimeError("No Chrome page target found on CDP debug port")

    for target in pages:
        if "zhipin.com" in (target.get("url") or ""):
            return target
    return pages[0]


def decode_body(body: dict | None) -> str:
    if not body:
        return ""
    text = body.get("body") or body.get("text") or ""
    if body.get("base64Encoded") and text:
        try:
            return base64.b64decode(text).decode("utf-8", errors="replace")
        except Exception:
            return text
    return text


async def main():
    target = find_page_target()
    print(f"[CDP] target: {target.get('url')}")

    state = {
        "requests": {},
        "all_api_requests": [],
        "interesting": [],
        "interesting_ids": set(),
        "bodies": {},
        "body_tasks": [],
        "body_scheduled": set(),
        "loaded": asyncio.get_running_loop().create_future(),
    }

    client = None

    async def fetch_body(request_id: str):
        try:
            body = await client.call(
                "Network.getResponseBody", {"requestId": request_id}, timeout=15
            )
            state["bodies"][request_id] = decode_body(body)
        except Exception as exc:
            state["bodies"][request_id] = f"<getResponseBody failed: {exc}>"

    def on_event(method: str, params: dict):
        if method == "Network.requestWillBeSent":
            request_id = params.get("requestId")
            request = params.get("request", {})
            resource_type = params.get("type")
            meta = {
                "requestId": request_id,
                "url": request.get("url"),
                "method": request.get("method"),
                "resourceType": resource_type,
                "headers": request.get("headers", {}),
                "postData": request.get("postData"),
                "timestamp": params.get("timestamp"),
                "documentURL": params.get("documentURL"),
            }
            state["requests"][request_id] = meta
            if resource_type in ("XHR", "Fetch"):
                state["all_api_requests"].append(meta)

        elif method == "Network.responseReceived":
            request_id = params.get("requestId")
            response = params.get("response", {})
            url = response.get("url", "")
            if any(part in url for part in INTERESTING_URL_PARTS):
                state["interesting_ids"].add(request_id)
                state["interesting"].append(
                    {
                        "requestId": request_id,
                        "url": url,
                        "status": response.get("status"),
                        "mimeType": response.get("mimeType"),
                        "headers": response.get("headers", {}),
                    }
                )

        elif method == "Network.loadingFinished":
            request_id = params.get("requestId")
            if (
                request_id in state["interesting_ids"]
                and request_id not in state["body_scheduled"]
            ):
                state["body_scheduled"].add(request_id)
                state["body_tasks"].append(asyncio.create_task(fetch_body(request_id)))

        elif method == "Page.loadEventFired":
            if not state["loaded"].done():
                state["loaded"].set_result(True)

    client = CDPClient(target["webSocketDebuggerUrl"])
    await client.connect(on_event)

    await client.call("Network.enable")
    await client.call("Page.enable")
    await client.call("Network.setCacheDisabled", {"cacheDisabled": True})

    print(f"[CDP] navigate: {TARGET_URL}")
    await client.call("Page.navigate", {"url": TARGET_URL})

    try:
        await asyncio.wait_for(state["loaded"], timeout=30)
    except asyncio.TimeoutError:
        print("[WARN] Page.loadEventFired timeout, continue after short wait")

    await asyncio.sleep(8)
    if state["body_tasks"]:
        await asyncio.gather(*state["body_tasks"], return_exceptions=True)

    result_items = []
    for item in state["interesting"]:
        request_id = item["requestId"]
        request_meta = state["requests"].get(request_id, {})
        result_items.append(
            {
                **item,
                "request": request_meta,
                "responseBody": state["bodies"].get(
                    request_id, "<response body not captured>"
                ),
            }
        )

    try:
        cookies = await client.call("Network.getCookies", {"urls": [TARGET_URL]})
        cookie_list = cookies.get("cookies", [])
    except Exception as exc:
        cookie_list = [{"error": str(exc)}]

    output = {
        "target_url": TARGET_URL,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "interesting_request_count": len(result_items),
        "all_api_requests": state["all_api_requests"],
        "requests": result_items,
        "cookies": cookie_list,
    }

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = CAPTURE_DIR / f"boss_unlogged_{ts}.json"
    out_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n[OK] captured {len(state['all_api_requests'])} XHR/Fetch requests")
    print(f"[OK] captured {len(result_items)} interesting requests")
    print(f"[OK] saved: {out_path}")

    print("\n[all XHR/Fetch]")
    for req in state["all_api_requests"]:
        print(f"  {req.get('method')} {req.get('url')}")

    for item in result_items:
        print("\n" + "=" * 80)
        print(f"{item['request'].get('method')} {item['url']}")
        print(f"status={item['status']} type={item['request'].get('resourceType')}")
        if item["request"].get("postData"):
            print("postData:", item["request"]["postData"])
        body_preview = item["responseBody"][:1200]
        print("responseBody preview:")
        print(body_preview)

    print("\n[cookies]")
    for cookie in cookie_list:
        if "name" in cookie:
            print(f"  {cookie['name']}={str(cookie.get('value', ''))[:80]}")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
