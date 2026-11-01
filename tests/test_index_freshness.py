"""The index status line must say how *old* the data is, not just HIT/MISS.

A HIT index can be a three-day-old one: nothing refreshes the index
automatically, and every query is answered correctly — about a three-day-old
world. v0.9.34 puts the coverage stamp on the one line every index-reading
command already prints, with a run hint once it trails the clock by over a
day (observed live: 2026-09-29 index found serving 2026-10-02 searches with
no hint anything was missing).
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from index_builder._schema import init_db, DB_PATH  # noqa: E402


def _load_recall():
    spec = importlib.util.spec_from_file_location(
        "sd_recall_freshness_under_test", str(SCRIPTS / "sd-recall.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class IndexFreshnessTest(unittest.TestCase):
    def setUp(self):
        self.recall = _load_recall()
        self.line = self.recall._index_status_line
        # conftest 临时库被同进程其他测试文件共用，每个用例自清表
        conn = sqlite3.connect(DB_PATH)
        try:
            init_db(conn)
            conn.execute("DELETE FROM sessions")
            conn.commit()
        finally:
            conn.close()

    def _insert(self, sid, iso):
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("INSERT INTO sessions (id, modified) VALUES (?, ?)", (sid, iso))
            conn.commit()
        finally:
            conn.close()

    def test_missing_db_reports_miss(self):
        # conftest 指向空临时目录：DB 文件还没建，必须是 MISS 而不是 HIT
        if DB_PATH.exists():
            self.skipTest("conftest data dir already initialized by another test module")
        self.assertIn("MISS", self.line())

    def test_empty_index_hit_without_coverage(self):
        self.assertEqual(self.line(), "HIT")

    def test_fresh_index_shows_coverage_without_hint(self):
        self._insert("s-fresh", _iso(datetime.now(timezone.utc)))
        line = self.line()
        self.assertIn("HIT", line)
        self.assertIn("data to", line)
        self.assertNotIn("stale", line)

    def test_stale_index_names_the_gap_and_the_fix(self):
        self._insert("s-stale", _iso(datetime.now(timezone.utc) - timedelta(days=3)))
        line = self.line()
        self.assertIn("3d stale", line)
        self.assertIn("run: index-builder.py build", line)

    def test_unparseable_timestamp_degrades_to_plain_hit(self):
        self._insert("s-junk", "not-a-timestamp")
        self.assertEqual(self.line(), "HIT")


if __name__ == "__main__":
    unittest.main()
