"""从职位详情正文抽取技能。

数据源固定为 job_details.post_description，不读取 jobs.skills 标签。
流程：分句 -> 判定分句角色 -> 词典别名匹配 -> 按职位去重 -> 标记 required/bonus。
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CATALOG_PATH = BASE_DIR / "static" / "skill_catalog.json"

_LEVEL_PRIORITY = {"required": 2, "bonus": 1}

_NEGATIVE_RE = re.compile(
    r"不要求|不需要|无需|不限|不考虑|不接受|不强制|不限制|非必须|"
    r"没有[^，。；;！!？?\n]{0,12}(?:也可以|亦可|也可)"
)
_BONUS_RE = re.compile(r"优先|加分|更佳|更好|者佳")
_REQUIRED_RE = re.compile(
    r"要求|需要|必须|熟悉|掌握|精通|熟练|擅长|具备|使用|运用|"
    r"能够|负责|参与|至少|任职|经验|技能|技术"
)
_ITEM_PREFIX_RE = re.compile(r"(?m)^\s*(?:\d+[.、)]|[-•·])\s*")
_INLINE_ITEM_RE = re.compile(r"(?<=[。；;！!？?])\s*(?:\d+[.、)]|[-•·])\s*")
_INLINE_NUMBER_RE = re.compile(r"(?<=\S)\s+(?:\d+[.、)]|[-•·])(?!\d)\s*")
_SPLIT_RE = re.compile(r"[\r\n。；;！!？?]+")
_SPACE_RE = re.compile(r"\s+")


def split_clauses(text: str) -> list[str]:
    """把 JD 正文切成短句/条款，同时保留编号条款的语义边界。"""
    if not text:
        return []
    value = str(text).replace("\u3000", " ")
    value = _ITEM_PREFIX_RE.sub("", value)
    value = _INLINE_ITEM_RE.sub(" ", value)
    value = _INLINE_NUMBER_RE.sub("。", value)
    return [
        clause
        for clause in (_SPACE_RE.sub(" ", part).strip() for part in _SPLIT_RE.split(value))
        if len(clause) >= 2
    ]


def classify_clause(clause: str) -> str:
    """required / bonus / negative / ambiguous。negative 与 ambiguous 不入库。"""
    if _NEGATIVE_RE.search(clause):
        return "negative"
    if _BONUS_RE.search(clause):
        return "bonus"
    if _REQUIRED_RE.search(clause):
        return "required"
    return "ambiguous"


def _is_ascii_alias(alias: str) -> bool:
    return bool(alias) and (alias[0].isascii() and (alias[0].isalnum() or alias[0] in "+#."))


def _alias_pattern(alias: str) -> re.Pattern:
    escaped = re.escape(alias)
    if _is_ascii_alias(alias):
        # 避免 Java 命中 JavaScript、Go 命中 Google / ago 这类误匹配
        return re.compile(rf"(?<![A-Za-z0-9+#._-]){escaped}(?![A-Za-z0-9+#._-])", re.I)
    return re.compile(escaped, re.I)


def _compile_catalog(catalog: dict) -> dict:
    skills = []
    name_map: dict[str, str] = {}
    alias_map: dict[str, str] = {}
    for item in catalog.get("skills", []):
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        aliases = []
        for raw in [name] + list(item.get("aliases", [])):
            alias = str(raw).strip()
            if alias and alias not in aliases:
                aliases.append(alias)
        for alias in aliases:
            key = alias.lower()
            if key in alias_map and alias_map[key] != name:
                raise ValueError(f"技能别名词典冲突: {alias} 同时指向 {alias_map[key]} 和 {name}")
            alias_map[key] = name
        name_map[name.lower()] = name
        skills.append({
            "name": name,
            "aliases": aliases,
            "category": str(item.get("category", "")),
            "roadmap_order": float(item.get("roadmap_order", 999)),
            "patterns": [_alias_pattern(a) for a in aliases],
            "excludes": [re.compile(str(x), re.I) for x in item.get("exclude", [])],
        })
    return {
        "version": str(catalog.get("version", "unknown")),
        "description": str(catalog.get("description", "")),
        "skills": skills,
        "name_map": name_map,
        "alias_map": alias_map,
    }


@lru_cache(maxsize=8)
def _compiled_from_path(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("skills"), list):
        raise ValueError("技能词典格式错误：需要 {version, skills: [...]}")
    return _compile_catalog(data)


def load_catalog(path: str | Path | None = None) -> dict:
    """返回原始词典 JSON。"""
    target = Path(path) if path else DEFAULT_CATALOG_PATH
    return json.loads(target.read_text(encoding="utf-8"))


def get_compiled_catalog(path: str | Path | None = None) -> dict:
    return _compiled_from_path(str(Path(path) if path else DEFAULT_CATALOG_PATH))


def catalog_summary(path: str | Path | None = None) -> dict:
    compiled = get_compiled_catalog(path)
    return {
        "version": compiled["version"],
        "description": compiled["description"],
        "skills": [
            {
                "name": s["name"],
                "aliases": s["aliases"],
                "category": s["category"],
                "roadmap_order": s["roadmap_order"],
            }
            for s in compiled["skills"]
        ],
    }


def canonical_skill(value: str, path: str | Path | None = None) -> str | None:
    """把用户输入的技能名/别名归一化到词典标准名，未收录返回 None。"""
    key = str(value or "").strip().lower()
    if not key:
        return None
    compiled = get_compiled_catalog(path)
    return compiled["alias_map"].get(key) or compiled["name_map"].get(key)


def extract_skills(text: str, path: str | Path | None = None) -> list[dict]:
    """从一个 JD 正文抽取技能；同一技能在一条 JD 中最多出现一次。"""
    compiled = get_compiled_catalog(path)
    found: dict[str, dict] = {}

    for clause in split_clauses(text):
        role = classify_clause(clause)
        if role not in _LEVEL_PRIORITY:
            continue
        for skill in compiled["skills"]:
            if any(ex.search(clause) for ex in skill["excludes"]):
                continue
            hit = next((alias for alias, pat in zip(skill["aliases"], skill["patterns"])
                        if pat.search(clause)), None)
            if not hit:
                continue
            previous = found.get(skill["name"])
            if previous and _LEVEL_PRIORITY[role] <= _LEVEL_PRIORITY[previous["skill_level"]]:
                continue
            found[skill["name"]] = {
                "skill_name": skill["name"],
                "category": skill["category"],
                "skill_level": role,
                "evidence_sentence": clause[:300],
                "matched_alias": hit,
                "catalog_version": compiled["version"],
            }

    return sorted(
        found.values(),
        key=lambda x: (-_LEVEL_PRIORITY[x["skill_level"]], x["skill_name"]),
    )
