import asyncio
import json
from datetime import datetime
from pathlib import Path

import requests
import websockets

from config import CDP_DEBUG_PORT, DATA_DIR, TARGET_URL

PATCH_SOURCE = r"""
window.__bossCanvasUrls = [];
window.__bossZArgs = [];
window.__bossIframeCanvasUrls = [];
window.__bossIframeZArgs = [];

(function () {
  const orig = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function () {
    const result = orig.apply(this, arguments);
    window.__bossCanvasUrls.push(result);
    return result;
  };

  let abcValue;
  Object.defineProperty(window, 'ABC', {
    configurable: true,
    get() { return abcValue; },
    set(value) {
      abcValue = value;
      try {
        const origZ = value.prototype.z;
        value.prototype.z = function () {
          window.__bossZArgs.push({ seed: arguments[0], ts: arguments[1] });
          return origZ.apply(this, arguments);
        };
      } catch (_) {}
    }
  });

  setInterval(function () {
    try {
      const frames = document.querySelectorAll('iframe');
      for (let i = 0; i < frames.length; i += 1) {
        const w = frames[i].contentWindow;
        if (!w) continue;
        const value = w.ABC;
        if (value && value.prototype && value.prototype.z && !value.prototype.__bossHooked) {
          const origZ = value.prototype.z;
          value.prototype.z = function () {
            window.__bossIframeZArgs.push({ seed: arguments[0], ts: arguments[1] });
            return origZ.apply(this, arguments);
          };
          value.prototype.__bossHooked = true;
        }
      }
    } catch (_) {}
  }, 1);

  const origCreate = document.createElement;
  document.createElement = function () {
    const el = origCreate.apply(this, arguments);
    const tag = String(arguments[0] || '').toLowerCase();
    if (tag !== 'iframe') return el;

    const patchFrame = function () {
      const w = el.contentWindow;
      if (!w) return false;
      try {
        const origToDataURL = w.HTMLCanvasElement.prototype.toDataURL;
        w.HTMLCanvasElement.prototype.toDataURL = function () {
          const result = origToDataURL.apply(this, arguments);
          window.__bossIframeCanvasUrls.push(result);
          return result;
        };
      } catch (_) {}

      const hookAbc = function () {
        try {
          const value = w.ABC;
          if (value && value.prototype && value.prototype.z && !value.prototype.__bossHooked) {
            const origZ = value.prototype.z;
            value.prototype.z = function () {
              window.__bossIframeZArgs.push({ seed: arguments[0], ts: arguments[1] });
              return origZ.apply(this, arguments);
            };
            value.prototype.__bossHooked = true;
            return true;
          }
        } catch (_) {}
        return false;
      };

      if (!hookAbc()) {
        let tries = 0;
        const timer = setInterval(function () {
          tries += 1;
          if (hookAbc() || tries > 3000) clearInterval(timer);
        }, 1);
      }
      return true;
    };

    let frameTries = 0;
    const tryPatchFrame = function () {
      if (patchFrame()) return;
      frameTries += 1;
      if (frameTries < 500) setTimeout(tryPatchFrame, 5);
    };
    tryPatchFrame();
    return el;
  };
})();
"""


async def main():
    targets = requests.get(
        f"http://127.0.0.1:{CDP_DEBUG_PORT}/json/list", timeout=5
    ).json()
    target = next(t for t in targets if t.get("type") == "page")
    print(f"[CDP] target: {target.get('url')}")

    next_id = 0
    pending = {}

    async with websockets.connect(
        target["webSocketDebuggerUrl"], max_size=50_000_000, ping_interval=None
    ) as ws:

        async def call(method, params=None, timeout=30):
            nonlocal next_id
            next_id += 1
            msg_id = next_id
            fut = asyncio.get_running_loop().create_future()
            pending[msg_id] = fut
            await ws.send(
                json.dumps({"id": msg_id, "method": method, "params": params or {}})
            )
            result = await asyncio.wait_for(fut, timeout=timeout)
            if "error" in result:
                raise RuntimeError(f"{method}: {result['error']}")
            return result.get("result", {})

        async def reader():
            async for raw in ws:
                msg = json.loads(raw)
                msg_id = msg.get("id")
                if msg_id in pending:
                    fut = pending.pop(msg_id)
                    if not fut.done():
                        fut.set_result(msg)

        reader_task = asyncio.create_task(reader())

        await call("Page.enable")
        await call("Runtime.enable")

        added = await call("Page.addScriptToEvaluateOnNewDocument", {"source": PATCH_SOURCE})
        script_id = added.get("identifier")

        print(f"[CDP] navigate: {TARGET_URL}")
        await call("Page.navigate", {"url": TARGET_URL})
        await asyncio.sleep(10)

        expression = r"""
        JSON.stringify({
          canvasUrls: window.__bossCanvasUrls || [],
          zArgs: window.__bossZArgs || [],
          navigator: {
            userAgent: navigator.userAgent,
            platform: navigator.platform,
            language: navigator.language,
            languages: navigator.languages,
            webdriver: navigator.webdriver,
            hardwareConcurrency: navigator.hardwareConcurrency,
            vendor: navigator.vendor,
            appName: navigator.appName,
            appVersion: navigator.appVersion,
            cookieEnabled: navigator.cookieEnabled
          },
          screen: {
            width: screen.width,
            height: screen.height,
            availWidth: screen.availWidth,
            availHeight: screen.availHeight,
            colorDepth: screen.colorDepth,
            pixelDepth: screen.pixelDepth
          },
          document: {
            hidden: document.hidden,
            visibilityState: document.visibilityState,
            readyState: document.readyState
          },
          location: {
            href: location.href,
            origin: location.origin,
            pathname: location.pathname
          },
          timezoneOffset: new Date().getTimezoneOffset(),
          cookies: document.cookie,
          iframe: (function () {
            var f = document.querySelector('iframe[name="zhipinFrame"]');
            if (!f || !f.contentWindow) return null;
            var w = f.contentWindow;
            var d = w.document;
            return {
              canvasUrls: window.__bossIframeCanvasUrls || w.__bossCanvasUrls || [],
              zArgs: window.__bossIframeZArgs || w.__bossZArgs || [],
              navigator: {
                userAgent: w.navigator.userAgent,
                platform: w.navigator.platform,
                language: w.navigator.language,
                languages: w.navigator.languages,
                webdriver: w.navigator.webdriver,
                hardwareConcurrency: w.navigator.hardwareConcurrency,
                vendor: w.navigator.vendor,
                appName: w.navigator.appName,
                appVersion: w.navigator.appVersion,
                cookieEnabled: w.navigator.cookieEnabled
              },
              screen: {
                width: w.screen.width,
                height: w.screen.height,
                availWidth: w.screen.availWidth,
                availHeight: w.screen.availHeight,
                colorDepth: w.screen.colorDepth,
                pixelDepth: w.screen.pixelDepth
              },
              document: {
                hidden: d.hidden,
                visibilityState: d.visibilityState,
                readyState: d.readyState
              },
              timezoneOffset: new w.Date().getTimezoneOffset(),
              cookies: d.cookie
            };
          })()
        })
        """
        evaluated = await call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
        )
        raw_value = evaluated.get("result", {}).get("value", "{}")
        data = json.loads(raw_value)
        data["captured_at"] = datetime.now().isoformat(timespec="seconds")

        out_path = DATA_DIR / "browser_fingerprint.json"
        out_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[OK] saved: {out_path}")
        print(f"[OK] canvasUrls: {len(data.get('canvasUrls', []))}")
        print(f"[OK] screen: {data.get('screen')}")

        if script_id:
            try:
                await call("Page.removeScriptToEvaluateOnNewDocument", {"identifier": script_id})
            except Exception:
                pass

        reader_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
