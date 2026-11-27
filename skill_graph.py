"""技能图谱统计与个人匹配。

依赖 job_skills（由 job_details.post_description 抽取），不读取 jobs.skills 标签。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations

import db
import skill_extractor

MAX_SKILLS_PER_JOB = 20


def get_catalog() -> dict:
    return skill_extractor.catalog_summary()


def rebuild_skill_index() -> dict:
    """用最新版详情正文重建 job_skills；旧结果整体覆盖。"""
    catalog = skill_extractor.load_catalog()
    details = db.list_latest_details_with_description()
    db.clear_job_skills()

    inserted = 0
    jobs_with_skills = 0
    for detail in details:
        extracted = skill_extractor.extract_skills(detail["post_description"])
        rows = [
            {
                **row,
                "detail_id": detail["id"],
                "lid": detail["lid"],
                "task_id": detail["task_id"],
                "job_name": detail["job_name"],
            }
            for row in extracted
        ]
        inserted += db.replace_job_skills(detail["id"], rows)
        if rows:
            jobs_with_skills += 1

    return {
        "catalog_version": str(catalog.get("version", "unknown")),
        "details_scanned": len(details),
        "jobs_with_skills": jobs_with_skills,
        "rows_inserted": inserted,
        "source": "job_details.post_description",
    }


def _aggregate(rows: list[dict]) -> dict:
    stats: dict[str, dict] = {}
    job_skills: dict[int, set[str]] = defaultdict(set)
    job_meta: dict[int, dict] = {}

    for row in rows:
        detail_id = int(row["detail_id"])
        name = row["skill_name"]
        level = row["skill_level"] if row["skill_level"] in ("required", "bonus") else "bonus"
        item = stats.setdefault(name, {
            "name": name,
            "category": row.get("category") or "",
            "required": set(),
            "bonus": set(),
            "evidence": "",
            "matched_alias": row.get("matched_alias") or "",
        })
        item[level].add(detail_id)
        if not item["evidence"] and row.get("evidence_sentence"):
            item["evidence"] = row["evidence_sentence"]
        if not item["category"] and row.get("category"):
            item["category"] = row["category"]

        job_skills[detail_id].add(name)
        job_meta.setdefault(detail_id, {
            "detail_id": detail_id,
            "lid": row.get("lid") or "",
            "task_id": row.get("task_id") or "",
            "job_name": row.get("job_name") or "",
        })

    return {"stats": stats, "job_skills": job_skills, "job_meta": job_meta}


def build_graph(keyword: str = "", min_jobs: int = 1, limit: int = 40) -> dict:
    """按职位名/采集关键词聚合技能频次与共现关系。"""
    rows = db.query_job_skills(keyword)
    aggregated = _aggregate(rows)
    stats: dict[str, dict] = aggregated["stats"]
    job_skills: dict[int, set[str]] = aggregated["job_skills"]
    job_meta: dict[int, dict] = aggregated["job_meta"]

    def job_count(name: str) -> int:
        return len(stats[name]["required"] | stats[name]["bonus"])

    ordered = sorted(
        stats.values(),
        key=lambda x: (-len(x["required"]), -len(x["bonus"]), x["name"]),
    )
    kept = [item for item in ordered if job_count(item["name"]) >= max(1, min_jobs)]
    if limit and limit > 0:
        kept = kept[:limit]
    kept_names = {item["name"] for item in kept}

    edge_weights: Counter = Counter()
    for detail_id, names in job_skills.items():
        ranked = sorted(
            (n for n in names if n in kept_names),
            key=lambda n: (-job_count(n), n),
        )[:MAX_SKILLS_PER_JOB]
        for source, target in combinations(ranked, 2):
            edge_weights[(source, target)] += 1

    compiled = skill_extractor.get_compiled_catalog()
    order = {s["name"]: s["roadmap_order"] for s in compiled["skills"]}
    nodes = [
        {
            "id": item["name"],
            "name": item["name"],
            "category": item["category"],
            "value": len(item["required"]),
            "required_count": len(item["required"]),
            "bonus_count": len(item["bonus"]),
            "job_count": job_count(item["name"]),
            "roadmap_order": order.get(item["name"], 999),
            "evidence": item["evidence"],
            "matched_alias": item["matched_alias"],
        }
        for item in kept
    ]
    links = [
        {"source": source, "target": target, "weight": weight}
        for (source, target), weight in sorted(
            edge_weights.items(), key=lambda kv: (-kv[1], kv[0])
        )
        if weight >= max(1, min_jobs)
    ]

    positions = Counter()
    for detail_id, meta in job_meta.items():
        positions[meta["job_name"] or "(未命名职位)"] += 1

    jobs = []
    for detail_id in sorted(job_meta, key=lambda x: job_meta[x]["job_name"]):
        meta = job_meta[detail_id]
        jobs.append({
            **meta,
            "skills": sorted(job_skills[detail_id]),
        })

    return {
        "keyword": keyword,
        "nodes": nodes,
        "links": links,
        "positions": [
            {"name": name, "job_count": count}
            for name, count in sorted(positions.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "jobs": jobs,
        "meta": {
            "catalog_version": compiled["version"],
            "source": "job_details.post_description",
            "job_count": len(job_meta),
            "row_count": len(rows),
            "skill_count": len(stats),
            "shown_skill_count": len(nodes),
            "link_count": len(links),
            "note": "仅统计详情正文；jobs.skills 标签不参与。required 计入主分析，bonus 单独标注。",
        },
    }


def match_user(keyword: str = "", user_skills: list[str] | None = None) -> dict:
    """对比用户已掌握技能与该职位的技能画像，给出匹配度和学习顺序。"""
    compiled = skill_extractor.get_compiled_catalog()
    user_set: set[str] = set()
    unknown: list[str] = []
    for raw in user_skills or []:
        canonical = skill_extractor.canonical_skill(raw)
        if canonical:
            user_set.add(canonical)
        elif str(raw or "").strip():
            unknown.append(str(raw).strip())

    graph = build_graph(keyword, min_jobs=1, limit=0)
    required = [n for n in graph["nodes"] if n["required_count"] > 0]
    bonus = [n for n in graph["nodes"] if n["required_count"] == 0 and n["bonus_count"] > 0]

    matched_required = [n for n in required if n["id"] in user_set]
    missing_required = [n for n in required if n["id"] not in user_set]
    matched_bonus = [n for n in bonus if n["id"] in user_set]

    def learning_key(node: dict):
        # 用户关心“还要学什么新技术”，把沟通/抗压等通用能力排到技术之后
        generic = node["category"] in ("通用能力", "管理职能")
        return (generic, node["roadmap_order"], -node["required_count"], node["name"])

    roadmap = sorted(missing_required, key=learning_key)
    coverage = round(100 * len(matched_required) / len(required), 1) if required else 0.0

    return {
        "keyword": keyword,
        "user_skills": sorted(user_set),
        "unknown_skills": unknown,
        "coverage": coverage,
        "matched_required": matched_required,
        "missing_required": missing_required,
        "matched_bonus": matched_bonus,
        "learning_order": [
            {
                "index": idx,
                "name": n["name"],
                "category": n["category"],
                "required_count": n["required_count"],
                "evidence": n["evidence"],
            }
            for idx, n in enumerate(roadmap, start=1)
        ],
        "meta": graph["meta"],
        "positions": graph["positions"],
    }
