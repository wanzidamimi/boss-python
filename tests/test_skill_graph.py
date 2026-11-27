"""技能图谱统计与匹配逻辑的单元测试（mock DB，不依赖 MySQL）。"""

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


if __name__ == "__main__":
    unittest.main()
