import os
from contextlib import contextmanager
from datetime import datetime

import pymysql
from pymysql.cursors import DictCursor

DB_CONFIG = {
    "host": os.getenv("BOSS_DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("BOSS_DB_PORT", "3307")),
    "user": os.getenv("BOSS_DB_USER", "boss"),
    "password": os.getenv("BOSS_DB_PASSWORD", ""),
    "database": os.getenv("BOSS_DB_NAME", "boss_recruit"),
    "charset": "utf8mb4",
    "init_command": "SET NAMES utf8mb4",
    "autocommit": True,
}

JOB_COLUMNS = {
    "jobName": "job_name",
    "salaryDesc": "salary_desc",
    "jobExperience": "job_experience",
    "jobDegree": "job_degree",
    "cityName": "city_name",
    "areaDistrict": "area_district",
    "businessDistrict": "business_district",
    "brandName": "brand_name",
    "brandIndustry": "brand_industry",
    "brandScaleName": "brand_scale_name",
    "bossName": "boss_name",
    "bossTitle": "boss_title",
    "skills": "skills",
    "jobLabels": "job_labels",
    "postDescription": "post_description",
    "address": "address",
    "encryptJobId": "encrypt_job_id",
    "lid": "lid",
    "securityId": "security_id",
    "detailUrl": "detail_url",
}


@contextmanager
def get_conn():
    conn = pymysql.connect(**DB_CONFIG, cursorclass=DictCursor)
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            _ensure_schema(cur)


def _ensure_schema(cur):
    """对已存在的旧库做幂等升级：缺表建表、缺列补列。"""
    cur.execute("CREATE TABLE IF NOT EXISTS accounts ("
                "account_id VARCHAR(32) NOT NULL PRIMARY KEY, "
                "label VARCHAR(64) NOT NULL, "
                "boss_user_id VARCHAR(64) NOT NULL DEFAULT '', "
                "cdp_port INT NOT NULL, "
                "status VARCHAR(16) NOT NULL DEFAULT 'expired', "
                "last_probe_at DATETIME NULL, "
                "last_used_at DATETIME NULL, "
                "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "KEY idx_accounts_status (status)"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
    cur.execute("SELECT COUNT(*) AS c FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='tasks' AND COLUMN_NAME='account_id'")
    if cur.fetchone()["c"] == 0:
        cur.execute("ALTER TABLE tasks ADD COLUMN account_id VARCHAR(32) NOT NULL DEFAULT '' "
                    "COMMENT '执行任务所用账号ID'")
    cur.execute("SELECT COUNT(*) AS c FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='tasks' AND COLUMN_NAME='account_label'")
    if cur.fetchone()["c"] == 0:
        cur.execute("ALTER TABLE tasks ADD COLUMN account_label VARCHAR(64) NOT NULL DEFAULT '' "
                    "COMMENT '执行账号备注名快照'")


def create_task(task: dict):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT IGNORE INTO tasks
                  (task_id, mode, keyword, city, pages, page_size, detail_limit,
                   account_id, account_label, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    task["task_id"],
                    task["mode"],
                    task["keyword"],
                    task["city"],
                    task["pages"],
                    task["page_size"],
                    task["detail_limit"],
                    task.get("account_id", ""),
                    task.get("account_label", ""),
                    "queued",
                ),
            )


def update_task_status(task_id, status, message="", rows_count=None, finished=False):
    fields = ["status=%s", "message=%s"]
    values = [status, message]
    if rows_count is not None:
        fields.append("rows_count=%s")
        values.append(rows_count)
    if finished:
        fields.append("finished_at=%s")
        values.append(datetime.now())
    values.append(task_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE tasks SET {', '.join(fields)} WHERE task_id=%s",
                values,
            )


def insert_jobs(task_id: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    columns = ["task_id"] + [JOB_COLUMNS[key] for key in JOB_COLUMNS]
    sql = (
        f"INSERT IGNORE INTO jobs ({', '.join(columns)}) VALUES "
        f"({', '.join(['%s'] * len(columns))})"
    )
    payload = []
    for row in rows:
        payload.append(
            [task_id] + [str(row.get(key, "") or "") for key in JOB_COLUMNS]
        )
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, payload)
    return len(payload)


def insert_health_check(
    probe_at,
    api_code: int,
    api_message: str,
    token_age_min: float,
    token_len: int,
    refreshed: bool,
    ok: bool,
    note: str = "",
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO health_checks
                  (probe_at, api_code, api_message, token_age_min,
                   token_len, refreshed, ok, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    probe_at,
                    api_code,
                    api_message,
                    token_age_min,
                    token_len,
                    1 if refreshed else 0,
                    1 if ok else 0,
                    note,
                ),
            )


def list_health_checks(limit: int = 200) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM health_checks ORDER BY id DESC LIMIT %s", (limit,)
            )
            return cur.fetchall()


def collect_stats() -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, COUNT(*) AS c FROM tasks GROUP BY status
                """
            )
            by_status = {row["status"]: row["c"] for row in cur.fetchall()}

            cur.execute("SELECT COUNT(*) AS c FROM jobs")
            jobs_total = cur.fetchone()["c"]

            cur.execute(
                """
                SELECT COUNT(*) AS c FROM tasks
                WHERE DATE(created_at) = CURDATE()
                """
            )
            tasks_today = cur.fetchone()["c"]

            cur.execute(
                """
                SELECT COUNT(DISTINCT task_id) AS c FROM jobs
                WHERE DATE(created_at) = CURDATE()
                """
            )
            job_tasks_today = cur.fetchone()["c"]

            cur.execute(
                """
                SELECT * FROM health_checks ORDER BY id DESC LIMIT 1
                """
            )
            latest_health = cur.fetchone()

    return {
        "tasks_total": sum(by_status.values()),
        "tasks_queued": by_status.get("queued", 0),
        "tasks_running": by_status.get("running", 0),
        "tasks_success": by_status.get("success", 0),
        "tasks_failed": by_status.get("failed", 0),
        "jobs_total": jobs_total,
        "tasks_today": tasks_today,
        "job_tasks_today": job_tasks_today,
        "latest_health": latest_health,
    }


def list_tasks(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM tasks ORDER BY created_at DESC LIMIT %s
                """,
                (limit,),
            )
            return cur.fetchall()


def get_task(task_id: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tasks WHERE task_id=%s", (task_id,))
            return cur.fetchone()


def query_jobs(task_id="", keyword="", city="", limit=50, offset=0) -> list[dict]:
    where = ["1=1"]
    params = []
    if task_id:
        where.append("task_id=%s")
        params.append(task_id)
    if keyword:
        where.append("(job_name LIKE %s OR brand_name LIKE %s)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if city:
        where.append("city_name LIKE %s")
        params.append(f"%{city}%")
    params.extend([limit, offset])
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM jobs
                WHERE {' AND '.join(where)}
                ORDER BY id DESC
                LIMIT %s OFFSET %s
                """,
                params,
            )
            return cur.fetchall()


def query_all_jobs(task_id="", keyword="", city="") -> list[dict]:
    where = ["1=1"]
    params = []
    if task_id:
        where.append("task_id=%s")
        params.append(task_id)
    if keyword:
        where.append("(job_name LIKE %s OR brand_name LIKE %s)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if city:
        where.append("city_name LIKE %s")
        params.append(f"%{city}%")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM jobs
                WHERE {' AND '.join(where)}
                ORDER BY id DESC
                """,
                params,
            )
            return cur.fetchall()


# ---------------------------------------------------------------------------
# 账号管理（多用户凭证的元数据；Cookie 本体存文件，不入库）
# ---------------------------------------------------------------------------

def list_accounts() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM accounts ORDER BY created_at ASC"
            )
            return cur.fetchall()


def get_account(account_id: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM accounts WHERE account_id=%s", (account_id,))
            return cur.fetchone()


def create_account(account: dict) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO accounts (account_id, label, boss_user_id, cdp_port, status) "
                "VALUES (%s, %s, %s, %s, 'expired')",
                (account["account_id"], account["label"],
                 account.get("boss_user_id", ""), account["cdp_port"]),
            )


def update_account(account_id: str, **fields) -> None:
    allowed = {"label", "boss_user_id", "cdp_port", "status",
               "last_probe_at", "last_used_at"}
    sets = [f"{k}=%s" for k in fields if k in allowed]
    if not sets:
        return
    values = [fields[k] for k in fields if k in allowed]
    values.append(account_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE accounts SET {', '.join(sets)} WHERE account_id=%s",
                values,
            )


def delete_account(account_id: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM accounts WHERE account_id=%s", (account_id,))


def pick_round_robin_account() -> dict | None:
    """轮询：在 active 账号中选最久未使用（last_used_at 最旧，NULL 优先）的一个。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM accounts WHERE status='active' "
                "ORDER BY last_used_at ASC, created_at ASC LIMIT 1"
            )
            return cur.fetchone()
