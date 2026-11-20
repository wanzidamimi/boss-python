"""职位详情字段的规范化、内容哈希与入库映射。

设计要点
--------
1. 版本判定用"业务字段整体哈希"，而不是逐字段比较：
   把影响职位语义的字段规范化后序列化成 canonical JSON，取 SHA-256。
   任何一项变化 -> 新版本；完全一致 -> 只刷新观测时间与易变字段。
2. 易变字段（活跃状态、活跃时间戳、地图/logo/头像 CDN 地址）不参与哈希，
   否则几乎每次采集都会产生一条"假新版本"。
3. 存储与哈希分离：flatten_detail 保留原始顺序（便于展示），
   哈希时才排序数组、裁剪空白，避免顺序抖动造成误判。
"""

import hashlib
import json
from datetime import datetime

# 参与哈希的字段（业务语义）。新增字段时同步修改 build_hash_payload。
HASH_FIELDS = (
    "jobName",
    "positionName",
    "salaryDesc",
    "payTypeDesc",
    "experienceName",
    "degreeName",
    "locationName",
    "address",
    "longitude",
    "latitude",
    "skills",
    "jobStatusDesc",
    "jobValid",
    "anonymous",
    "postDescription",
    "bossName",
    "bossTitle",
    "bossCertificated",
    "brandName",
    "brandStage",
    "brandScale",
    "brandIndustry",
    "brandIntroduce",
    "brandLabels",
)


def norm_text(value) -> str:
    """统一换行、去掉每行行尾空白与首尾空白。"""
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def norm_list(value) -> list[str]:
    """规范化字符串数组：裁剪、去空、排序（用于哈希，不用于展示）。"""
    if not value:
        return []
    items = [norm_text(item) for item in value]
    return sorted(item for item in items if item)


def norm_float(value):
    """浮点统一到 6 位小数；无法解析时返回 None。"""
    if value is None or value == "":
        return None
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def _flag(value) -> int:
    return 1 if value else 0


def build_hash_payload(zp_data: dict) -> dict:
    """提取参与哈希的业务字段，缺失一律按空处理。"""
    job = (zp_data or {}).get("jobInfo") or {}
    boss = (zp_data or {}).get("bossInfo") or {}
    brand = (zp_data or {}).get("brandComInfo") or {}
    return {
        "jobName": norm_text(job.get("jobName")),
        "positionName": norm_text(job.get("positionName")),
        "salaryDesc": norm_text(job.get("salaryDesc")),
        "payTypeDesc": norm_text(job.get("payTypeDesc")),
        "experienceName": norm_text(job.get("experienceName")),
        "degreeName": norm_text(job.get("degreeName")),
        "locationName": norm_text(job.get("locationName")),
        "address": norm_text(job.get("address")),
        "longitude": norm_float(job.get("longitude")),
        "latitude": norm_float(job.get("latitude")),
        "skills": norm_list(job.get("showSkills")),
        "jobStatusDesc": norm_text(job.get("jobStatusDesc")),
        "jobValid": 0 if job.get("invalidStatus") else 1,
        "anonymous": _flag(job.get("anonymous")),
        "postDescription": norm_text(job.get("postDescription")),
        "bossName": norm_text(boss.get("name")),
        "bossTitle": norm_text(boss.get("title")),
        "bossCertificated": _flag(boss.get("certificated")),
        "brandName": norm_text(brand.get("brandName")),
        "brandStage": norm_text(brand.get("stageName")),
        "brandScale": norm_text(brand.get("scaleName")),
        "brandIndustry": norm_text(brand.get("industryName")),
        "brandIntroduce": norm_text(brand.get("introduce")),
        "brandLabels": norm_list(brand.get("labels")),
    }


def content_hash(zp_data: dict) -> str:
    """业务字段 canonical JSON 的 SHA-256（64 位十六进制）。"""
    payload = build_hash_payload(zp_data)
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _join_list(value) -> str:
    """展示用：保留原顺序，裁剪空白，去空值。"""
    if not value:
        return ""
    items = []
    for item in value:
        text = norm_text(item)
        if text:
            items.append(text)
    return "|".join(items)


def _brand_active_at(brand: dict):
    """把品牌活跃时间戳（毫秒）转成 datetime。"""
    raw = brand.get("activeTime")
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(int(raw) / 1000)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def flatten_detail(zp_data: dict, lid: str, security_id: str = "") -> dict:
    """把 detail.json 的 zpData 映射成 job_details 的一行。"""
    zp_data = zp_data or {}
    job = zp_data.get("jobInfo") or {}
    boss = zp_data.get("bossInfo") or {}
    brand = zp_data.get("brandComInfo") or {}
    return {
        "lid": lid,
        "content_hash": content_hash(zp_data),
        "job_name": norm_text(job.get("jobName")),
        "position_name": norm_text(job.get("positionName")),
        "salary_desc": norm_text(job.get("salaryDesc")),
        "experience_name": norm_text(job.get("experienceName")),
        "degree_name": norm_text(job.get("degreeName")),
        "location_name": norm_text(job.get("locationName")),
        "address": norm_text(job.get("address")),
        "longitude": norm_float(job.get("longitude")),
        "latitude": norm_float(job.get("latitude")),
        "static_map_url": norm_text(
            job.get("pcStaticMapUrl") or job.get("staticMapUrl")
        ),
        "skills": _join_list(job.get("showSkills")),
        "job_status_desc": norm_text(job.get("jobStatusDesc")),
        "job_valid": 0 if job.get("invalidStatus") else 1,
        "anonymous": _flag(job.get("anonymous")),
        "post_description": norm_text(job.get("postDescription")),
        "boss_name": norm_text(boss.get("name")),
        "boss_title": norm_text(boss.get("title")),
        "boss_avatar": norm_text(boss.get("tiny") or boss.get("large")),
        "boss_active_desc": norm_text(boss.get("activeTimeDesc")),
        "boss_online": _flag(boss.get("bossOnline")),
        "boss_certificated": _flag(boss.get("certificated")),
        "brand_name": norm_text(brand.get("brandName")),
        "brand_logo": norm_text(brand.get("logo")),
        "brand_stage": norm_text(brand.get("stageName")),
        "brand_scale": norm_text(brand.get("scaleName")),
        "brand_industry": norm_text(brand.get("industryName")),
        "brand_introduce": norm_text(brand.get("introduce")),
        "brand_labels": _join_list(brand.get("labels")),
        "brand_active_at": _brand_active_at(brand),
        "encrypt_brand_id": norm_text(brand.get("encryptBrandId")),
        "encrypt_job_id": norm_text(
            job.get("encryptId") or job.get("encryptJobId")
        ),
        "security_id": norm_text(security_id),
        "raw_json": json.dumps(zp_data, ensure_ascii=False, separators=(",", ":")),
    }


def decide_action(latest: dict | None, new_hash: str) -> str:
    """版本决策：无历史或内容变化 -> insert；内容一致 -> update。"""
    if not latest or latest.get("content_hash") != new_hash:
        return "insert"
    return "update"
