"""技能图谱统计与匹配逻辑的单元测试（mock DB，不依赖 MySQL）。"""

import json
import unittest
from unittest.mock import patch

import skill_graph


def make_row(detail_id, skill, level="required", job_name="Python开发工程师", category="编程开发"):
    return {
        "detail_id": detail_id,
        "lid": f"lid-{detail_id}",
        "task_id": "task-1",
        "job_name": job_name,
        "skill_name": skill,
        "category": category,
        "skill_level": level,
        "evidence_sentence": f"熟悉 {skill}",
        "matched_alias": skill,
    }


ROWS = [
    make_row(1, "Python"),
    make_row(1, "Flask"),
    make_row(1, "MySQL"),
    make_row(2, "Python"),
    make_row(2, "Docker"),
    make_row(2, "Kubernetes", "bonus", category="运维与云原生"),
]


class SkillGraphTest(unittest.TestCase):
    def test_build_graph_counts_jobs_and_cooccurrence(self):
        with patch.object(skill_graph.db, "query_job_skills", return_value=ROWS) as query:
            graph = skill_graph.build_graph("Python", limit=20)
        query.assert_called_once_with("Python")
        nodes = {n["name"]: n for n in graph["nodes"]}
        self.assertEqual(nodes["Python"]["required_count"], 2)
        self.assertEqual(nodes["Kubernetes"]["bonus_count"], 1)
        links = {frozenset((l["source"], l["target"])): l["weight"] for l in graph["links"]}
        self.assertEqual(links[frozenset({"Docker", "Python"})], 1)
        self.assertEqual(graph["meta"]["job_count"], 2)
        self.assertEqual(graph["positions"][0]["name"], "Python开发工程师")

    def test_match_user_coverage_and_learning_order(self):
        with patch.object(skill_graph.db, "query_job_skills", return_value=ROWS):
            result = skill_graph.match_user("Python", ["python", "不存在技能"])
        self.assertEqual(result["user_skills"], ["Python"])
        self.assertEqual(result["unknown_skills"], ["不存在技能"])
        self.assertEqual(result["coverage"], 25.0)
        names = [item["name"] for item in result["learning_order"]]
        self.assertEqual(names, ["MySQL", "Flask", "Docker"])
        self.assertIn("Python", [n["name"] for n in result["matched_required"]])

    def test_rebuild_uses_detail_text_only(self):
        details = [{
            "id": 9, "lid": "lid-9", "task_id": "task-9",
            "job_name": "Python工程师",
            "post_description": "任职要求：熟悉 Python 和 Flask；有 Docker 经验优先。",
        }]
        with patch.object(skill_graph.db, "list_latest_details_with_description", return_value=details), \
             patch.object(skill_graph.db, "clear_job_skills") as clear, \
             patch.object(skill_graph.db, "replace_job_skills", return_value=3) as replace:
            result = skill_graph.rebuild_skill_index()
        clear.assert_called_once_with()
        replace.assert_called_once()
        self.assertEqual(result["details_scanned"], 1)
        self.assertEqual(result["rows_inserted"], 3)
        self.assertEqual(result["source"], "job_details.post_description")


class SkillRecommendTest(unittest.TestCase):
    def rows(self):
        return [
            {**make_row(1, "Python", job_name="爬虫工程师"), "task_keyword": "爬虫"},
            {**make_row(1, "Scrapy", job_name="爬虫工程师"), "task_keyword": "爬虫"},
            {**make_row(2, "Python", job_name="爬虫工程师"), "task_keyword": "爬虫"},
            {**make_row(2, "Kubernetes", "bonus", job_name="爬虫工程师",
                         category="运维与云原生"), "task_keyword": "爬虫"},
            make_row(3, "Java", job_name="Java开发工程师"),
        ]

    def test_recommend_skills_for_position(self):
        with patch.object(skill_graph.db, "query_job_skills", return_value=self.rows()):
            result = skill_graph.recommend_positions("爬虫工程师")
        self.assertEqual(result["job_count"], 2)
        skills = {s["name"]: s for s in result["skills"]}
        self.assertEqual(skills["Python"]["required_count"], 2)
        self.assertEqual(skills["Python"]["level"], "核心必备")
        self.assertEqual(skills["Kubernetes"]["level"], "加分项")
        self.assertEqual(result["heatmap"][0]["name"], "Python")
        self.assertEqual(result["learning_path"][0]["name"], "Python")
        self.assertNotIn("Java开发工程师", [p["name"] for p in result["matched_positions"]])

    def test_recommend_empty_with_suggestions(self):
        with patch.object(skill_graph.db, "query_job_skills", return_value=self.rows()):
            result = skill_graph.recommend_positions("不存在的职位")
        self.assertEqual(result["job_count"], 0)
        self.assertIn("Java开发工程师", result["meta"]["suggestions"])


class CandidatePoolTest(unittest.TestCase):
    def test_approve_candidate_uses_llm_suggestion(self):
        row = {
            "id": 1,
            "raw_mention": "Milvus",
            "normalized_key": "milvus",
            "llm_suggestion_json": json.dumps({
                "id": 1, "is_skill": True, "canonical_name": "向量数据库",
                "category": "数据库", "roadmap_order": 30, "confidence": 0.9,
            }, ensure_ascii=False),
        }
        with patch.object(skill_graph.db, "get_skill_candidate", return_value=row), \
             patch.object(skill_graph.db, "add_skill_catalog_item", return_value=True) as add, \
             patch.object(skill_graph.db, "update_skill_candidate") as update:
            result = skill_graph.approve_candidate(1)
        self.assertEqual(result["name"], "向量数据库")
        self.assertEqual(result["category"], "数据库")
        self.assertEqual(add.call_args.args[0], "向量数据库")
        update.assert_called_once_with(1, status="approved")

    def test_apply_llm_suggestions_only_high_confidence(self):
        rows = [
            {"id": 1, "llm_status": "suggested", "llm_suggestion_json": json.dumps({
                "is_skill": True, "canonical_name": "向量数据库", "category": "数据库",
                "roadmap_order": 30, "confidence": 0.9,
            }, ensure_ascii=False)},
            {"id": 2, "llm_status": "suggested", "llm_suggestion_json": json.dumps({
                "is_skill": True, "canonical_name": "不确定的词", "category": "其他",
                "roadmap_order": 999, "confidence": 0.4,
            }, ensure_ascii=False)},
        ]
        with patch.object(skill_graph.db, "list_skill_candidates", return_value=rows), \
             patch.object(skill_graph, "approve_candidate") as approve:
            result = skill_graph.apply_llm_suggestions(min_confidence=0.75)
        self.assertEqual(result["applied"], 1)
        approve.assert_called_once()

    def test_mine_candidates_rebuilds_pending_pool(self):
        details = [{"id": 1, "post_description": "熟悉 Python、Milvus 和向量数据库；有数据清洗经验"}]
        with patch.object(skill_graph.db, "list_latest_details_with_description", return_value=details), \
             patch.object(skill_graph.db, "reset_pending_skill_candidates") as reset, \
             patch.object(skill_graph.db, "upsert_skill_candidates") as upsert:
            result = skill_graph.mine_candidates(min_hits=1, max_rows=20)
        reset.assert_called_once_with()
        self.assertGreaterEqual(result["candidates_kept"], 1)
        self.assertGreater(upsert.call_count, 0)


if __name__ == "__main__":
    unittest.main()
