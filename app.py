import csv
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from openpyxl import Workbook
from pydantic import BaseModel

import db

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
EXPORT_DIR = DATA_DIR / "exports"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

task_queue = queue.Queue()
last_task_started = 0.0
queue_lock = threading.Lock()
MIN_TASK_INTERVAL = float(os.getenv("BOSS_MIN_TASK_INTERVAL", "300"))
# 浏览器刷新后产出的 stoken 即可用，等待时间主要用于让服务端完成状态同步
STOKEN_WAIT = float(os.getenv("BOSS_STOKEN_WAIT", "60"))

app = FastAPI(title="Boss 招聘采集服务")


class TaskRequest(BaseModel):
    mode: str = "recommend"
    keyword: str = ""
    city: str = ""
    pages: int = 1
    page_size: int = 15
    detail_limit: int = 0
    delay: float = 3.0


def run_cmd(args, timeout):
    result = subprocess.run(
        [sys.executable] + args,
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "unknown error")[-3000:])
    return result.stdout


def worker():
    global last_task_started
    while True:
        task = task_queue.get()
        task_id = task["task_id"]
        try:
            with queue_lock:
                elapsed = time.time() - last_task_started
                if elapsed < MIN_TASK_INTERVAL:
                    time.sleep(MIN_TASK_INTERVAL - elapsed)
                last_task_started = time.time()

            db.update_task_status(task_id, "running", "refreshing stoken")
            run_cmd(["stoken_browser.py"], timeout=180)

            if STOKEN_WAIT > 0:
                time.sleep(STOKEN_WAIT)

            db.update_task_status(task_id, "running", "calling boss api")
            prefix = f"task_{task_id}"
            cmd = [
                "job_api.py",
                "--mode", task["mode"],
                "--pages", str(task["pages"]),
                "--page-size", str(task["page_size"]),
                "--detail-limit", str(task["detail_limit"]),
                "--delay", str(task["delay"]),
                "--prefix", prefix,
            ]
            if task["mode"] == "search":
                cmd.extend(["--keyword", task["keyword"]])
            if task["city"]:
                cmd.extend(["--city", task["city"]])

            run_cmd(cmd, timeout=1800)

            candidates = sorted(
                DATA_DIR.glob(f"{prefix}_*.json"),
                key=lambda p: p.stat().st_mtime,
            )
            if not candidates:
                raise RuntimeError("job_api.py produced no JSON output")

            rows = json.loads(candidates[-1].read_text(encoding="utf-8"))
            inserted = db.insert_jobs(task_id, rows)
            db.update_task_status(
                task_id,
                "success",
                f"saved {inserted} jobs",
                rows_count=inserted,
                finished=True,
            )
        except Exception as exc:
            db.update_task_status(
                task_id,
                "failed",
                str(exc)[:1000],
                finished=True,
            )
        finally:
            task_queue.task_done()


@app.on_event("startup")
def startup_event():
    db.init_db()
    threading.Thread(target=worker, daemon=True).start()


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/cities")
def cities():
    """返回 BOSS 城市中文名 -> 9 位编码码表，供前端城市选择使用。"""
    path = STATIC_DIR / "city_codes.json"
    try:
        return JSONResponse(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"city codes load error: {exc}")


@app.post("/api/tasks")
def create_task(req: TaskRequest):
    if req.mode not in ("recommend", "search"):
        raise HTTPException(status_code=400, detail="mode must be recommend or search")
    if req.mode == "search" and not req.keyword.strip():
        raise HTTPException(status_code=400, detail="search mode requires keyword")
    if req.pages < 1 or req.pages > 10:
        raise HTTPException(status_code=400, detail="pages must be 1-10")
    if req.detail_limit < 0 or req.detail_limit > 3:
        raise HTTPException(status_code=400, detail="detail_limit must be 0-3")
    if req.delay < 2:
        raise HTTPException(status_code=400, detail="delay must be >= 2 seconds")

    task_id = uuid.uuid4().hex[:12]
    task = {
        "task_id": task_id,
        "mode": req.mode,
        "keyword": req.keyword.strip(),
        "city": req.city.strip(),
        "pages": req.pages,
        "page_size": req.page_size,
        "detail_limit": req.detail_limit,
        "delay": req.delay,
    }
    try:
        db.create_task(task)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"database error: {exc}")
    task_queue.put(task)
    return {"task_id": task_id, "status": "queued"}


@app.get("/api/tasks")
def tasks():
    try:
        return db.list_tasks(50)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str):
    try:
        task = db.get_task(task_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@app.get("/api/stats")
def stats():
    try:
        return db.collect_stats()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/jobs")
def jobs(task_id: str = "", keyword: str = "", city: str = "", limit: int = 50):
    try:
        return db.query_jobs(task_id, keyword, city, min(limit, 200), 0)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/export")
def export(format: str = "csv", task_id: str = "", keyword: str = "", city: str = ""):
    try:
        rows = db.query_all_jobs(task_id, keyword, city)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    token = uuid.uuid4().hex[:8]
    if format == "csv":
        path = EXPORT_DIR / f"boss_{token}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
            writer.writeheader()
            writer.writerows(rows)
        return FileResponse(path, filename=path.name, media_type="text/csv")

    if format == "excel":
        path = EXPORT_DIR / f"boss_{token}.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "jobs"
        if rows:
            headers = list(rows[0].keys())
            ws.append(headers)
            for row in rows:
                ws.append([row.get(h, "") for h in headers])
        wb.save(path)
        return FileResponse(
            path,
            filename=path.name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    return JSONResponse(status_code=400, content={"detail": "format must be csv or excel"})
