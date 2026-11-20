"""布隆去重逻辑的红绿测试（用内存假后端，不依赖 Redis）。"""

import unittest
from datetime import datetime

from detail_dedupe import DetailDedupe


class FakeBackend:
    def __init__(self):
        self.filters = {}
        self.ttls = {}

    def exists_many(self, key, members):
        bucket = self.filters.get(key, set())
        return [member in bucket for member in members]

    def add_many(self, key, members):
        self.filters.setdefault(key, set()).update(members)

    def expire(self, key, seconds):
        self.ttls[key] = seconds

    def boom(self, *_):
        raise ConnectionError("redis down")


def day(text):
    return datetime.strptime(text, "%Y-%m-%d")


class TestDetailDedupe(unittest.TestCase):
    def make(self, ttl_days=1, today="2026-11-20", backend=None, enabled=True):
        return DetailDedupe(
            backend=backend if backend is not None else FakeBackend(),
            ttl_days=ttl_days,
            prefix="test:bf:",
            today_provider=lambda: day(today),
            enabled=enabled,
        )

    def test_fresh_item_should_fetch(self):
        dedupe = self.make()
        self.assertTrue(dedupe.should_fetch("job-1", confirm=lambda _: True))

    def test_marked_item_skipped_when_db_confirms(self):
        dedupe = self.make()
        dedupe.mark("job-1")
        self.assertTrue(dedupe.seen("job-1"))
        self.assertFalse(dedupe.should_fetch("job-1", confirm=lambda _: True))
        self.assertEqual(dedupe.bucket_keys()[0], "test:bf:20261120")
        self.assertEqual(dedupe.backend.ttls["test:bf:20261120"], 86400)

    def test_false_positive_is_still_fetched(self):
        dedupe = self.make()
        dedupe.mark("job-1")
        # 布隆说见过，但 MySQL 里没有 -> 可能是误判，仍应抓取
        self.assertTrue(dedupe.should_fetch("job-1", confirm=lambda _: False))

    def test_missing_confirm_does_not_skip(self):
        dedupe = self.make()
        dedupe.mark("job-1")
        self.assertTrue(dedupe.should_fetch("job-1", confirm=None))

    def test_bucket_rolls_after_ttl(self):
        backend = FakeBackend()
        dedupe = self.make(ttl_days=1, today="2026-11-20", backend=backend)
        dedupe.mark("job-1")
        next_day = DetailDedupe(
            backend=backend, ttl_days=1, prefix="test:bf:",
            today_provider=lambda: day("2026-11-21"),
        )
        self.assertFalse(next_day.seen("job-1"))
        self.assertTrue(next_day.should_fetch("job-1", confirm=lambda _: True))

    def test_two_day_window_keeps_yesterday(self):
        backend = FakeBackend()
        dedupe = self.make(ttl_days=2, today="2026-11-20", backend=backend)
        dedupe.mark("job-1")
        next_day = DetailDedupe(
            backend=backend, ttl_days=2, prefix="test:bf:",
            today_provider=lambda: day("2026-11-21"),
        )
        self.assertTrue(next_day.seen("job-1"))
        self.assertEqual(len(next_day.bucket_keys()), 2)

    def test_backend_failure_fail_open(self):
        backend = FakeBackend()
        dedupe = self.make(backend=backend)
        backend.exists_many = backend.boom
        backend.add_many = backend.boom
        self.assertFalse(dedupe.seen("job-1"))
        self.assertTrue(dedupe.should_fetch("job-1", confirm=lambda _: True))
        dedupe.mark("job-1")  # 不应抛错

    def test_disabled_always_fetches(self):
        backend = FakeBackend()
        dedupe = self.make(backend=backend, enabled=False)
        dedupe.mark("job-1")
        self.assertEqual(backend.filters, {})
        self.assertTrue(dedupe.should_fetch("job-1", confirm=lambda _: True))

    def test_mark_many_skips_empty_ids(self):
        dedupe = self.make()
        dedupe.mark_many(["a", "", None, "b"])
        self.assertEqual(dedupe.backend.filters["test:bf:20261120"], {"a", "b"})


if __name__ == "__main__":
    unittest.main()
