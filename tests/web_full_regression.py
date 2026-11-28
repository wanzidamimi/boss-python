"""Web 端全功能回归：页面点击流 + API + MySQL 数据校验。

用法：
    python tests/web_full_regression.py

只在本机 127.0.0.1:8899 服务上运行。会临时写入两条候选技能用于验证人工确认/忽略，结束后清理。
不会触发真实 BOSS 采集任务，也不会删除账号或弹出登录浏览器。
"""

from __future__ import annotations

import atexit
import json
import random
import string
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import expect, sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import db

BASE = "http://127.0.0.1:8899"
OUT_DIR = Path(r"D:\Scrapy-project\artifacts\web_full_regression")
OUT_DIR.mkdir(parents=True, exist_ok=True)

SUFFIX = "".join(random.choice(string.ascii_lowercase) for _ in range(6))
RAW_APPROVE = f"回归测试候选{SUFFIX}"
KEY_APPROVE = f"regression_approve_{SUFFIX}"
CUSTOM_NAME = f"回归正式技能{SUFFIX}"
CUSTOM_CAT = f"回归分类{SUFFIX}"
RAW_REJECT = f"回归忽略候选{SUFFIX}"
KEY_REJECT = f"regression_reject_{SUFFIX}"

results: list[dict] = []
console_errors: list[str] = []
old_catalog_version: str | None = None
cleaned_up = False


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append({"name": name, "ok": bool(ok), "detail": str(detail)[:1200]})
    print(("PASS  " if ok else "FAIL  ") + name + ("  | " + str(detail)[:300] if detail else ""))


def skip(name: str, detail: str) -> None:
    results.append({"name": name, "ok": None, "detail": detail})
    print("SKIP  " + name + "  | " + detail)


def api_check(name: str, method: str, path: str, expected: int = 200,
              allow: tuple[int, ...] = (), **kwargs) -> dict | None:
    try:
        res = requests.request(method, BASE + path, timeout=60, **kwargs)
        ok = res.status_code == expected or (allow and res.status_code in allow)
        text = res.text[:500]
        record(name, ok, f"HTTP {res.status_code}; {text}")
        if ok and res.headers.get("content-type", "").startswith("application/json"):
            return res.json()
        return None
    except Exception as exc:  # noqa: BLE001
        record(name, False, repr(exc))
        return None


def db_one(sql: str, params: tuple = ()) -> dict | None:
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()


def db_count(sql: str, params: tuple = ()) -> int:
    row = db_one(sql, params)
    return int(row["n"]) if row else 0


def save_shot(page, name: str) -> None:
    try:
        page.screenshot(path=str(OUT_DIR / f"{name}.png"), full_page=True)
    except Exception as exc:  # noqa: BLE001
        record(f"截图 {name}", False, repr(exc))


def attach_console(page) -> None:
    def on_console(msg) -> None:
        if msg.type == "error" and "favicon.ico" not in msg.text:
            location = msg.location or {}
            console_errors.append(f"console: {msg.text} @ {location.get('url', '')}")

    def on_page_error(exc) -> None:
        console_errors.append(f"pageerror: {exc}")

    def on_request_failed(request) -> None:
        if "favicon.ico" in request.url:
            return
        # 下载触发的导航会被浏览器主动 abort，这不是失败
        if "/api/export" in request.url and request.failure == "net::ERR_ABORTED":
            return
        console_errors.append(f"requestfailed: {request.url} {request.failure}")

    page.on("console", on_console)
    page.on("pageerror", on_page_error)
    page.on("requestfailed", on_request_failed)


def wait_toast(page, keyword: str, timeout: int = 15000) -> None:
    page.locator("#toasts .toast", has_text=keyword).first.wait_for(
        state="visible", timeout=timeout
    )


def prepare_temp_candidates() -> tuple[int, int]:
    db.upsert_skill_candidates([
        {
            "raw_mention": RAW_APPROVE,
            "normalized_key": KEY_APPROVE,
            "hit_count": 1,
            "evidence_sentence": f"回归测试：{RAW_APPROVE} 用于人工确认验证",
            "status": "pending",
            "llm_status": "none",
            "source": "web_regression",
        },
        {
            "raw_mention": RAW_REJECT,
            "normalized_key": KEY_REJECT,
            "hit_count": 1,
            "evidence_sentence": f"回归测试：{RAW_REJECT} 用于忽略验证",
            "status": "pending",
            "llm_status": "none",
            "source": "web_regression",
        },
    ])
    a = db_one("SELECT id FROM skill_candidates WHERE normalized_key=%s", (KEY_APPROVE,))
    b = db_one("SELECT id FROM skill_candidates WHERE normalized_key=%s", (KEY_REJECT,))
    return int(a["id"]), int(b["id"])


def cleanup_temp_data() -> None:
    global cleaned_up
    if cleaned_up:
        return
    cleaned_up = True
    try:
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM skill_catalog WHERE name=%s", (CUSTOM_NAME,))
                cur.execute(
                    "DELETE FROM skill_candidates WHERE normalized_key IN (%s, %s)",
                    (KEY_APPROVE, KEY_REJECT),
                )
        if old_catalog_version:
            db.set_skill_catalog_meta("version", old_catalog_version)
        record("清理临时测试数据", True, "已删除临时候选与临时正式技能，并恢复词典版本")
    except Exception as exc:  # noqa: BLE001
        record("清理临时测试数据", False, repr(exc))


atexit.register(cleanup_temp_data)


def main() -> int:
    global old_catalog_version

    meta = db_one("SELECT meta_value FROM skill_catalog_meta WHERE meta_key='version'")
    old_catalog_version = meta["meta_value"] if meta else None

    # ---------- API 层 ----------
    api_check("API 根服务存活", "GET", "/")
    stats_api = api_check("API /api/stats", "GET", "/api/stats")
    api_check("API /api/cities", "GET", "/api/cities")
    catalog_api = api_check("API /api/skills/catalog", "GET", "/api/skills/catalog")
    graph_api = api_check(
        "API /api/skills/graph", "GET", "/api/skills/graph?keyword=%E7%88%AC%E8%99%AB&min_jobs=1&limit=20"
    )
    rec_api = api_check(
        "API /api/skills/recommend 爬虫工程师", "POST", "/api/skills/recommend",
        json={"position": "爬虫工程师"},
    )
    rec2_api = api_check(
        "API /api/skills/recommend Python", "POST", "/api/skills/recommend",
        json={"position": "Python"},
    )
    api_check(
        "API /api/skills/match", "POST", "/api/skills/match",
        json={"keyword": "爬虫工程师", "user_skills": ["Python", "Requests"]},
    )
    candidates_api = api_check(
        "API /api/skills/candidates", "GET", "/api/skills/candidates?limit=5"
    )
    jobs_api = api_check("API /api/jobs", "GET", "/api/jobs?limit=5&with_details=1")
    tasks_api = api_check("API /api/tasks", "GET", "/api/tasks")
    accounts_api = api_check("API /api/accounts", "GET", "/api/accounts")

    if stats_api and catalog_api:
        db_stats = db.collect_stats()
        ok = stats_api.get("jobs_total") == db_stats["jobs_total"]
        record("API 统计与 MySQL 一致", ok, f"api={stats_api.get('jobs_total')}, db={db_stats['jobs_total']}")
    if catalog_api:
        ok = catalog_api.get("source") == "mysql.skill_catalog" and len(catalog_api.get("skills", [])) > 0
        record("词典来源为 MySQL", ok, f"source={catalog_api.get('source')}, count={len(catalog_api.get('skills', []))}")
    if graph_api:
        record("技能图有节点", len(graph_api.get("nodes", [])) > 0, f"nodes={len(graph_api.get('nodes', []))}")
    if rec_api and rec2_api:
        ok = rec_api.get("job_count", 0) > 0 and rec2_api.get("job_count", 0) > 0
        record("两次职位推荐均有数据", ok,
               f"爬虫工程师={rec_api.get('job_count')}, Python={rec2_api.get('job_count')}")
    if candidates_api:
        llm = candidates_api.get("llm", {})
        record("LLM 未配置时接口明确提示", llm.get("configured") is False, json.dumps(llm, ensure_ascii=False))

    jobs = jobs_api if isinstance(jobs_api, list) else []
    first_with_detail = next((j for j in jobs if j.get("detail_id") or j.get("detail_hash")), None)
    if first_with_detail:
        lid = first_with_detail.get("lid")
        api_check("API /api/details/{lid}", "GET", f"/api/details/{lid}")
    else:
        skip("API /api/details/{lid}", "前 5 条职位没有详情，跳过")

    tasks = tasks_api if isinstance(tasks_api, list) else []
    if tasks:
        tid = tasks[0]["task_id"]
        api_check("API /api/tasks/{id}", "GET", f"/api/tasks/{tid}")
        api_check("API /api/tasks/{id}/progress", "GET", f"/api/tasks/{tid}/progress")
        api_check("API /api/tasks/{id}/log", "GET", f"/api/tasks/{tid}/log",
                  allow=(404,))
    else:
        skip("任务详情/进度/日志", "无任务记录")

    export = requests.get(BASE + "/api/export?format=csv&with_details=1", timeout=60)
    ok = export.status_code == 200 and "attachment" in export.headers.get("content-disposition", "")
    record("API /api/export CSV", ok, f"HTTP {export.status_code}, bytes={len(export.content)}")

    invalid_task = {
        "mode": "search", "keyword": "python", "city": "", "pages": 11,
        "page_size": 15, "detail_limit": 0, "detail_interval": 2,
        "delay": 2, "account_id": "",
    }
    api_check("API /api/tasks 参数校验拒绝 pages=11", "POST", "/api/tasks",
              expected=400, json=invalid_task)
    api_check("API /api/open-link 拒绝非 zhipin 域名", "POST", "/api/open-link",
              expected=400, json={"url": "https://example.com", "task_id": ""})
    skip("API POST /api/tasks 真实采集", "会产生外部抓取、浏览器与账号副作用，未在本轮自动触发")
    skip("API 账号登录/重新登录/删除", "会启动浏览器、写 Cookie 或删除 profile，未在本轮自动触发")

    # ---------- 页面点击流 ----------
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True, viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        attach_console(page)

        # 数据采集页
        page.goto(BASE + "/", wait_until="networkidle", timeout=60000)
        navs = page.locator(".topnav a")
        nav_hrefs = [navs.nth(i).get_attribute("href") for i in range(navs.count())]
        record("导航包含三个页面", nav_hrefs == ["/", "/skills", "/candidates"], str(nav_hrefs))
        for key in ("#sJobs", "#sTasks", "#sToday", "#sFailed", "#sDetails"):
            page.locator(key).wait_for(state="visible")
        record("统计卡与状态灯可见", page.locator("#healthText").is_visible(),
               page.locator("#healthText").inner_text())
        record("账号表有数据", page.locator("#accountBody tr").count() > 0,
               f"rows={page.locator('#accountBody tr').count()}")

        page.locator("#cityName").fill("北京")
        page.locator("#citySuggest .item").first.wait_for(state="visible", timeout=10000)
        item = page.locator("#citySuggest .item").first
        expected_code = item.get_attribute("data-code")
        item.click()
        city_code = page.locator("#city").input_value()
        record("城市联想写入隐藏编码", city_code == expected_code,
               f"city={city_code}, expected={expected_code}")

        page.locator("#mode").select_option("recommend")
        rec_disabled = page.locator("#keyword").is_disabled()
        page.locator("#mode").select_option("search")
        search_enabled = page.locator("#keyword").is_enabled()
        record("采集模式切换控制关键词输入", rec_disabled and search_enabled,
               f"recommend_disabled={rec_disabled}, search_enabled={search_enabled}")

        page.locator("#keyword").fill("python")
        page.locator("#pages").fill("11")
        page.locator("#submit").click()
        expect(page.locator("#submitMsg")).to_contain_text("提交失败", timeout=15000)
        record("采集页无效参数显示提交失败", True, page.locator("#submitMsg").inner_text())
        console_errors.clear()
        page.locator("#pages").fill("1")

        first_task_link = page.locator("#taskBody .task-link").first
        if first_task_link.count():
            first_task_link.click()
            page.locator("#taskProgressMask").wait_for(state="visible", timeout=10000)
            record("任务进度弹窗可打开", True, "")
            page.locator("#taskProgressMask .modal-head button").first.click()
        else:
            skip("任务进度弹窗", "任务表无任务链接")

        detail_btn = page.locator('#jobBody button:has-text("查看")').first
        if detail_btn.count():
            detail_btn.click()
            page.locator("#detailMask").wait_for(state="visible", timeout=10000)
            title = page.locator("#detailTitle").inner_text()
            record("职位详情弹窗可打开", bool(title.strip()), title[:120])
            page.locator("#detailMask .modal-head button").first.click()
        else:
            skip("职位详情弹窗", "职位表暂无可查看的详情按钮")

        page.locator("#filterKeyword").fill("python")
        page.locator("#filterKeyword").dispatch_event("change")
        page.wait_for_timeout(1200)
        record("职位筛选不报错", page.locator("#jobCount").is_visible(),
               page.locator("#jobCount").inner_text())

        try:
            with page.expect_download(timeout=15000) as download_info:
                page.locator('button[onclick="doExport(\'csv\')"]').click()
            download = download_info.value
            record("页面导出 CSV 触发下载", download.suggested_filename.endswith(".csv"),
                   download.suggested_filename)
        except Exception as exc:  # noqa: BLE001
            record("页面导出 CSV 触发下载", False, repr(exc))

        save_shot(page, "01_index")
        page_errors_index = list(console_errors)

        # 技能图谱页
        console_errors.clear()
        page.goto(BASE + "/skills", wait_until="networkidle", timeout=60000)
        page.locator("summary").filter(has_text="可选：看看我会哪些技能").click()
        page.locator("#catalogSkills .pill").first.wait_for(state="visible", timeout=15000)
        record("技能词典胶囊已渲染", page.locator("#catalogSkills .pill").count() > 0,
               f"pills={page.locator('#catalogSkills .pill').count()}")

        jobs_before = db_count("SELECT COUNT(*) AS n FROM job_skills")
        page.locator("#rebuildBtn").click()
        wait_toast(page, "重建完成", timeout=60000)
        jobs_after = db_count("SELECT COUNT(*) AS n FROM job_skills")
        record("重建技能索引按钮生效", jobs_after > 0,
               f"before={jobs_before}, after={jobs_after}, toast={page.locator('#toasts').inner_text()[:200]}")

        page.locator("#positionInput").fill("爬虫工程师")
        page.locator("#recommendBtn").click()
        expect(page.locator("#heatmapMeta")).to_contain_text("个职位", timeout=30000)
        first_meta = page.locator("#heatmapMeta").inner_text()
        first_rows = page.locator("#skillBody tr").count()
        chart_ok = page.locator("#heatmapChart").is_visible() and page.locator("#heatmapChart canvas").count() > 0
        record("ECharts 词云 canvas 渲染", chart_ok,
               f"canvas={page.locator('#heatmapChart canvas').count()}, pills={page.locator('#heatmapChart .pill').count()}")
        record("首次职位热力图与技能表渲染", chart_ok and first_rows > 0,
               f"meta={first_meta}, rows={first_rows}")
        save_shot(page, "02_skills_first")

        page.locator("#positionInput").fill("Python")
        page.locator("#recommendBtn").click()
        page.wait_for_function(
            "(old) => document.querySelector('#heatmapMeta').textContent !== old",
            arg=first_meta, timeout=30000,
        )
        second_meta = page.locator("#heatmapMeta").inner_text()
        second_rows = page.locator("#skillBody tr").count()
        record("二次查询结果重新渲染", second_rows > 0 and second_meta != first_meta,
               f"first={first_meta}, second={second_meta}, rows={second_rows}")
        save_shot(page, "03_skills_second")

        page.locator("#userSkillInput").fill("Python")
        page.locator("#userSkillInput").press("Enter")
        page.locator("#catalogSkills .pill").first.click()
        page.locator("#matchBtn").click()
        expect(page.locator("#matchMsg")).to_contain_text("覆盖率", timeout=30000)
        record("技能匹配覆盖率计算", "%" in page.locator("#matchMsg").inner_text(),
               f"matchMsg={page.locator('#matchMsg').inner_text()}")
        save_shot(page, "04_skills_match")
        page_errors_skills = list(console_errors)

        # 候选技能池页
        console_errors.clear()
        page.goto(BASE + "/candidates", wait_until="networkidle", timeout=60000)
        expect(page.locator("#count")).not_to_contain_text("加载中", timeout=15000)
        record("候选池页面加载", page.locator("#candidateBody tr").count() > 0,
               page.locator("#count").inner_text())
        record("候选页显示 LLM 状态", "LLM" in page.locator("#llmStatus").inner_text(),
               page.locator("#llmStatus").inner_text())

        # 新增筛选功能：默认待处理、状态切换、关键词搜索、重置
        total_items = requests.get(BASE + "/api/skills/candidates?limit=500", timeout=30).json().get("items", [])
        pending_badges = page.locator("#candidateBody .badge").all_inner_texts()
        record("候选池默认只显示待处理", bool(pending_badges) and all(s == "pending" for s in pending_badges),
               f"rows={len(pending_badges)}, count={page.locator('#count').inner_text()}")

        page.locator("#filterStatus").select_option("approved")
        page.wait_for_timeout(300)
        approved_badges = page.locator("#candidateBody .badge").all_inner_texts()
        record("状态筛选：已加入", bool(approved_badges) and all(s == "approved" for s in approved_badges),
               f"rows={len(approved_badges)}, count={page.locator('#count').inner_text()}")

        if any(r.get("status") == "rejected" for r in total_items):
            page.locator("#filterStatus").select_option("rejected")
            page.wait_for_timeout(300)
            rejected_badges = page.locator("#candidateBody .badge").all_inner_texts()
            record("状态筛选：已忽略", bool(rejected_badges) and all(s == "rejected" for s in rejected_badges),
                   f"rows={len(rejected_badges)}, count={page.locator('#count').inner_text()}")
        else:
            skip("状态筛选：已忽略", "当前数据库无 rejected 候选")

        page.locator("#filterStatus").select_option("all")
        page.wait_for_timeout(300)
        all_badges = page.locator("#candidateBody .badge").all_inner_texts()
        record("状态筛选：全部与数据库一致", len(all_badges) == len(total_items),
               f"ui={len(all_badges)}, db={len(total_items)}")

        first_pending = next((r for r in total_items if r.get("status") == "pending"), None)
        if first_pending:
            keyword = str(first_pending.get("raw_mention") or "")
            page.locator("#filterKeyword").fill(keyword)
            page.wait_for_timeout(300)
            visible = page.locator("#candidateBody tr").all_inner_texts()
            record("关键词筛选只保留匹配候选",
                   bool(visible) and all(keyword.lower() in text.lower() for text in visible),
                   f"keyword={keyword}, rows={len(visible)}")
        else:
            skip("关键词筛选", "当前数据库无 pending 候选")

        page.locator("#resetFilter").click()
        page.wait_for_timeout(300)
        record("重置筛选回到待处理", page.locator("#filterStatus").input_value() == "pending"
               and page.locator("#filterKeyword").input_value() == "",
               page.locator("#filterSummary").inner_text())

        before_llm_filter = page.locator("#candidateBody tr").count()
        page.locator("#filterLlm").select_option("none")
        page.wait_for_timeout(300)
        after_llm_filter = page.locator("#candidateBody tr").count()
        record("LLM 状态筛选控件可用", after_llm_filter <= before_llm_filter,
               f"{before_llm_filter} -> {after_llm_filter}")
        page.locator("#resetFilter").click()

        page.locator("#mineBtn").click()
        wait_toast(page, "挖掘完成", timeout=60000)
        pending_count = db_count("SELECT COUNT(*) AS n FROM skill_candidates WHERE status='pending'")
        record("重新挖掘候选按钮生效", pending_count > 0, f"pending={pending_count}")

        approve_id, reject_id = prepare_temp_candidates()
        page.reload(wait_until="networkidle")
        expect(page.locator("#count")).not_to_contain_text("加载中", timeout=15000)

        # LLM 未配置：review 应明确返回错误数，apply 应为 0
        row_app = page.locator("#candidateBody tr").filter(has_text=RAW_APPROVE)
        row_app.locator("input.candidate-check").check()
        page.locator("#reviewBtn").click()
        wait_toast(page, "LLM 判断完成", timeout=60000)
        page.wait_for_load_state("networkidle")
        llm_status = db_one(
            "SELECT llm_status, llm_suggestion_json FROM skill_candidates WHERE id=%s",
            (approve_id,),
        )
        record("LLM 未配置时 review 明确标记错误",
               llm_status and llm_status["llm_status"] == "error",
               json.dumps(llm_status, ensure_ascii=False, default=str))

        row_app = page.locator("#candidateBody tr").filter(has_text=RAW_APPROVE)
        row_app.locator("input.candidate-check").check()
        page.locator("#applyBtn").click()
        wait_toast(page, "已应用", timeout=60000)
        page.wait_for_load_state("networkidle")
        status_after_apply = db_one("SELECT status FROM skill_candidates WHERE id=%s", (approve_id,))
        record("错误建议不会被应用到正式池",
               status_after_apply and status_after_apply["status"] == "pending",
               json.dumps(status_after_apply, ensure_ascii=False, default=str))

        # 人工确认：自定义标准名称/分类必须落库并在刷新后保留
        row_app = page.locator("#candidateBody tr").filter(has_text=RAW_APPROVE)
        row_app.locator(f'input[data-name="{approve_id}"]').fill(CUSTOM_NAME)
        row_app.locator(f'input[data-cat="{approve_id}"]').fill(CUSTOM_CAT)
        row_app.get_by_role("button", name="加入词典").click()
        wait_toast(page, "正式技能池", timeout=30000)

        cand_row = db_one("SELECT * FROM skill_candidates WHERE id=%s", (approve_id,))
        cat_row = db_one("SELECT * FROM skill_catalog WHERE name=%s", (CUSTOM_NAME,))
        ok = bool(
            cand_row and cand_row.get("status") == "approved"
            and cand_row.get("manual_name") == CUSTOM_NAME
            and cand_row.get("manual_category") == CUSTOM_CAT
            and cat_row and cat_row.get("name") == CUSTOM_NAME
            and cat_row.get("category") == CUSTOM_CAT
        )
        record("人工加入词典写入 MySQL 并保存自定义名称分类", ok,
               f"candidate={json.dumps(cand_row, ensure_ascii=False, default=str)[:500]}, "
               f"catalog={json.dumps(cat_row, ensure_ascii=False, default=str)[:500]}")

        page.reload(wait_until="networkidle")
        expect(page.locator("#count")).not_to_contain_text("加载中", timeout=15000)
        page.locator("#filterStatus").select_option("approved")
        page.wait_for_timeout(300)
        row_after = page.locator("#candidateBody tr").filter(has_text=RAW_APPROVE)
        name_value = row_after.locator(f'input[data-name="{approve_id}"]').input_value()
        cat_value = row_after.locator(f'input[data-cat="{approve_id}"]').input_value()
        record("刷新后自定义名称分类保留", name_value == CUSTOM_NAME and cat_value == CUSTOM_CAT,
               f"name={name_value}, category={cat_value}")

        # 忽略：状态写 rejected
        page.locator("#filterStatus").select_option("pending")
        page.wait_for_timeout(300)
        row_rej = page.locator("#candidateBody tr").filter(has_text=RAW_REJECT)
        row_rej.get_by_role("button", name="忽略").click()
        page.wait_for_timeout(1200)
        rej_row = db_one("SELECT status FROM skill_candidates WHERE id=%s", (reject_id,))
        record("忽略候选写入 rejected 状态", rej_row and rej_row["status"] == "rejected",
               json.dumps(rej_row, ensure_ascii=False, default=str))

        save_shot(page, "05_candidates")
        page_errors_candidates = list(console_errors)
        browser.close()

        record("首页无 console/page error", not page_errors_index, " | ".join(page_errors_index[:10]))
        record("技能页无 console/page error", not page_errors_skills, " | ".join(page_errors_skills[:10]))
        record("候选页无 console/page error", not page_errors_candidates, " | ".join(page_errors_candidates[:10]))

    cleanup_temp_data()

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "base_url": BASE,
        "passed": sum(1 for r in results if r["ok"] is True),
        "failed": sum(1 for r in results if r["ok"] is False),
        "skipped": sum(1 for r in results if r["ok"] is None),
        "results": results,
    }
    (OUT_DIR / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "passed": report["passed"], "failed": report["failed"], "skipped": report["skipped"],
        "report": str(OUT_DIR / "report.json"),
    }, ensure_ascii=False))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
