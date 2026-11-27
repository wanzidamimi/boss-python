"""职位详情正文技能抽取的单元测试（不依赖 MySQL）。"""

import unittest

from skill_extractor import (
    canonical_skill,
    classify_clause,
    extract_skills,
    split_clauses,
)


class SkillExtractorTest(unittest.TestCase):
    def test_split_keeps_decimal_years_together(self):
        clauses = split_clauses("1. 熟悉 Python 3.5年以上 2.掌握 Flask")
        self.assertIn("熟悉 Python 3.5年以上", clauses)
        self.assertIn("掌握 Flask", clauses)

    def test_clause_roles(self):
        self.assertEqual(classify_clause("需要熟悉 Python"), "required")
        self.assertEqual(classify_clause("有 Kubernetes 经验优先"), "bonus")
        self.assertEqual(classify_clause("无需 Go 经验"), "negative")
        self.assertEqual(classify_clause("弹性工作"), "ambiguous")

    def test_extract_required_bonus_and_negative(self):
        text = (
            "岗位要求：熟悉 Python、Flask、MySQL，掌握 Docker；"
            "有 Kubernetes 经验优先；无需 Go 经验。"
        )
        rows = {row["skill_name"]: row for row in extract_skills(text)}
        self.assertEqual(rows["Python"]["skill_level"], "required")
        self.assertEqual(rows["Docker"]["skill_level"], "required")
        self.assertEqual(rows["Kubernetes"]["skill_level"], "bonus")
        self.assertNotIn("Go", rows)
        self.assertIn("Python", rows["Python"]["evidence_sentence"])

    def test_required_wins_over_bonus_and_no_duplicate(self):
        text = "熟悉 Java 优先；岗位要求必须熟练使用 Java 和 Spring Boot。"
        rows = {row["skill_name"]: row for row in extract_skills(text)}
        self.assertEqual(rows["Java"]["skill_level"], "required")
        self.assertEqual(rows["Spring Boot"]["skill_level"], "required")
        self.assertEqual(len([r for r in rows.values() if r["skill_name"] == "Java"]), 1)

    def test_ascii_boundaries_avoid_javascript_false_positive(self):
        rows = {row["skill_name"] for row in extract_skills("任职要求：熟悉 JavaScript 开发")}
        self.assertIn("JavaScript", rows)
        self.assertNotIn("Java", rows)

    def test_canonical_skill(self):
        self.assertEqual(canonical_skill("python"), "Python")
        self.assertEqual(canonical_skill("NX"), "UG/NX")
        self.assertIsNone(canonical_skill("不存在的技能"))


if __name__ == "__main__":
    unittest.main()
