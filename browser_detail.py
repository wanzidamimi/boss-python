"""通过页面自身的 axios 实例抓详情，复用站点的 stoken 轮换机制。

原理（逆向自 app~2 前端包 49657 模块）：
- 服务端每次响应会下发 __zp_sseed__ / __zp_sname__ / __zp_sts__ 三个 Cookie；
- 页面请求拦截器在每次响应后调用安全 JS（iframe + ABC().z(seed, ts)）重新生成
  __zp_stoken__ 并清掉这三个种子 Cookie；
- 因此页面自身的请求 stoken 始终新鲜，不会出现"每令牌约 5 条后 code=37"。

直接用 requests 复刻请求头无法享受这套轮换（实测第 5 条必 37），所以这里改为
在页面上下文里调用应用自己的 axios 实例，让拦截器替我们维护 stoken。
"""

import asyncio
import json
import time

import requests
import websockets

from config import TARGET_URL

INIT_JS = r"""
(() => {
  if (window.__bossDetailApi && typeof window.__bossDetailApi.get === 'function') return 'ready';
  window.__bossDetailApi = null;
  const chunkKey = Object.keys(window).find(k => k.indexOf('webpackChunk') === 0);
  if (!chunkKey) return 'no-chunk';
  let req = null;
  window[chunkKey].push([[Math.random().toString(36).slice(2)], {}, function (r) { req = r; }]);
  if (!req) return 'no-require';
  const candidates = [];
  try { const m = req(49657); if (m && m.A) candidates.push(m.A); } catch (e) {}
  if (!candidates.length && req.m) {
    for (const id of Object.keys(req.m)) {
      let src = '';
      try { src = req.m[id].toString(); } catch (e) { continue; }
      if (src.indexOf('boss_safe_zpToken') >= 0) {
        try { const m = req(id); if (m && m.A) candidates.push(m.A); } catch (e) {}
      }
    }
  }
  const api = candidates.find(a => a && typeof a.get === 'function');
  if (!api) return 'no-api';
  window.__bossDetailApi = api;
  return 'ready';
})()
"""

FETCH_JS = r"""
(function () {
  if (!window.__bossDetailApi || typeof window.__bossDetailApi.get !== 'function') {
    return 'MISSING';
  }
  return window.__bossDetailApi.get('/wapi/zpgeek/job/detail.json', {
    params: {securityId: %s, lid: %s, _: Date.now()}
  }).then(function (r) { return JSON.stringify(r); });
})()
"""


class BrowserDetailError(RuntimeError):
    pass


def _targets(port: int):
    return json.loads(
        requests.get(f"http://127.0.0.1:{port}/json/list", timeout=5).text
    )


def _page_target(port: int):
    pages = [t for t in _targets(port) if t.get("type") == "page"]
    if not pages:
        raise BrowserDetailError(f"CDP {port} 无页面目标")
    return next(
        (t for t in pages if "zhipin.com" in t.get("url", "")),
        pages[0],
    )


async def _evaluate(ws_url: str, expression: str, await_promise: bool = False,
                    timeout: int = 60, max_size: int = 50_000_000):
    async with websockets.connect(
        ws_url, max_size=max_size, ping_interval=None
    ) as ws:
        await ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
                "userGesture": True,
            },
        }))
        while True:
            raw = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if raw.get("id") != 1:
                continue
            result = raw.get("result", {})
            if "exceptionDetails" in result:
                detail = result["exceptionDetails"].get("exception", {})
                raise BrowserDetailError(
                    detail.get("description") or "页面执行异常"
                )
            return result.get("result", {}).get("value")


def _navigate(port: int, url: str, wait: int = 12):
    page = _page_target(port)
    async def _do():
        async with websockets.connect(
            page["webSocketDebuggerUrl"], max_size=20_000_000, ping_interval=None
        ) as ws:
            await ws.send(json.dumps({
                "id": 1, "method": "Page.navigate", "params": {"url": url},
            }))
            await asyncio.sleep(wait)
    asyncio.run(_do())


class BrowserDetail:
    """在指定 CDP 端口页面上抓详情。"""

    def __init__(self, port: int):
        self.port = int(port)

    def ensure_ready(self, timeout: int = 20, navigate: bool = False) -> None:
        page = _page_target(self.port)
        if navigate or "zhipin.com" not in page.get("url", ""):
            _navigate(self.port, TARGET_URL, wait=12)
            page = _page_target(self.port)
            time.sleep(1)
        state = None
        for attempt in range(2):
            state = asyncio.run(
                _evaluate(page["webSocketDebuggerUrl"], INIT_JS, timeout=timeout)
            )
            if state == "ready":
                self._ws_url = page["webSocketDebuggerUrl"]
                return
            # 页面不是招聘 SPA（例如残留的职位详情页/安全校验页）：
            # 强制导航到列表页后重试
            print(f"[detail] init state={state}, navigate to {TARGET_URL}", flush=True)
            _navigate(self.port, TARGET_URL, wait=12)
            page = _page_target(self.port)
        raise BrowserDetailError(f"详情引擎初始化失败: {state}")

    def reload(self, wait: int = 12) -> None:
        """页面被站点重置/风控后，重新加载并重建 axios 实例。"""
        page = _page_target(self.port)
        async def _do():
            async with websockets.connect(
                page["webSocketDebuggerUrl"],
                max_size=20_000_000,
                ping_interval=None,
            ) as ws:
                await ws.send(json.dumps({
                    "id": 1, "method": "Page.reload", "params": {"ignoreCache": False},
                }))
                await asyncio.sleep(wait)
        asyncio.run(_do())
        self.ensure_ready()

    def fetch(self, security_id: str, lid: str, timeout: int = 40) -> dict:
        """抓一条详情，返回 detail.json 的完整 payload。

        页面被重置（重载/安全校验）会导致 window.__bossDetailApi 丢失，
        此时自动重建一次再重试，避免整批任务全部失败。
        """
        expr = FETCH_JS % (json.dumps(security_id), json.dumps(lid))
        last_error = None
        for attempt in range(2):
            try:
                raw = asyncio.run(
                    _evaluate(self._ws_url, expr, await_promise=True, timeout=timeout)
                )
            except BrowserDetailError as exc:
                last_error = exc
                raw = "MISSING"
            if raw == "MISSING":
                print("[detail] 页面上下文丢失，重建详情引擎 ...", flush=True)
                try:
                    self.ensure_ready()
                except Exception as exc:
                    last_error = exc
                    time.sleep(2)
                continue
            if not raw:
                raise BrowserDetailError("详情响应为空")
            return json.loads(raw)
        raise BrowserDetailError(f"详情引擎上下文不可用: {last_error}")

    def fetch_job(self, job: dict) -> tuple[int, dict | None, str]:
        """返回 (api_code, zpData, error)。37 等风控码会刷新页面重试一次。"""
        last_error = ""
        for attempt in range(2):
            try:
                payload = self.fetch(
                    job.get("securityId", ""), job.get("lid", "")
                )
            except Exception as exc:
                last_error = str(exc)[:200]
                if attempt == 0:
                    try:
                        self.reload()
                        continue
                    except Exception as reload_exc:
                        last_error = f"{last_error}; reload: {reload_exc}"[:200]
                return -1, None, last_error

            code = int(payload.get("code", -1))
            if code in (35, 36, 37) and attempt == 0:
                print(f"[detail] risk code={code}, reload page and retry", flush=True)
                try:
                    self.reload()
                    continue
                except Exception as exc:
                    return code, None, f"{payload.get('message', '')}; reload: {exc}"[:200]
            if code != 0:
                return code, None, str(payload.get("message", ""))
            return 0, payload.get("zpData") or {}, ""
        return -1, None, last_error


def is_available(port: int) -> bool:
    try:
        _page_target(port)
        return True
    except Exception:
        return False
