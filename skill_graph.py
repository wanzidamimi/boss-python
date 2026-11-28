"""技能图谱统计与个人匹配。

依赖 job_skills（由 job_details.post_description 抽取），不读取 jobs.skills 标签。
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from itertools import combinations

import db
import llm_reviewer
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
        if row.get("evidence_sentence") and (level == "required" or not item["evidence"]):
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


# ---------------------------------------------------------------------------
# 按职位名推荐技能：输入职位 -> 输出该职位应该会什么
# ---------------------------------------------------------------------------
_TITLE_NOISE_RE = re.compile(r"[\s\-_/\\()（）\[\]【】·、,，.。]+")


def _normalize_title(value: str) -> str:
    return _TITLE_NOISE_RE.sub("", str(value or "")).lower()


def _bigram_similarity(left: str, right: str) -> float:
    if len(left) < 2 or len(right) < 2:
        return 0.0
    a = {left[i:i + 2] for i in range(len(left) - 1)}
    b = {right[i:i + 2] for i in range(len(right) - 1)}
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _position_score(row: dict, position: str) -> float:
    """职位名匹配得分：精确 > 包含 > 任务关键词 > 字符二元组模糊。"""
    target = _normalize_title(position)
    if not target:
        return 0.0
    name = _normalize_title(row.get("job_name"))
    task = _normalize_title(row.get("task_keyword"))

    if name and name == target:
        return 1.0
    if name and target in name:
        return min(0.95, 0.85 + 0.1 * len(target) / len(name))
    if name and len(name) >= 2 and name in target:
        return 0.85
    if task and task == target:
        return 0.7
    if task and target in task:
        return 0.65
    similarity = max(
        _bigram_similarity(target, name),
        _bigram_similarity(target, task) * 0.8,
    )
    return 0.6 * similarity if similarity >= 0.35 else 0.0


def _empty_recommendation(position: str, rows: list[dict], compiled: dict) -> dict:
    suggestions = Counter(row.get("job_name") or "(未命名职位)" for row in rows)
    return {
        "position": position,
        "job_count": 0,
        "matched_positions": [],
        "required_skills": [],
        "bonus_skills": [],
        "skills": [],
        "heatmap": [],
        "learning_path": [],
        "meta": {
            "catalog_version": compiled["version"],
            "source": "job_details.post_description",
            "note": "未匹配到该职位，请换一个更接近职位名称的关键词。",
            "suggestions": [name for name, _ in suggestions.most_common(10)],
        },
    }


def recommend_positions(position: str = "") -> dict:
    """输入职位名，输出该职位应该具备的技能画像和学习顺序。"""
    position = str(position or "").strip()
    compiled = skill_extractor.get_compiled_catalog()
    rows = db.query_job_skills()
    if not position:
        return _empty_recommendation(position, rows, compiled)

    scored_details: dict[int, dict] = {}
    for row in rows:
        score = _position_score(row, position)
        if score < 0.6:
            continue
        detail_id = int(row["detail_id"])
        current = scored_details.setdefault(detail_id, {"score": score, "rows": []})
        current["score"] = max(current["score"], score)
        current["rows"].append(row)

    selected_rows = [row for item in scored_details.values() for row in item["rows"]]
    if not selected_rows:
        return _empty_recommendation(position, rows, compiled)

    aggregated = _aggregate(selected_rows)
    stats: dict[str, dict] = aggregated["stats"]
    job_meta: dict[int, dict] = aggregated["job_meta"]
    job_count = len(job_meta)
    order = {s["name"]: s["roadmap_order"] for s in compiled["skills"]}

    skills = []
    for name, item in stats.items():
        required_count = len(item["required"])
        bonus_count = len(item["bonus"])
        required_ratio = round(required_count / job_count, 3) if job_count else 0.0
        bonus_ratio = round(bonus_count / job_count, 3) if job_count else 0.0
        if required_ratio >= 0.5:
            level = "核心必备"
        elif required_ratio >= 0.25:
            level = "常见必备"
        elif required_count:
            level = "低频必备"
        else:
            level = "加分项"
        skills.append({
            "name": name,
            "category": item["category"],
            "level": level,
            "required_count": required_count,
            "bonus_count": bonus_count,
            "job_count": len(item["required"] | item["bonus"]),
            "required_ratio": required_ratio,
            "bonus_ratio": bonus_ratio,
            "importance_score": round(required_ratio * 100 + bonus_ratio * 20, 1),
            "roadmap_order": order.get(name, 999),
            "evidence": item["evidence"],
            "matched_alias": item["matched_alias"],
        })

    skills.sort(key=lambda s: (-s["importance_score"], -s["required_count"], s["name"]))
    max_required = max([s["required_count"] for s in skills] + [1])
    heatmap = [
        {
            "name": s["name"],
            "value": s["required_count"],
            "ratio": s["required_ratio"],
            "intensity": round(s["required_count"] / max_required, 3),
            "level": s["level"],
            "category": s["category"],
            "evidence": s["evidence"],
        }
        for s in skills
    ]

    def learning_key(skill: dict):
        generic = skill["category"] in ("通用能力", "管理职能")
        return (
            skill["level"] == "加分项",
            generic,
            skill["roadmap_order"],
            -skill["importance_score"],
            skill["name"],
        )

    learning_path = [
        {
            "index": idx,
            "name": s["name"],
            "category": s["category"],
            "level": s["level"],
            "required_count": s["required_count"],
            "required_ratio": s["required_ratio"],
            "evidence": s["evidence"],
        }
        for idx, s in enumerate(sorted(skills, key=learning_key), start=1)
    ]

    positions = Counter(
        meta["job_name"] or "(未命名职位)" for meta in job_meta.values()
    )
    return {
        "position": position,
        "job_count": job_count,
        "matched_positions": [
            {"name": name, "job_count": count}
            for name, count in sorted(positions.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "required_skills": [s for s in skills if s["required_count"] > 0],
        "bonus_skills": [s for s in skills if s["required_count"] == 0 and s["bonus_count"] > 0],
        "skills": skills,
        "heatmap": heatmap,
        "learning_path": learning_path,
        "meta": {
            "catalog_version": compiled["version"],
            "source": "job_details.post_description",
            "job_count": job_count,
            "skill_count": len(skills),
            "note": "按详情正文统计；每个职位命中同一技能只计一次。字体越大、颜色越深表示该职位技能要求中出现得越多。",
        },
    }


# ---------------------------------------------------------------------------
# 候选技能池
# ---------------------------------------------------------------------------
def mine_candidates(min_hits: int = 2, max_rows: int = 200) -> dict:
    """扫描详情正文中未命中正式词典的片段，重建 pending 候选池。"""
    details = db.list_latest_details_with_description()
    collected: dict[str, dict] = {}

    for detail in details:
        for candidate in skill_extractor.extract_candidates(detail["post_description"]):
            key = candidate["normalized_key"]
            item = collected.setdefault(key, {
                "raw_mention": candidate["raw_mention"],
                "normalized_key": key,
                "hit_count": 0,
                "evidence_sentence": candidate["evidence_sentence"],
                "status": "pending",
                "llm_status": "none",
                "source": "rule",
            })
            item["hit_count"] += 1
            if len(candidate["raw_mention"]) > len(item["raw_mention"]):
                item["raw_mention"] = candidate["raw_mention"]

    rows = [row for row in collected.values() if row["hit_count"] >= max(1, min_hits)]
    rows.sort(key=lambda row: (-row["hit_count"], -len(row["normalized_key"]), row["normalized_key"]))

    # 去掉被更长、更高频候选包含的短片段，例如“向量”被“向量数据库”包含
    kept: list[dict] = []
    for row in rows[:max_rows * 3]:
        key = row["normalized_key"]
        if any(key != other["normalized_key"] and key in other["normalized_key"]
               and row["hit_count"] <= other["hit_count"] for other in kept):
            continue
        kept.append(row)
        if len(kept) >= max_rows:
            break

    db.reset_pending_skill_candidates()
    db.upsert_skill_candidates(kept)
    return {
        "details_scanned": len(details),
        "raw_candidates": len(collected),
        "candidates_kept": len(kept),
        "min_hits": min_hits,
        "source": "job_details.post_description",
    }


def list_candidates(status: str = "", limit: int = 200) -> list[dict]:
    rows = db.list_skill_candidates(status=status, limit=limit)
    for row in rows:
        try:
            row["llm_suggestion"] = json.loads(row.get("llm_suggestion_json") or "null")
        except (TypeError, ValueError):
            row["llm_suggestion"] = None
    return rows


def approve_candidate(candidate_id: int, name: str = "", aliases: list[str] | None = None,
                      category: str = "", roadmap_order: float = 999,
                      source: str = "manual") -> dict:
    """把候选技能确认进正式技能池。"""
    row = db.get_skill_candidate(candidate_id)
    if not row:
        raise ValueError("候选不存在")

    suggestion = None
    if row.get("llm_suggestion_json"):
        try:
            suggestion = json.loads(row["llm_suggestion_json"])
        except (TypeError, ValueError):
            suggestion = None

    final_name = str(name or "").strip()
    if not final_name and suggestion:
        final_name = str(suggestion.get("canonical_name") or "").strip()
    final_name = final_name or row["raw_mention"]

    final_category = str(category or "").strip()
    if not final_category and suggestion:
        final_category = str(suggestion.get("category") or "").strip()
    final_category = final_category or "待分类"

    final_aliases = list(aliases or [])
    if not final_aliases:
        final_aliases = [row["raw_mention"], row["normalized_key"]]
        if suggestion and suggestion.get("canonical_name"):
            final_aliases.append(str(suggestion["canonical_name"]))

    if suggestion and not roadmap_order:
        try:
            roadmap_order = float(suggestion.get("roadmap_order", 999))
        except (TypeError, ValueError):
            roadmap_order = 999

    added = db.add_skill_catalog_item(
        final_name, final_aliases, final_category, roadmap_order, source
    )
    db.update_skill_candidate(candidate_id, status="approved")
    return {
        "candidate_id": candidate_id,
        "added": added,
        "name": final_name,
        "category": final_category,
        "aliases": final_aliases,
        "source": source,
    }


def reject_candidate(candidate_id: int) -> dict:
    if not db.get_skill_candidate(candidate_id):
        raise ValueError("候选不存在")
    db.update_skill_candidate(candidate_id, status="rejected")
    return {"candidate_id": candidate_id, "status": "rejected"}


def review_candidates_with_llm(ids: list[int] | None = None, limit: int = 20,
                               auto_apply: bool = False) -> dict:
    """用 LLM 判断候选是否为技能；默认只生成建议，不自动加入正式池。"""
    rows = db.list_skill_candidates(status="pending", limit=500)
    if ids:
        id_set = {int(x) for x in ids}
        rows = [row for row in rows if int(row["id"]) in id_set]
    rows = rows[:max(1, limit)]

    if not rows:
        return {"reviewed": 0, "applied": 0, "errors": 0}

    try:
        suggestions = llm_reviewer.review_candidates(rows)
    except Exception as exc:
        for row in rows:
            db.update_skill_candidate(
                row["id"], llm_status="error",
                llm_suggestion_json=json.dumps({"error": str(exc)[:500]}, ensure_ascii=False),
            )
        return {"reviewed": 0, "applied": 0, "errors": len(rows), "error": str(exc)[:500]}

    by_id = {int(item.get("id")): item for item in suggestions}
    applied = 0
    errors = 0
    for row in rows:
        suggestion = by_id.get(int(row["id"]))
        if not suggestion:
            errors += 1
            db.update_skill_candidate(row["id"], llm_status="error")
            continue
        db.update_skill_candidate(
            row["id"], llm_status="suggested",
            llm_suggestion_json=json.dumps(suggestion, ensure_ascii=False),
        )
        if auto_apply and suggestion.get("is_skill") and float(suggestion.get("confidence", 0)) >= 0.75:
            approve_candidate(
                row["id"],
                name=str(suggestion.get("canonical_name") or ""),
                category=str(suggestion.get("category") or ""),
                roadmap_order=float(suggestion.get("roadmap_order", 999)),
                source="llm",
            )
            applied += 1

    return {"reviewed": len(rows), "applied": applied, "errors": errors}


def apply_llm_suggestions(ids: list[int] | None = None, min_confidence: float = 0.75) -> dict:
    """将 LLM 建议中高置信度且判定为技能的项目加入正式池。"""
    rows = db.list_skill_candidates(status="pending", limit=500)
    rows = [row for row in rows if row.get("llm_status") == "suggested"]
    if ids:
        id_set = {int(x) for x in ids}
        rows = [row for row in rows if int(row["id"]) in id_set]

    applied = 0
    for row in rows:
        try:
            suggestion = json.loads(row.get("llm_suggestion_json") or "null")
        except (TypeError, ValueError):
            suggestion = None
        if not suggestion or not suggestion.get("is_skill"):
            continue
        try:
            confidence = float(suggestion.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0
        if confidence < min_confidence:
            continue
        approve_candidate(
            row["id"],
            name=str(suggestion.get("canonical_name") or ""),
            category=str(suggestion.get("category") or ""),
            roadmap_order=float(suggestion.get("roadmap_order", 999)),
            source="llm",
        )
        applied += 1
    return {"applied": applied, "reviewed": len(rows)}


def llm_status() -> dict:
    return llm_reviewer.config_status()
