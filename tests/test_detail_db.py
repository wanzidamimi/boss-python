"""job_details 版本写入的集成测试（需要真实 MySQL，设置 BOSS_TEST_DB=1 才运行）。

覆盖：
- 首次采集 -> 新增版本
- 内容未变 -> 不新增行，只刷新 captured_at / capture_count / 易变字段
- 内容变化 -> 新增版本
- 内容回退 -> 再次新增版本（因为相对上一条是变化）
"""

import os
import unittest
from datetime import datetime

import db
from detail_hash import flatten_detail

TEST_LID = "__test__detail_store"


def make_zp(desc="职位描述 A", active="今日活跃", salary="20-30K"):
    return {
        "jobInfo": {
            "encryptId": "enc-test",
            "jobName": "测试职位",
            "salaryDesc": salary,
            "experienceName": "3-5年",
            "degreeName": "本科",
            "locationName": "北京",
            "address": "海淀区",
            "longitude": 116.31,
            "latitude": 39.98,
            "showSkills": ["Python"],
            "postDescription": desc,
            "jobStatusDesc": "招聘中",
        },
        "bossInfo": {
            "name": "张先生",
            "title": "HR",
            "tiny": "https://img/a.jpg",
            "activeTimeDesc": active,
            "certificated": True,
        },
        "brandComInfo": {
            "brandName": "测试公司",
            "stageName": "不需要融资",
            "scaleName": "100-499人",
            "industryName": "互联网",
            "introduce": "简介",
            "labels": ["五险一金"],
            "activeTime": 1794837031064,
        },
    }


TEST_ACCOUNT = "acct_test"


def cleanup():
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM job_details WHERE lid=%s", (TEST_LID,))
            cur.execute("DELETE FROM detail_requests WHERE lid=%s", (TEST_LID,))
            cur.execute("DELETE FROM accounts WHERE account_id=%s", (TEST_ACCOUNT,))


def ensure_account():
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM accounts WHERE account_id=%s", (TEST_ACCOUNT,))
            if cur.fetchone():
                return
            cur.execute(
                "INSERT INTO accounts (account_id, label, boss_user_id, cdp_port, status) "
                "VALUES (%s, %s, '', 9999, 'expired')",
                (TEST_ACCOUNT, "测试账号"),
            )


@unittest.skipUnless(os.getenv("BOSS_TEST_DB") == "1", "需要 BOSS_TEST_DB=1 才跑数据库测试")
class TestDetailStore(unittest.TestCase):
    def setUp(self):
        db.init_db()
        cleanup()
        ensure_account()

    def tearDown(self):
        cleanup()

    def insert(self, zp, task_id="task_test"):
        item = flatten_detail(zp, TEST_LID, "sec-test")
        return db.insert_detail_snapshot(task_id, item)

    def test_insert_then_same_content_updates(self):
        self.assertEqual(self.insert(make_zp()), "inserted")
        latest = db.get_latest_detail(TEST_LID)
        self.assertEqual(latest["capture_count"], 1)
        first_at = latest["captured_at"]

        self.assertEqual(self.insert(make_zp()), "updated")
        latest = db.get_latest_detail(TEST_LID)
        self.assertEqual(latest["capture_count"], 2)
        self.assertGreater(latest["captured_at"], first_at)
        self.assertEqual(db.count_detail_versions(TEST_LID), 1)

    def test_volatile_field_is_refreshed_on_update(self):
        self.insert(make_zp(active="今日活跃"))
        self.insert(make_zp(active="刚刚活跃"))
        latest = db.get_latest_detail(TEST_LID)
        self.assertEqual(latest["boss_active_desc"], "刚刚活跃")
        self.assertEqual(latest["capture_count"], 2)
        self.assertEqual(db.count_detail_versions(TEST_LID), 1)

    def test_encrypt_id_lookup(self):
        self.assertFalse(db.has_detail_by_encrypt_id("enc-test"))
        self.insert(make_zp())
        self.assertTrue(db.has_detail_by_encrypt_id("enc-test"))
        self.assertFalse(db.has_detail_by_encrypt_id("enc-other"))

    def test_content_change_inserts_new_version(self):
        self.insert(make_zp(desc="职位描述 A"))
        self.assertEqual(self.insert(make_zp(desc="职位描述 B")), "inserted")
        self.assertEqual(db.count_detail_versions(TEST_LID), 2)
        latest = db.get_latest_detail(TEST_LID)
        self.assertEqual(latest["post_description"], "职位描述 B")

    def test_revert_inserts_again(self):
        self.insert(make_zp(desc="A"))
        self.insert(make_zp(desc="B"))
        self.assertEqual(self.insert(make_zp(desc="A")), "inserted")
        self.assertEqual(db.count_detail_versions(TEST_LID), 3)

    def test_request_log_and_block(self):
        now = datetime.now()
        db.log_detail_requests([
            {"account_id": TEST_ACCOUNT, "task_id": "t1", "lid": TEST_LID,
             "requested_at": now, "api_code": 0, "ok": True, "note": ""},
            {"account_id": TEST_ACCOUNT, "task_id": "t1", "lid": TEST_LID,
             "requested_at": now, "api_code": 37, "ok": False, "note": "risk"},
        ])
        self.assertEqual(db.count_recent_detail_requests(TEST_ACCOUNT, minutes=10), 2)
        db.set_account_detail_block(TEST_ACCOUNT, now)
        self.assertIsNotNone(db.get_account_detail_block(TEST_ACCOUNT))


if __name__ == "__main__":
    unittest.main()
