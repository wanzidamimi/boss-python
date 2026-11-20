"""任务进度旁路存储：job_api / worker 写 JSON，Web 端轮询读取。

设计目的：不改动采集架构（仍是队列 + 子进程），只把运行中的进度写到
data/progress/task_<id>.json，前端弹窗轮询该文件对应的接口即可看到实时进度。
原子替换写入，避免前端读到半截 JSON。
"""

import json
import os
from datetime import datetime
from pathlib import Path

from config import DATA_DIR

PROGRESS_DIR = DATA_DIR / "progress"
PROGRESS_DIR.mkdir(parents=True, exist_ok=True)


def progress_path(task_id: str, base_dir: Path | None = None) -> Path:
    return (base_dir or PROGRESS_DIR) / f"task_{task_id}.json"


def read_progress(task_id: str, base_dir: Path | None = None) -> dict | None:
    if not task_id:
        return None
    path = progress_path(task_id, base_dir)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_progress(task_id: str, base_dir: Path | None = None, **fields) -> dict:
    """合并写入进度字段（保留已有字段），原子替换。"""
    if not task_id:
        return {}
    path = progress_path(task_id, base_dir)
    data = read_progress(task_id, base_dir) or {}
    data.update(fields)
    data["task_id"] = task_id
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    os.replace(tmp, path)
    return data


def append_event(data: dict, message: str, limit: int = 15) -> list[str]:
    """把一条事件追加到进度数据的 events 列表（保留最近 N 条）。"""
    events = list(data.get("events") or [])
    events.append(message)
    return events[-limit:]


def cleanup_progress(days: int = 7, base_dir: Path | None = None) -> int:
    """清理超过 N 天的进度文件，返回删除数量。"""
    cutoff = datetime.now().timestamp() - days * 86400
    removed = 0
    for path in (base_dir or PROGRESS_DIR).glob("task_*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed

