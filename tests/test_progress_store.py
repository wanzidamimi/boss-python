"""进度旁路存储的红绿测试。"""

import tempfile
import unittest
from pathlib import Path

import progress_store as ps


class TestProgressStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_and_read(self):
        ps.write_progress("t1", base_dir=self.base, phase="detail", done=3, total=10)
        data = ps.read_progress("t1", base_dir=self.base)
        self.assertEqual(data["phase"], "detail")
        self.assertEqual(data["done"], 3)
        self.assertEqual(data["total"], 10)
        self.assertEqual(data["task_id"], "t1")
        self.assertIn("updated_at", data)

    def test_update_preserves_existing_fields(self):
        ps.write_progress("t2", base_dir=self.base, phase="list", pages=5, page=1)
        ps.write_progress("t2", base_dir=self.base, page=2)
        data = ps.read_progress("t2", base_dir=self.base)
        self.assertEqual(data["phase"], "list")
        self.assertEqual(data["pages"], 5)
        self.assertEqual(data["page"], 2)

    def test_missing_returns_none(self):
        self.assertIsNone(ps.read_progress("nope", base_dir=self.base))

    def test_empty_task_id_is_noop(self):
        self.assertIsNone(ps.read_progress("", base_dir=self.base))
        self.assertEqual(ps.write_progress("", base_dir=self.base, phase="x"), {})

    def test_no_tmp_file_left(self):
        ps.write_progress("t3", base_dir=self.base, phase="done")
        self.assertFalse(list(self.base.glob("*.tmp")))

    def test_append_event_keeps_tail(self):
        events = []
        for i in range(20):
            events = ps.append_event({"events": events}, f"e{i}", limit=5)
        self.assertEqual(events, ["e15", "e16", "e17", "e18", "e19"])

    def test_cleanup_removes_old_files(self):
        import os
        import time

        ps.write_progress("old", base_dir=self.base, phase="done")
        ps.write_progress("new", base_dir=self.base, phase="done")
        old_path = ps.progress_path("old", self.base)
        os.utime(old_path, (time.time() - 10 * 86400,) * 2)
        removed = ps.cleanup_progress(days=7, base_dir=self.base)
        self.assertEqual(removed, 1)
        self.assertIsNone(ps.read_progress("old", self.base))
        self.assertIsNotNone(ps.read_progress("new", self.base))


if __name__ == "__main__":
    unittest.main()
