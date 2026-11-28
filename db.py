import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pymysql
from pymysql.cursors import DictCursor

from config import load_env
from detail_hash import decide_action

load_env()

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


DETAIL_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS job_details (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  lid               VARCHAR(255) NOT NULL COMMENT '关联 jobs.lid',
  task_id           VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '产生本次快照的任务',
  content_hash      CHAR(64)     NOT NULL DEFAULT '' COMMENT '业务字段规范化 JSON 的 sha256',
  job_name          VARCHAR(255) NOT NULL DEFAULT '',
  position_name     VARCHAR(255) NOT NULL DEFAULT '',
  salary_desc       VARCHAR(100) NOT NULL DEFAULT '',
  experience_name   VARCHAR(100) NOT NULL DEFAULT '',
  degree_name       VARCHAR(100) NOT NULL DEFAULT '',
  location_name     VARCHAR(100) NOT NULL DEFAULT '',
  address           VARCHAR(500) NOT NULL DEFAULT '',
  longitude         DECIMAL(10,6) NULL,
  latitude          DECIMAL(10,6) NULL,
  static_map_url    VARCHAR(500) NOT NULL DEFAULT '',
  skills            TEXT NULL,
  job_status_desc   VARCHAR(64) NOT NULL DEFAULT '',
  job_valid         TINYINT(1) NOT NULL DEFAULT 1,
  anonymous         TINYINT(1) NOT NULL DEFAULT 0,
  post_description  MEDIUMTEXT NULL,
  boss_name         VARCHAR(100) NOT NULL DEFAULT '',
  boss_title        VARCHAR(100) NOT NULL DEFAULT '',
  boss_avatar       VARCHAR(500) NOT NULL DEFAULT '',
  boss_active_desc  VARCHAR(64) NOT NULL DEFAULT '' COMMENT '易变, 不参与哈希',
  boss_online       TINYINT(1) NOT NULL DEFAULT 0,
  boss_certificated TINYINT(1) NOT NULL DEFAULT 0,
  brand_name        VARCHAR(255) NOT NULL DEFAULT '',
  brand_logo        VARCHAR(500) NOT NULL DEFAULT '',
  brand_stage       VARCHAR(64) NOT NULL DEFAULT '',
  brand_scale       VARCHAR(64) NOT NULL DEFAULT '',
  brand_industry    VARCHAR(255) NOT NULL DEFAULT '',
  brand_introduce   MEDIUMTEXT NULL,
  brand_labels      TEXT NULL,
  brand_active_at   DATETIME NULL COMMENT '易变, 不参与哈希',
  encrypt_brand_id  VARCHAR(128) NOT NULL DEFAULT '',
  encrypt_job_id    VARCHAR(255) NOT NULL DEFAULT '' COMMENT '跨会话稳定的职位ID, 去重用',
  security_id       VARCHAR(512) NOT NULL DEFAULT '',
  raw_json          LONGTEXT NULL COMMENT '完整 zpData',
  first_captured_at DATETIME(3) NOT NULL COMMENT '该版本首次采集时间',
  captured_at       DATETIME(3) NOT NULL COMMENT '最近一次观测时间',
  capture_count     INT NOT NULL DEFAULT 1 COMMENT '该版本被观测次数',
  created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_lid_captured (lid, captured_at),
  KEY idx_lid_hash (lid, content_hash),
  KEY idx_detail_task (task_id),
  KEY idx_detail_brand (brand_name),
  KEY idx_detail_encrypt (encrypt_job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='职位详情版本快照；同 lid 内容变化才新增行'
"""

DETAIL_REQUEST_DDL = """
CREATE TABLE IF NOT EXISTS detail_requests (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  account_id   VARCHAR(32) NOT NULL DEFAULT '',
  task_id      VARCHAR(64) NOT NULL DEFAULT '',
  lid          VARCHAR(255) NOT NULL DEFAULT '',
  requested_at DATETIME NOT NULL,
  api_code     INT NOT NULL DEFAULT 0,
  ok           TINYINT(1) NOT NULL DEFAULT 0,
  note         VARCHAR(255) NOT NULL DEFAULT '',
  PRIMARY KEY (id),
  KEY idx_detail_req_at (requested_at),
  KEY idx_detail_req_account (account_id, requested_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='详情请求日志, 用于配额统计与健康观测'
"""

JOB_SKILL_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS job_skills (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  detail_id         BIGINT UNSIGNED NOT NULL COMMENT '来源详情快照 job_details.id',
  lid               VARCHAR(255) NOT NULL DEFAULT '',
  task_id           VARCHAR(64)  NOT NULL DEFAULT '',
  job_name          VARCHAR(255) NOT NULL DEFAULT '',
  skill_name        VARCHAR(128) NOT NULL COMMENT '词典标准名',
  category          VARCHAR(64)  NOT NULL DEFAULT '',
  skill_level       VARCHAR(16)  NOT NULL DEFAULT 'required' COMMENT 'required/bonus',
  evidence_sentence TEXT NULL COMMENT '命中技能的详情正文分句',
  matched_alias     VARCHAR(128) NOT NULL DEFAULT '',
  catalog_version   VARCHAR(32)  NOT NULL DEFAULT '',
  extracted_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_job_skill (detail_id, skill_name, skill_level),
  KEY idx_js_skill (skill_name, skill_level),
  KEY idx_js_job_name (job_name),
  KEY idx_js_category (category),
  KEY idx_js_lid (lid),
  KEY idx_js_task (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='职位详情正文抽取出的技能; 只用 post_description, 不用 jobs.skills'
"""

SKILL_CATALOG_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS skill_catalog (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name          VARCHAR(128) NOT NULL COMMENT '标准技能名',
  aliases_json  TEXT NOT NULL COMMENT '别名 JSON 数组',
  category      VARCHAR(64) NOT NULL DEFAULT '',
  roadmap_order FLOAT NOT NULL DEFAULT 999 COMMENT '学习顺序权重, 越小越靠前',
  enabled       TINYINT(1) NOT NULL DEFAULT 1,
  version       VARCHAR(64) NOT NULL DEFAULT '',
  source        VARCHAR(32) NOT NULL DEFAULT 'seed' COMMENT 'seed/manual/llm',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_skill_catalog_name (name),
  KEY idx_skill_catalog_category (category)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='正式技能池; 运行时唯一词典来源'
"""

SKILL_CANDIDATE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS skill_candidates (
  id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  raw_mention         VARCHAR(128) NOT NULL COMMENT 'JD 中发现的原始候选词',
  normalized_key      VARCHAR(128) NOT NULL COMMENT '归一化 key, 用于去重',
  hit_count           INT NOT NULL DEFAULT 0 COMMENT '命中职位数',
  evidence_sentence   TEXT NULL COMMENT '证据句',
  status              VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'pending/approved/rejected',
  llm_status          VARCHAR(16) NOT NULL DEFAULT 'none' COMMENT 'none/suggested/error',
  llm_suggestion_json TEXT NULL COMMENT 'LLM 结构化建议',
  source              VARCHAR(32) NOT NULL DEFAULT 'rule',
  created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_skill_candidate_key (normalized_key),
  KEY idx_skill_candidate_status (status),
  KEY idx_skill_candidate_hits (hit_count)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='候选技能池; 未命中正式词典的 JD 片段'
"""

SKILL_META_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS skill_catalog_meta (
  meta_key   VARCHAR(64) NOT NULL,
  meta_value VARCHAR(500) NOT NULL DEFAULT '',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (meta_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='正式技能池元数据: version/description'
"""

DETAIL_COLUMNS = (
    "content_hash", "job_name", "position_name", "salary_desc",
    "experience_name", "degree_name", "location_name", "address",
    "longitude", "latitude", "static_map_url", "skills",
    "job_status_desc", "job_valid", "anonymous", "post_description",
    "boss_name", "boss_title", "boss_avatar", "boss_active_desc",
    "boss_online", "boss_certificated", "brand_name", "brand_logo",
    "brand_stage", "brand_scale", "brand_industry", "brand_introduce",
    "brand_labels", "brand_active_at", "encrypt_brand_id",
    "encrypt_job_id", "security_id", "raw_json",
)


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) AS c FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
        (table,),
    )
    return cur.fetchone()["c"] > 0


def _add_column(cur, table: str, column: str, ddl: str) -> None:
    if not _table_exists(cur, table):
        # 表还不存在（全新库尚未执行 sql/init.sql），交给初始化脚本建表
        return
    cur.execute(
        "SELECT COUNT(*) AS c FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
        (table, column),
    )
    if cur.fetchone()["c"] == 0:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _add_index(cur, table: str, index: str, columns: str) -> None:
    if not _table_exists(cur, table):
        return
    cur.execute(
        "SELECT COUNT(*) AS c FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME=%s",
        (table, index),
    )
    if cur.fetchone()["c"] == 0:
        cur.execute(f"ALTER TABLE {table} ADD INDEX {index} ({columns})")


def _seed_skill_catalog_from_json(cur) -> None:
    """一次性把旧 JSON 词典迁移到 MySQL；JSON 之后不再参与运行时读取。"""
    path = Path(__file__).resolve().parent / "static" / "skill_catalog.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict) or not isinstance(data.get("skills"), list):
        return

    cur.execute("SELECT COUNT(*) AS c FROM skill_catalog")
    if cur.fetchone()["c"] > 0:
        return

    version = str(data.get("version", "unknown"))
    for item in data["skills"]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        aliases = [str(x).strip() for x in ([name] + list(item.get("aliases", []))) if str(x).strip()]
        cur.execute(
            "INSERT IGNORE INTO skill_catalog "
            "(name, aliases_json, category, roadmap_order, enabled, version, source) "
            "VALUES (%s, %s, %s, %s, 1, %s, 'seed')",
            (
                name,
                json.dumps(aliases, ensure_ascii=False),
                str(item.get("category", "")),
                float(item.get("roadmap_order", 999)),
                version,
            ),
        )

    for key, value in (
        ("version", version),
        ("description", str(data.get("description", ""))),
    ):
        cur.execute(
            "INSERT INTO skill_catalog_meta (meta_key, meta_value) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE meta_value=VALUES(meta_value)",
            (key, value),
        )


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
                "detail_blocked_until DATETIME NULL, "
                "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "KEY idx_accounts_status (status)"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
    cur.execute(DETAIL_TABLE_DDL)
    cur.execute(DETAIL_REQUEST_DDL)
    cur.execute(JOB_SKILL_TABLE_DDL)
    cur.execute(SKILL_CATALOG_TABLE_DDL)
    cur.execute(SKILL_CANDIDATE_TABLE_DDL)
    cur.execute(SKILL_META_TABLE_DDL)
    _seed_skill_catalog_from_json(cur)

    _add_column(cur, "accounts", "detail_blocked_until",
                "detail_blocked_until DATETIME NULL COMMENT '详情风控冷却截止时间'")
    _add_column(cur, "tasks", "account_id",
                "account_id VARCHAR(32) NOT NULL DEFAULT '' COMMENT '执行任务所用账号ID'")
    _add_column(cur, "tasks", "account_label",
                "account_label VARCHAR(64) NOT NULL DEFAULT '' COMMENT '执行账号备注名快照'")
    _add_column(cur, "tasks", "detail_interval",
                "detail_interval FLOAT NOT NULL DEFAULT 60 COMMENT '详情请求间隔秒数'")
    _add_column(cur, "tasks", "detail_ok",
                "detail_ok INT NOT NULL DEFAULT 0 COMMENT '详情成功条数'")
    _add_column(cur, "tasks", "detail_fail",
                "detail_fail INT NOT NULL DEFAULT 0 COMMENT '详情失败条数'")
    _add_column(cur, "tasks", "detail_status",
                "detail_status VARCHAR(16) NOT NULL DEFAULT 'none' "
                "COMMENT 'none/partial/full/blocked'")
    _add_column(cur, "job_details", "encrypt_job_id",
                "encrypt_job_id VARCHAR(255) NOT NULL DEFAULT '' "
                "COMMENT '跨会话稳定的职位ID, 去重用'")
    _add_index(cur, "job_details", "idx_detail_encrypt", "encrypt_job_id")
    # 旧详情行补齐 encrypt_job_id（通过 lid 关联 jobs）
    if _table_exists(cur, "jobs"):
        cur.execute(
            "UPDATE job_details d JOIN jobs j ON j.lid = d.lid "
            "SET d.encrypt_job_id = j.encrypt_job_id "
            "WHERE d.encrypt_job_id = '' AND j.encrypt_job_id <> ''"
        )


def create_task(task: dict):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT IGNORE INTO tasks
                  (task_id, mode, keyword, city, pages, page_size, detail_limit,
                   detail_interval, account_id, account_label, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    task["task_id"],
                    task["mode"],
                    task["keyword"],
                    task["city"],
                    task["pages"],
                    task["page_size"],
                    task["detail_limit"],
                    task.get("detail_interval", 60),
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


def update_task_detail(task_id, detail_ok: int, detail_fail: int,
                       detail_status: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET detail_ok=%s, detail_fail=%s, detail_status=%s "
                "WHERE task_id=%s",
                (detail_ok, detail_fail, detail_status, task_id),
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

            cur.execute("SELECT COUNT(*) AS c FROM job_details")
            details_total = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(DISTINCT lid) AS c FROM job_details")
            jobs_with_detail = cur.fetchone()["c"]
            cur.execute(
                "SELECT COUNT(*) AS c FROM detail_requests "
                "WHERE DATE(requested_at) = CURDATE()"
            )
            detail_requests_today = cur.fetchone()["c"]
            cur.execute(
                "SELECT COUNT(*) AS c FROM accounts "
                "WHERE detail_blocked_until IS NOT NULL AND detail_blocked_until > NOW()"
            )
            detail_blocked_accounts = cur.fetchone()["c"]

    return {
        "tasks_total": sum(by_status.values()),
        "tasks_queued": by_status.get("queued", 0),
        "tasks_running": by_status.get("running", 0),
        "tasks_success": by_status.get("success", 0),
        "tasks_failed": by_status.get("failed", 0),
        "jobs_total": jobs_total,
        "tasks_today": tasks_today,
        "job_tasks_today": job_tasks_today,
        "details_total": details_total,
        "jobs_with_detail": jobs_with_detail,
        "detail_requests_today": detail_requests_today,
        "detail_blocked_accounts": detail_blocked_accounts,
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


DETAIL_LATEST_JOIN = """
LEFT JOIN job_details d ON d.lid = j.lid AND d.captured_at = (
    SELECT MAX(d2.captured_at) FROM job_details d2 WHERE d2.lid = j.lid
)
"""

DETAIL_EXPORT_SELECT = """
d.post_description AS detail_post_description,
d.address AS detail_address,
d.experience_name AS detail_experience,
d.degree_name AS detail_degree,
d.salary_desc AS detail_salary,
d.skills AS detail_skills,
d.job_status_desc AS detail_job_status,
d.brand_introduce AS detail_brand_introduce,
d.brand_labels AS detail_brand_labels,
d.boss_active_desc AS detail_boss_active,
d.captured_at AS detail_captured_at
"""


def _job_where(task_id="", keyword="", city=""):
    where = ["1=1"]
    params = []
    if task_id:
        where.append("j.task_id=%s")
        params.append(task_id)
    if keyword:
        where.append("(j.job_name LIKE %s OR j.brand_name LIKE %s)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if city:
        where.append("j.city_name LIKE %s")
        params.append(f"%{city}%")
    return where, params


def query_jobs(task_id="", keyword="", city="", limit=50, offset=0,
               with_detail=False) -> list[dict]:
    where, params = _job_where(task_id, keyword, city)
    select = "j.*"
    join = ""
    if with_detail:
        select += (", d.id AS detail_id, d.captured_at AS detail_captured_at, "
                   "d.content_hash AS detail_hash")
        join = DETAIL_LATEST_JOIN
    params.extend([limit, offset])
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {select}
                FROM jobs j
                {join}
                WHERE {' AND '.join(where)}
                ORDER BY j.id DESC
                LIMIT %s OFFSET %s
                """,
                params,
            )
            return cur.fetchall()


def query_all_jobs(task_id="", keyword="", city="", with_detail=False) -> list[dict]:
    where, params = _job_where(task_id, keyword, city)
    select = "j.*"
    join = ""
    if with_detail:
        select += ", " + DETAIL_EXPORT_SELECT
        join = DETAIL_LATEST_JOIN
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {select}
                FROM jobs j
                {join}
                WHERE {' AND '.join(where)}
                ORDER BY j.id DESC
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


# ---------------------------------------------------------------------------
# 职位详情版本（job_details）与详情请求日志（detail_requests）
# ---------------------------------------------------------------------------

def _insert_detail_row(cur, task_id: str, item: dict, captured_at: datetime) -> str:
    """插入新版本；若同一 lid 在同一毫秒已有行，顺延 1ms 重试。"""
    columns = ("lid", "task_id") + DETAIL_COLUMNS + (
        "first_captured_at", "captured_at", "capture_count",
    )
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f"INSERT INTO job_details ({', '.join(columns)}) VALUES ({placeholders})"
    for _ in range(5):
        values = [item["lid"], task_id]
        values.extend(item.get(col, "") for col in DETAIL_COLUMNS)
        values.extend([captured_at, captured_at, 1])
        try:
            cur.execute(sql, values)
            return "inserted"
        except pymysql.err.IntegrityError:
            captured_at = captured_at + timedelta(milliseconds=1)
    raise RuntimeError("job_details 主键冲突重试失败")


def insert_detail_snapshot(task_id: str, item: dict,
                           captured_at: datetime | None = None) -> str:
    """按"内容是否变化"写入详情快照。

    与同 lid 的最新版本比较 content_hash：
    - 一致：不新增行，只刷新易变字段、captured_at 与 capture_count
    - 不一致或无历史：新增一行（first_captured_at = captured_at）
    返回 "inserted" 或 "updated"。
    """
    captured_at = captured_at or datetime.now()
    lid = item["lid"]
    with get_conn() as conn:
        with conn.cursor() as cur:
            conn.begin()
            cur.execute(
                "SELECT id, content_hash, captured_at FROM job_details "
                "WHERE lid=%s ORDER BY captured_at DESC, id DESC LIMIT 1 FOR UPDATE",
                (lid,),
            )
            latest = cur.fetchone()
            action = decide_action(latest, item.get("content_hash", ""))
            result = "updated"
            if action == "update":
                # 保证观测时间单调递增（DATETIME(3) 精度下可能撞同一毫秒）
                if latest.get("captured_at") and captured_at <= latest["captured_at"]:
                    captured_at = latest["captured_at"] + timedelta(milliseconds=1)
                update_fields = [c for c in DETAIL_COLUMNS if c != "content_hash"]
                sets = ", ".join(f"{col}=%s" for col in update_fields)
                values = [task_id]
                values.extend(item.get(col, "") for col in update_fields)
                values.extend([captured_at, latest["id"]])
                cur.execute(
                    f"UPDATE job_details SET task_id=%s, {sets}, "
                    "captured_at=%s, capture_count=capture_count+1 WHERE id=%s",
                    values,
                )
            else:
                _insert_detail_row(cur, task_id, item, captured_at)
                result = "inserted"
            conn.commit()
    return result


def get_latest_detail(lid: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM job_details WHERE lid=%s "
                "ORDER BY captured_at DESC, id DESC LIMIT 1",
                (lid,),
            )
            return cur.fetchone()


def get_detail_by_id(detail_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM job_details WHERE id=%s", (detail_id,))
            return cur.fetchone()


def count_detail_versions(lid: str) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM job_details WHERE lid=%s", (lid,))
            return cur.fetchone()["c"]


def has_detail_by_encrypt_id(encrypt_job_id: str) -> bool:
    """该职位（跨任务稳定 ID）是否已有详情记录。"""
    if not encrypt_job_id:
        return False
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 AS x FROM job_details WHERE encrypt_job_id=%s LIMIT 1",
                (encrypt_job_id,),
            )
            return cur.fetchone() is not None


def list_detail_versions(lid: str) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, content_hash, salary_desc, experience_name, degree_name, "
                "first_captured_at, captured_at, capture_count "
                "FROM job_details WHERE lid=%s ORDER BY captured_at DESC, id DESC",
                (lid,),
            )
            return cur.fetchall()


def log_detail_requests(rows: list[dict]) -> None:
    """批量写入详情请求日志（成功失败都记，用于配额统计）。"""
    if not rows:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO detail_requests "
                "(account_id, task_id, lid, requested_at, api_code, ok, note) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        row.get("account_id", ""),
                        row.get("task_id", ""),
                        row.get("lid", ""),
                        row.get("requested_at") or datetime.now(),
                        int(row.get("api_code", 0)),
                        1 if row.get("ok") else 0,
                        str(row.get("note", ""))[:255],
                    )
                    for row in rows
                ],
            )


def count_recent_detail_requests(account_id: str, minutes: int = 10) -> int:
    cutoff = datetime.now() - timedelta(minutes=minutes)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM detail_requests "
                "WHERE account_id=%s AND requested_at > %s",
                (account_id, cutoff),
            )
            return cur.fetchone()["c"]


def set_account_detail_block(account_id: str, until: datetime) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE accounts SET detail_blocked_until=%s WHERE account_id=%s",
                (until, account_id),
            )


def get_account_detail_block(account_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT detail_blocked_until FROM accounts WHERE account_id=%s",
                (account_id,),
            )
            row = cur.fetchone()
            return row["detail_blocked_until"] if row else None


def is_account_detail_blocked(account_id: str) -> bool:
    until = get_account_detail_block(account_id)
    return bool(until and until > datetime.now())


def select_jobs_for_detail(limit: int = 50, task_id: str = "",
                           force: bool = False) -> list[dict]:
    """挑选可回填详情的职位：有 securityId，且（可选）还没有详情版本。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT j.id, j.task_id, j.lid, j.security_id, j.job_name,
                       j.brand_name, j.encrypt_job_id
                FROM jobs j
                WHERE j.security_id <> ''
                  AND (%s = '' OR j.task_id = %s)
                  AND (%s = 1 OR (
                      NOT EXISTS (SELECT 1 FROM job_details d WHERE d.lid = j.lid)
                      AND NOT EXISTS (
                          SELECT 1 FROM job_details d2
                          WHERE d2.encrypt_job_id = j.encrypt_job_id
                      )
                  ))
                ORDER BY j.id DESC
                LIMIT %s
                """,
                (task_id, task_id, 1 if force else 0, limit),
            )
            return cur.fetchall()


def count_jobs_without_detail() -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM jobs j WHERE j.security_id <> '' "
                "AND NOT EXISTS (SELECT 1 FROM job_details d WHERE d.lid = j.lid) "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM job_details d2 "
                "  WHERE d2.encrypt_job_id = j.encrypt_job_id"
                ")"
            )
            return cur.fetchone()["c"]


# ---------------------------------------------------------------------------
# 职位技能图谱：从 job_details.post_description 派生，不读 jobs.skills
# ---------------------------------------------------------------------------
def list_latest_details_with_description() -> list[dict]:
    """每个 lid 只取最新一个含详情正文的快照；lid 为空时按 id 取最新。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.id, d.lid, d.task_id, d.job_name, d.post_description
                FROM job_details d
                WHERE d.post_description IS NOT NULL AND d.post_description <> ''
                  AND (
                    (d.lid <> '' AND NOT EXISTS (
                        SELECT 1 FROM job_details d2
                        WHERE d2.lid = d.lid
                          AND (d2.captured_at > d.captured_at
                               OR (d2.captured_at = d.captured_at AND d2.id > d.id))
                    ))
                    OR (d.lid = '' AND NOT EXISTS (
                        SELECT 1 FROM job_details d2
                        WHERE d2.lid = '' AND d2.id > d.id
                    ))
                  )
                ORDER BY d.lid, d.id
                """
            )
            return cur.fetchall()


def replace_job_skills(detail_id: int, rows: list[dict]) -> int:
    """覆盖某个详情快照的抽取结果；rows 为空时等价于删除旧结果。"""
    columns = (
        "detail_id", "lid", "task_id", "job_name", "skill_name",
        "category", "skill_level", "evidence_sentence", "matched_alias",
        "catalog_version",
    )
    values = [
        tuple(row.get(col, "" if col != "detail_id" else detail_id) for col in columns)
        for row in rows
    ]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM job_skills WHERE detail_id=%s", (detail_id,))
            if values:
                placeholders = ", ".join(["%s"] * len(columns))
                cur.executemany(
                    f"INSERT IGNORE INTO job_skills ({', '.join(columns)}) "
                    f"VALUES ({placeholders})",
                    values,
                )
            return len(values)


def clear_job_skills() -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM job_skills")


def query_job_skills(keyword: str = "", task_id: str = "") -> list[dict]:
    """按职位名或采集关键词筛选技能行；keyword 为空表示全部。"""
    where = ["1=1"]
    params: list = []
    if keyword:
        like = f"%{keyword}%"
        where.append("(js.job_name LIKE %s OR t.keyword LIKE %s)")
        params.extend([like, like])
    if task_id:
        where.append("js.task_id = %s")
        params.append(task_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT js.*, t.keyword AS task_keyword FROM job_skills js "
                "LEFT JOIN tasks t ON t.task_id = js.task_id "
                f"WHERE {' AND '.join(where)} "
                "ORDER BY js.job_name, js.detail_id, js.skill_level, js.skill_name",
                tuple(params),
            )
            return cur.fetchall()


# ---------------------------------------------------------------------------
# 正式技能池与候选池
# ---------------------------------------------------------------------------
def list_skill_catalog(enabled_only: bool = True) -> list[dict]:
    where = "WHERE enabled=1" if enabled_only else ""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, aliases_json, category, roadmap_order, enabled, "
                "version, source, created_at, updated_at "
                f"FROM skill_catalog {where} ORDER BY roadmap_order, name"
            )
            return cur.fetchall()


def get_skill_catalog_meta() -> dict[str, str]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT meta_key, meta_value FROM skill_catalog_meta")
            return {row["meta_key"]: row["meta_value"] for row in cur.fetchall()}


def set_skill_catalog_meta(key: str, value: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO skill_catalog_meta (meta_key, meta_value) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE meta_value=VALUES(meta_value)",
                (key, str(value)),
            )


def add_skill_catalog_item(name: str, aliases: list[str] | None, category: str,
                           roadmap_order: float = 999, source: str = "manual") -> bool:
    """新增正式技能；已存在同名技能返回 False。"""
    name = str(name or "").strip()
    if not name:
        return False
    alias_list = []
    for raw in [name] + list(aliases or []):
        value = str(raw or "").strip()
        if value and value not in alias_list:
            alias_list.append(value)
    version = datetime.now().strftime("cat-%Y%m%d-%H%M%S")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT IGNORE INTO skill_catalog "
                "(name, aliases_json, category, roadmap_order, enabled, version, source) "
                "VALUES (%s, %s, %s, %s, 1, %s, %s)",
                (name, json.dumps(alias_list, ensure_ascii=False), str(category or ""),
                 float(roadmap_order), version, str(source or "manual")),
            )
            added = cur.rowcount > 0
            if added:
                set_skill_catalog_meta("version", version)
            return added


def list_skill_candidates(status: str = "", limit: int = 200) -> list[dict]:
    where, params = "", []
    if status:
        where = "WHERE status=%s"
        params.append(status)
    params.append(limit)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM skill_candidates "
                f"{where} ORDER BY hit_count DESC, id ASC LIMIT %s",
                tuple(params),
            )
            return cur.fetchall()


def get_skill_candidate(candidate_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM skill_candidates WHERE id=%s", (candidate_id,))
            return cur.fetchone()


def reset_pending_skill_candidates() -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM skill_candidates WHERE status='pending'")


def upsert_skill_candidates(rows: list[dict]) -> None:
    columns = (
        "raw_mention", "normalized_key", "hit_count", "evidence_sentence",
        "status", "llm_status", "source",
    )
    values = [
        tuple(row.get(col, "pending" if col == "status" else
                       "none" if col == "llm_status" else
                       0 if col == "hit_count" else "") for col in columns)
        for row in rows
    ]
    if not values:
        return
    placeholders = ", ".join(["%s"] * len(columns))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO skill_candidates ({', '.join(columns)}) VALUES ({placeholders}) "
                "ON DUPLICATE KEY UPDATE "
                "hit_count=IF(status='pending', VALUES(hit_count), hit_count), "
                "evidence_sentence=IF(status='pending', VALUES(evidence_sentence), evidence_sentence), "
                "source=IF(status='pending', VALUES(source), source)",
                values,
            )


def update_skill_candidate(candidate_id: int, **fields) -> None:
    allowed = {"status", "llm_status", "llm_suggestion_json"}
    updates, values = [], []
    for key, value in fields.items():
        if key in allowed:
            updates.append(f"{key}=%s")
            values.append(value)
    if not updates:
        return
    values.append(candidate_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE skill_candidates SET {', '.join(updates)} WHERE id=%s",
                tuple(values),
            )

