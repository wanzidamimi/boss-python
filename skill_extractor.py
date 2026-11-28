"""从职位详情正文抽取技能与候选技能。

数据源固定为 job_details.post_description，不读取 jobs.skills 标签。
正式技能池来自 MySQL(skill_catalog)，JSON 只保留为一次性迁移种子。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import db

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

_EN_WORD_RE = re.compile(
    r"(?<![A-Za-z0-9+#])[A-Za-z][A-Za-z0-9+#._/-]{1,29}(?![A-Za-z0-9+#])"
)
_CN_SEG_RE = re.compile(r"[\u4e00-\u9fff]{2,14}")

_EN_STOPWORDS = {
    "and", "or", "the", "a", "an", "with", "from", "for", "have", "has", "had",
    "can", "could", "will", "would", "should", "must", "are", "is", "was", "were",
    "this", "that", "these", "those", "you", "we", "our", "your", "their", "its",
    "at", "in", "on", "of", "to", "by", "as", "be", "been", "being", "etc",
    "skill", "skills", "ability", "abilities", "experience", "experiences",
    "knowledge", "work", "working", "team", "teams", "year", "years", "preferred",
    "required", "responsible", "development", "related", "good", "strong", "better",
    "familiar", "proficient", "understanding", "using", "use", "used", "including",
    "include", "includes", "other", "others", "more", "least", "than", "within",
}

_CN_STOPWORDS = {
    "工作", "经验", "能力", "要求", "负责", "熟悉", "掌握", "熟练", "精通", "优先",
    "相关", "公司", "岗位", "职位", "团队", "沟通", "以及", "并且", "能够", "具有",
    "具备", "至少", "以上", "以下", "其他", "我们", "他们", "自己", "工作内容",
    "岗位职责", "岗位要求", "任职要求", "职位描述", "优先考虑", "一年", "三年",
    "五年", "学历", "本科", "大专", "专业", "待遇", "福利", "薪资", "工作地点",
    "开发", "任职", "技术", "数据", "使用", "设计", "良好", "好的", "框架", "系统",
    "优化", "项目", "参与", "工具", "问题", "编程", "语言", "独立", "学习", "意识",
    "基础", "文档", "记录", "说明", "推动", "解决", "处理", "流程", "运行", "跟踪",
    "效果", "需求", "分析", "配合", "计算机", "在校", "竞赛", "经历", "价值", "效率",
    "质量", "判断", "输出", "任务", "具体", "结合", "完成", "了解", "应用", "研究",
    "进行", "实现", "维护", "支持", "根据", "通过", "包括", "等", "和", "与", "或",
}

_TECH_HINTS = (
    "算法", "模型", "训练", "推理", "数据", "数据库", "缓存", "接口", "网络", "协议",
    "引擎", "平台", "架构", "服务", "存储", "解析", "调度", "监控", "日志", "前端",
    "后端", "全栈", "部署", "运维", "测试", "安全", "自动化", "可视化", "工程",
    "知识", "向量", "特征", "指标", "权限", "认证", "地图", "爬虫", "招聘", "仿真",
    "控制", "焊接", "数控", "造价", "预算", "结算", "建筑", "机械", "电气", "机电",
)

_GENERIC_PREFIXES = (
    "熟悉", "掌握", "熟练", "精通", "具备", "使用", "负责", "参与", "了解", "能够",
    "要求", "需要", "任职", "具有", "良好", "优秀的", "较强的",
    "的", "了", "等", "用", "型", "务", "处", "上", "下", "中", "对", "从", "在",
    "为", "有", "及", "主流", "常用", "流", "量", "业务",
)


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
        return re.compile(rf"(?<![A-Za-z0-9+#._-]){escaped}(?![A-Za-z0-9+#._-])", re.I)
    return re.compile(escaped, re.I)


def compile_catalog(catalog: dict) -> dict:
    """把原始词典编译成可匹配结构。"""
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


def load_catalog_from_file(path: str | Path | None = None) -> dict:
    """仅用于一次性迁移和离线测试；运行时不读取 JSON。"""
    target = Path(path) if path else DEFAULT_CATALOG_PATH
    return json.loads(target.read_text(encoding="utf-8"))


def load_catalog() -> dict:
    """从 MySQL 正式技能池读取词典。"""
    meta = db.get_skill_catalog_meta()
    skills = []
    for row in db.list_skill_catalog(enabled_only=True):
        try:
            aliases = json.loads(row["aliases_json"])
        except (ValueError, TypeError):
            aliases = [row["name"]]
        skills.append({
            "name": row["name"],
            "aliases": aliases,
            "category": row["category"],
            "roadmap_order": row["roadmap_order"],
            "exclude": [],
        })
    return {
        "version": meta.get("version", "unknown"),
        "description": meta.get("description", ""),
        "skills": skills,
    }


def get_compiled_catalog() -> dict:
    return compile_catalog(load_catalog())


def catalog_summary() -> dict:
    compiled = get_compiled_catalog()
    return {
        "version": compiled["version"],
        "description": compiled["description"],
        "source": "mysql.skill_catalog",
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


def canonical_skill(value: str) -> str | None:
    """把用户输入的技能名/别名归一化到正式技能池标准名。"""
    key = str(value or "").strip().lower()
    if not key:
        return None
    compiled = get_compiled_catalog()
    return compiled["alias_map"].get(key) or compiled["name_map"].get(key)


def extract_skills(text: str, compiled: dict | None = None) -> list[dict]:
    """从一个 JD 正文抽取正式技能；同一技能在一条 JD 中最多出现一次。"""
    compiled = compiled or get_compiled_catalog()
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


def _candidate_key(value: str) -> str:
    return _SPACE_RE.sub("", str(value or "")).strip(" .,/_-").lower()


def _valid_candidate(value: str) -> bool:
    if len(value) < 2 or len(value) > 40:
        return False
    if re.fullmatch(r"[\d.]+", value):
        return False
    if any(ch in value for ch in "，。；：！？、（）()[]{}<>“”\"'"):
        return False

    lower = value.lower()
    if lower in _EN_STOPWORDS or value in _CN_STOPWORDS:
        return False

    is_chinese = bool(re.fullmatch(r"[\u4e00-\u9fff]+", value))
    if is_chinese:
        # 两字中文片段几乎都是泛词，不进入候选池
        if len(value) < 3:
            return False
        if value.startswith(_GENERIC_PREFIXES):
            return False
        if value.endswith(("的", "了", "等")):
            return False
        if not any(hint in value for hint in _TECH_HINTS):
            return False
    return True


def extract_candidates(text: str, compiled: dict | None = None) -> list[dict]:
    """从 JD 正文中找出未被正式词典命中的候选技能片段。"""
    compiled = compiled or get_compiled_catalog()
    found: dict[str, dict] = {}

    for clause in split_clauses(text):
        role = classify_clause(clause)
        if role not in _LEVEL_PRIORITY:
            continue

        cleaned = clause
        for skill in compiled["skills"]:
            for pattern in skill["patterns"]:
                cleaned = pattern.sub(" ", cleaned)

        raw_candidates: list[str] = []
        raw_candidates.extend(
            m.group(0).strip(".,/_-") for m in _EN_WORD_RE.finditer(cleaned)
        )
        for segment in _CN_SEG_RE.findall(cleaned):
            for size in (3, 4, 5, 6):
                for i in range(len(segment) - size + 1):
                    raw_candidates.append(segment[i:i + size])

        for raw in raw_candidates:
            key = _candidate_key(raw)
            if not key or not _valid_candidate(raw):
                continue
            item = found.setdefault(key, {
                "raw_mention": raw,
                "normalized_key": key,
                "evidence_sentence": clause[:300],
            })
            if len(raw) > len(item["raw_mention"]):
                item["raw_mention"] = raw

    return list(found.values())
