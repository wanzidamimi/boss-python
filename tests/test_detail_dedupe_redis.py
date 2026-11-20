"""RedisBloom 集成测试（需要真实 Redis，设置 BOSS_TEST_REDIS=1 才运行）。"""

import os
import unittest
import uuid
from datetime import datetime

from detail_dedupe import DetailDedupe, RedisBloomBackend

REDIS_URL = os.getenv("BOSS_REDIS_URL", "redis://127.0.0.1:6380/0")


@unittest.skipUnless(os.getenv("BOSS_TEST_REDIS") == "1",
                     "需要 BOSS_TEST_REDIS=1 且 Redis 可用")
class TestRedisBloom(unittest.TestCase):
    def setUp(self):
        self.prefix = f"test:bf:{uuid.uuid4().hex[:8]}:"
        self.backend = RedisBloomBackend(REDIS_URL)
        self.dedupe = DetailDedupe(
            backend=self.backend,
            ttl_days=1,
            prefix=self.prefix,
            today_provider=lambda: datetime(2026, 11, 20),
        )

    def tearDown(self):
        for key in self.backend.client.keys(self.prefix + "*"):
            self.backend.client.delete(key)

    def test_mark_seen_and_ttl(self):
        self.assertFalse(self.dedupe.seen("job-x"))
        self.dedupe.mark("job-x")
        self.assertTrue(self.dedupe.seen("job-x"))

        key = self.prefix + "20261120"
        self.assertGreater(self.backend.client.ttl(key), 0)

    def test_should_fetch_with_confirm(self):
        self.dedupe.mark("job-x")
        self.assertFalse(self.dedupe.should_fetch("job-x", confirm=lambda _: True))
        self.assertTrue(self.dedupe.should_fetch("job-x", confirm=lambda _: False))

    def test_filter_is_reserved_with_capacity(self):
        self.dedupe.mark("job-x")
        key = self.prefix + "20261120"
        info = self.backend.client.execute_command("BF.INFO", key)
        # redis-py 在 RESP3 下可能返回 dict，RESP2 下是 [字段, 值, ...]
        if isinstance(info, dict):
            capacity = info.get("Capacity")
        else:
            text = [str(item) for item in info]
            capacity = text[text.index("Capacity") + 1]
        self.assertGreater(int(capacity), 0)

    def test_rollover_creates_new_bucket(self):
        self.dedupe.mark("job-x")
        next_day = DetailDedupe(
            backend=self.backend, ttl_days=1, prefix=self.prefix,
            today_provider=lambda: datetime(2026, 11, 21),
        )
        self.assertFalse(next_day.seen("job-x"))
        next_day.mark("job-y")
        self.assertTrue(next_day.seen("job-y"))
        self.assertTrue(self.backend.client.exists(self.prefix + "20261121"))


class TestRedisUnavailable(unittest.TestCase):
    def test_bad_url_fails_open(self):
        backend = RedisBloomBackend("redis://127.0.0.1:6399/0")
        dedupe = DetailDedupe(backend=backend, prefix="test:bf:", ttl_days=1)
        self.assertFalse(dedupe.seen("job-x"))
        self.assertTrue(dedupe.should_fetch("job-x", confirm=lambda _: True))
        dedupe.mark("job-x")  # 不应抛错


if __name__ == "__main__":
    unittest.main()
