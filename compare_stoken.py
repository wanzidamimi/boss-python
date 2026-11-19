import asyncio
import json
from pathlib import Path

import requests
import websockets

from config import BASE_DIR, CDP_DEBUG_PORT, DATA_DIR

JS_PATH = BASE_DIR / "js" / "b9a070d3.js"
COOKIE_PATH = DATA_DIR / "cookies_logged.json.bak"


def get_seed_sts():
    data = json.loads(COOKIE_PATH.read_text(encoding="utf-8"))
    cookies = {item["name"]: item["value"] for item in data["cookies"]}
    return cookies["__zp_sseed__"], int(cookies["__zp_sts__"])


async def main():
    seed, sts = get_seed_sts()
    js_source = JS_PATH.read_text(encoding="utf-8")
    print(f"[params] seed={seed} sts={sts}")
    print(f"[js] {JS_PATH} len={len(js_source)}")

    target = next(
        t
        for t in requests.get(
            f"http://127.0.0.1:{CDP_DEBUG_PORT}/json/list", timeout=5
        ).json()
        if t.get("type") == "page"
    )

    next_id = 0
    pending = {}

    async with websockets.connect(
        target["webSocketDebuggerUrl"], max_size=100_000_000, ping_interval=None
    ) as ws:

        async def call(method, params=None, timeout=60):
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
                if msg.get("id") in pending:
                    fut = pending.pop(msg["id"])
                    if not fut.done():
                        fut.set_result(msg)

        reader_task = asyncio.create_task(reader())
        await call("Runtime.enable")

        await call(
            "Runtime.evaluate",
            {
                "expression": "window.__bossJsSource = " + json.dumps(js_source),
            },
        )

        expression = f"""
        (function () {{
          var seed = {json.dumps(seed)};
          var sts = {sts};
          var f = document.createElement('iframe');
          f.style.display = 'none';
          document.body.appendChild(f);
          var w = f.contentWindow;
          var d = w.document;
          w.__bossCanvasUrls = [];
          var orig = w.HTMLCanvasElement.prototype.toDataURL;
          w.HTMLCanvasElement.prototype.toDataURL = function () {{
            var r = orig.apply(this, arguments);
            w.__bossCanvasUrls.push(r);
            return r;
          }};
          w.__trace = [];
          function watch(obj, prefix, keys) {{
            keys.forEach(function (k) {{
              try {{
                var v = obj[k];
                Object.defineProperty(obj, k, {{
                  configurable: true,
                  get: function () {{ w.__trace.push(prefix + k); return v; }}
                }});
              }} catch (e) {{}}
            }});
          }}
          watch(w.navigator, 'navigator.', [
            'userAgent','platform','language','languages','webdriver',
            'hardwareConcurrency','vendor','appName','appVersion',
            'cookieEnabled','plugins','mimeTypes','doNotTrack',
            'maxTouchPoints','deviceMemory','webkitTemporaryStorage'
          ]);
          watch(w.screen, 'screen.', [
            'width','height','availWidth','availHeight','colorDepth','pixelDepth'
          ]);
          watch(d, 'document.', [
            'hidden','visibilityState','readyState','cookie',
            'characterSet','contentType','URL','referrer','title',
            'body','head','documentElement'
          ]);
          var origCreate = d.createElement.bind(d);
          d.createElement = function (t) {{
            w.__trace.push('createElement:' + t);
            return origCreate(t);
          }};

          var s = d.createElement('script');
          s.textContent = window.__bossJsSource;
          d.body.appendChild(s);
          var tz = new w.Date().getTimezoneOffset();
          var ts = sts + 60 * (480 + tz) * 1000;
          window.__bossFullTrace = (w.__trace || []).slice();
          var origStringify = w.JSON.stringify;
          w.__stringifyInputs = [];
          w.JSON.stringify = function () {{
            w.__stringifyInputs.push(arguments[0]);
            return origStringify.apply(w.JSON, arguments);
          }};
          w.__trace = [];
          var token = new w.ABC().z(seed, ts);
          w.JSON.stringify = origStringify;
          return JSON.stringify({{
            token: token,
            trace: w.__trace || [],
            fullTrace: window.__bossFullTrace || [],
            stringifyInputs: (w.__stringifyInputs || []).map(function (x) {{
              try {{ return origStringify(x); }} catch (e) {{ return String(x); }}
            }}).slice(0, 20),
            seed: seed,
            sts: sts,
            ts: ts,
            timezoneOffset: tz,
            canvasUrls: w.__bossCanvasUrls,
            navigator: {{
              userAgent: w.navigator.userAgent,
              platform: w.navigator.platform,
              language: w.navigator.language,
              languages: w.navigator.languages,
              webdriver: w.navigator.webdriver,
              hardwareConcurrency: w.navigator.hardwareConcurrency,
              vendor: w.navigator.vendor,
              appName: w.navigator.appName,
              appVersion: w.navigator.appVersion,
              cookieEnabled: w.navigator.cookieEnabled,
              pluginsLength: w.navigator.plugins && w.navigator.plugins.length,
              mimeTypesLength: w.navigator.mimeTypes && w.navigator.mimeTypes.length,
              doNotTrack: w.navigator.doNotTrack,
              maxTouchPoints: w.navigator.maxTouchPoints,
              deviceMemory: w.navigator.deviceMemory,
              webkitTemporaryStorageType: typeof w.navigator.webkitTemporaryStorage
            }},
            windowFingerprint: {{
              innerWidth: w.innerWidth,
              innerHeight: w.innerHeight,
              outerWidth: w.outerWidth,
              outerHeight: w.outerHeight,
              devicePixelRatio: w.devicePixelRatio,
              screenX: w.screenX,
              screenY: w.screenY,
              length: w.length,
              name: w.name,
              hasFrameElement: !!w.frameElement,
              hasTop: !!w.top
            }},
            documentExtra: {{
              characterSet: d.characterSet,
              contentType: d.contentType,
              URL: d.URL,
              referrer: d.referrer,
              title: d.title,
              headChildren: d.head ? d.head.children.length : 0,
              bodyChildren: d.body ? d.body.children.length : 0,
              cookies: d.cookie
            }},
            screen: {{
              width: w.screen.width,
              height: w.screen.height,
              availWidth: w.screen.availWidth,
              availHeight: w.screen.availHeight,
              colorDepth: w.screen.colorDepth,
              pixelDepth: w.screen.pixelDepth
            }},
            document: {{
              hidden: d.hidden,
              visibilityState: d.visibilityState,
              readyState: d.readyState
            }}
          }});
        }})()
        """

        evaluated = await call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
            timeout=60,
        )
        result = evaluated.get("result", {})
        if "exceptionDetails" in result:
            raise RuntimeError(json.dumps(result["exceptionDetails"], ensure_ascii=False)[:2000])

        value = json.loads(result["value"])
        out_path = DATA_DIR / "browser_stoken.json"
        out_path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        params_path = DATA_DIR / "test_params.json"
        params_path.write_text(
            json.dumps(
                {
                    "seed": value["seed"],
                    "sts": value["sts"],
                    "timezoneOffset": value["timezoneOffset"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        print(f"[OK] browser token len={len(value['token'])}")
        print(f"[OK] token prefix={value['token'][:80]}")
        (DATA_DIR / "browser_trace_full.json").write_text(
            json.dumps(value.get("fullTrace", []), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[OK] saved: {out_path}")
        print(f"[OK] saved: {params_path}")

        reader_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
