"""LLM 候选技能判断。

只输出结构化建议，不直接决定正式技能池；由人工或应用建议接口确认后写入 MySQL。
兼容 OpenAI Chat Completions 风格的接口。
"""
from __future__ import annotations

import json
import os
import re

import requests

SYSTEM_PROMPT = """你是招聘 JD 技能词典审核助手。
请判断每个候选词是否是一个真实、具体、可用于职位技能图谱的技能或工具。

要求：
1. 只根据候选词和证据句判断，不要臆造证据中没有的信息。
2. 泛化词、动作词、部门名、公司福利、软泛词不算技能，例如“工作”“经验”“团队”“薪资”“北京”“优先”。
3. 如果候选词只是更长技能的片段，给出更完整的 canonical_name。
4. 输出严格 JSON 对象：{"results":[{"id":候选ID,"is_skill":true或false,"canonical_name":"标准技能名或空字符串","category":"分类或空字符串","roadmap_order":数字,"confidence":0到1,"reason":"简短理由"}]}
5. results 数量必须与输入数量一致，不要输出 Markdown。
"""


def config_status() -> dict:
    return {
        "configured": bool(os.getenv("BOSS_LLM_API_KEY") and os.getenv("BOSS_LLM_BASE_URL")),
        "base_url": os.getenv("BOSS_LLM_BASE_URL", ""),
        "model": os.getenv("BOSS_LLM_MODEL", "gpt-4o-mini"),
        "note": "未配置时不调用外部模型；页面仍可人工确认。",
    }


def _extract_json(text: str) -> dict:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    try:
        data = json.loads(value)
    except ValueError as exc:
        raise ValueError(f"LLM 输出不是合法 JSON: {value[:300]}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("LLM 输出缺少 results 数组")
    return data


def review_candidates(rows: list[dict]) -> list[dict]:
    base_url = os.getenv("BOSS_LLM_BASE_URL", "").rstrip("/")
    api_key = os.getenv("BOSS_LLM_API_KEY", "")
    model = os.getenv("BOSS_LLM_MODEL", "gpt-4o-mini")
    if not base_url or not api_key:
        raise RuntimeError("未配置 BOSS_LLM_BASE_URL / BOSS_LLM_API_KEY")

    candidates = [
        {
            "id": row["id"],
            "raw_mention": row["raw_mention"],
            "hit_count": row["hit_count"],
            "evidence": row.get("evidence_sentence", ""),
        }
        for row in rows
    ]
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(candidates, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
    }
    response = requests.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    data = _extract_json(content)
    results = []
    for item in data["results"]:
        if not isinstance(item, dict) or "id" not in item:
            continue
        try:
            item["id"] = int(item["id"])
            item["confidence"] = float(item.get("confidence", 0))
            item["roadmap_order"] = float(item.get("roadmap_order", 999))
            item["is_skill"] = bool(item.get("is_skill"))
        except (TypeError, ValueError):
            continue
        results.append(item)
    return results
