"""详情字段规范化与内容哈希的红绿测试。

覆盖：
- 同一内容稳定哈希
- 空白/换行/技能顺序差异不误判
- 描述、薪资变化能识别
- 易变字段（活跃状态、时间戳、地图、logo、头像）不参与哈希
- flatten 字段映射
- 版本决策（新增/更新）
"""

import copy
import json
import unittest

SAMPLE = {
    "code": 0,
    "message": "Success",
    "zpData": {
        "pageType": 0,
        "selfAccess": False,
        "securityId": "resp-security-id",
        "sessionId": None,
        "lid": "testLid.search.1",
        "jobInfo": {
            "encryptId": "enc1",
            "encryptUserId": "u1",
            "invalidStatus": False,
            "jobName": "Python 工程师",
            "position": 1,
            "positionName": "后端开发",
            "location": 101010100,
            "locationName": "北京",
            "locationUrl": "/c101010100/",
            "experienceName": "3-5年",
            "degreeName": "本科",
            "jobType": 0,
            "proxyJob": 0,
            "proxyType": 0,
            "salaryDesc": "20-30K",
            "payTypeDesc": None,
            "postDescription": "职责：\r\n• 负责服务端开发  \r\n• 参与架构设计",
            "recruitmentCountDesc": "",
            "encryptAddressId": "a1",
            "address": "北京市海淀区中关村1号",
            "longitude": 116.310316,
            "latitude": 39.984215,
            "staticMapUrl": "https://img.example.com/map1.jpg",
            "pcStaticMapUrl": "https://img.example.com/map1.webp",
            "baiduStaticMapUrl": "",
            "baiduPcStaticMapUrl": "",
            "overseasAddressList": [],
            "overseasInfo": None,
            "showSkills": ["Python", " FastAPI ", "MySQL", ""],
            "anonymous": 0,
            "jobStatusDesc": "招聘中",
        },
        "bossInfo": {
            "name": "张先生",
            "title": "技术总监",
            "tiny": "https://img.example.com/b1.jpg",
            "large": "https://img.example.com/b2.jpg",
            "activeTimeDesc": "今日活跃",
            "bossOnline": True,
            "brandName": "测试公司",
            "bossSource": 0,
            "certificated": True,
            "tagIconUrl": None,
            "avatarStickerUrl": None,
        },
        "brandComInfo": {
            "encryptBrandId": "b1",
            "brandName": "测试公司",
            "logo": "https://img.example.com/logo.jpg",
            "stage": 808,
            "stageName": "不需要融资",
            "scale": 304,
            "scaleName": "100-499人",
            "industry": 100020,
            "industryName": "互联网",
            "introduce": "公司简介...\n第二行  ",
            "labels": ["五险一金", " 年终奖 ", ""],
            "activeTime": 1794837031064,
            "visibleBrandInfo": True,
            "focusBrand": False,
            "customerBrandName": "测试公司",
            "customerBrandStageName": "不需要融资",
        },
        "oneKeyResumeInfo": {"inviteType": 0, "alreadySend": False},
        "relationInfo": {"interestJob": False, "beFriend": False},
        "handicappedInfo": None,
        "appendixInfo": {"canFeedback": False, "chatBubble": None},
        "atsOnlineApplyInfo": {"inviteType": 0, "alreadyApply": False},
        "certMaterials": [],
    },
}


def sample():
    return copy.deepcopy(SAMPLE)


def zp():
    return sample()["zpData"]


class TestContentHash(unittest.TestCase):
    def test_same_content_same_hash(self):
        from detail_hash import content_hash

        self.assertEqual(content_hash(zp()), content_hash(zp()))

    def test_volatile_fields_ignored(self):
        from detail_hash import content_hash

        a = zp()
        b = zp()
        b["jobInfo"]["staticMapUrl"] = "https://img.example.com/other.jpg"
        b["bossInfo"]["activeTimeDesc"] = "刚刚活跃"
        b["bossInfo"]["bossOnline"] = False
        b["bossInfo"]["tiny"] = "https://img.example.com/new-avatar.jpg"
        b["brandComInfo"]["activeTime"] = b["brandComInfo"]["activeTime"] + 9999
        b["brandComInfo"]["logo"] = "https://img.example.com/new-logo.jpg"
        self.assertEqual(content_hash(a), content_hash(b))

    def test_whitespace_and_newline_normalized(self):
        from detail_hash import content_hash

        a = zp()
        b = zp()
        b["jobInfo"]["postDescription"] = "职责：\n• 负责服务端开发\n• 参与架构设计"
        self.assertEqual(content_hash(a), content_hash(b))

    def test_skill_order_ignored(self):
        from detail_hash import content_hash

        a = zp()
        b = zp()
        b["jobInfo"]["showSkills"] = ["MySQL", "Python", "FastAPI"]
        self.assertEqual(content_hash(a), content_hash(b))

    def test_description_change_detected(self):
        from detail_hash import content_hash

        a = zp()
        b = zp()
        b["jobInfo"]["postDescription"] += "\n新增要求：熟悉爬虫"
        self.assertNotEqual(content_hash(a), content_hash(b))

    def test_salary_change_detected(self):
        from detail_hash import content_hash

        a = zp()
        b = zp()
        b["jobInfo"]["salaryDesc"] = "25-35K"
        self.assertNotEqual(content_hash(a), content_hash(b))

    def test_brand_intro_change_detected(self):
        from detail_hash import content_hash

        a = zp()
        b = zp()
        b["brandComInfo"]["introduce"] = "完全不同的公司介绍"
        self.assertNotEqual(content_hash(a), content_hash(b))

    def test_missing_fields_are_tolerated(self):
        from detail_hash import content_hash

        empty = {"jobInfo": {}, "bossInfo": {}, "brandComInfo": {}}
        self.assertIsInstance(content_hash(empty), str)
        self.assertEqual(len(content_hash(empty)), 64)


class TestFlatten(unittest.TestCase):
    def test_field_mapping(self):
        from detail_hash import flatten_detail

        row = flatten_detail(zp(), "testLid.search.1", "sec-123")
        self.assertEqual(row["lid"], "testLid.search.1")
        self.assertEqual(row["job_name"], "Python 工程师")
        self.assertEqual(row["position_name"], "后端开发")
        self.assertEqual(row["salary_desc"], "20-30K")
        self.assertEqual(row["experience_name"], "3-5年")
        self.assertEqual(row["degree_name"], "本科")
        self.assertEqual(row["location_name"], "北京")
        self.assertEqual(row["address"], "北京市海淀区中关村1号")
        self.assertEqual(row["longitude"], 116.310316)
        self.assertEqual(row["latitude"], 39.984215)
        self.assertEqual(row["boss_name"], "张先生")
        self.assertEqual(row["boss_title"], "技术总监")
        self.assertEqual(row["brand_name"], "测试公司")
        self.assertEqual(row["brand_stage"], "不需要融资")
        self.assertEqual(row["brand_scale"], "100-499人")
        self.assertEqual(row["brand_industry"], "互联网")
        self.assertEqual(row["encrypt_brand_id"], "b1")
        self.assertEqual(row["encrypt_job_id"], "enc1")
        self.assertEqual(row["security_id"], "sec-123")
        self.assertEqual(row["job_status_desc"], "招聘中")
        self.assertEqual(row["job_valid"], 1)
        self.assertEqual(row["anonymous"], 0)

    def test_lists_joined_with_pipe(self):
        from detail_hash import flatten_detail

        row = flatten_detail(zp(), "testLid.search.1")
        # 存储保留原顺序，空值被剔除，首尾空白被裁剪
        self.assertEqual(row["skills"], "Python|FastAPI|MySQL")
        self.assertEqual(row["brand_labels"], "五险一金|年终奖")

    def test_volatile_fields_stored(self):
        from detail_hash import flatten_detail

        row = flatten_detail(zp(), "testLid.search.1")
        self.assertEqual(row["boss_active_desc"], "今日活跃")
        self.assertEqual(row["boss_online"], 1)
        self.assertTrue(row["static_map_url"].endswith("map1.webp"))
        self.assertIsNotNone(row["brand_active_at"])

    def test_raw_json_roundtrip(self):
        from detail_hash import flatten_detail

        data = zp()
        row = flatten_detail(data, "testLid.search.1")
        self.assertEqual(json.loads(row["raw_json"])["jobInfo"]["jobName"], "Python 工程师")

    def test_hash_in_row_matches_content_hash(self):
        from detail_hash import content_hash, flatten_detail

        row = flatten_detail(zp(), "testLid.search.1")
        self.assertEqual(row["content_hash"], content_hash(zp()))


class TestDecideAction(unittest.TestCase):
    def test_first_capture_inserts(self):
        from detail_hash import decide_action

        self.assertEqual(decide_action(None, "abc"), "insert")

    def test_same_content_updates(self):
        from detail_hash import decide_action

        self.assertEqual(decide_action({"content_hash": "abc"}, "abc"), "update")

    def test_changed_content_inserts(self):
        from detail_hash import decide_action

        self.assertEqual(decide_action({"content_hash": "abc"}, "def"), "insert")


if __name__ == "__main__":
    unittest.main()
