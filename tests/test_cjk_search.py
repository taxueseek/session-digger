#!/usr/bin/env python3
"""CJK retrieval contract tests.

The index stores messages_fts.text in split-CJK form (index_builder._cjk),
so a Chinese substring query only hits when WRITE side splits and QUERY side
rebuilds the per-character phrase. These tests pin all three sides:

  1. _cjk unit behavior (split / match-query / uncjk round-trip)
  2. query safety (FTS5 operators and punctuation cannot inject)
  3. sd-recall semantics ([] = genuine miss, never a file-scan fallback;
     None = index missing, fallback allowed)
  4. gold queries against a real-shaped FTS db — the minimum recall bar
     a rebuild must keep green (argo-style gate).
"""
import os
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPT_DIR))

from index_builder._cjk import build_match_query, split_cjk, uncjk  # noqa: E402


class CjkTokenizeTest(unittest.TestCase):
    def test_split_cjk_inserts_spaces_inside_runs_only(self):
        self.assertEqual(split_cjk("数据库迁移"), "数 据 库 迁 移")
        self.assertEqual(split_cjk("use git 迁移 ok"), "use git 迁 移 ok")
        self.assertEqual(split_cjk(""), "")
        self.assertEqual(split_cjk("plain ascii"), "plain ascii")

    def test_uncjk_round_trip(self):
        raw = "数据库迁移完成，切换到 PostgreSQL use git ok"
        self.assertEqual(uncjk(split_cjk(raw)), raw)

    def test_build_match_query_cjk_phrase(self):
        self.assertEqual(build_match_query("迁移"), '"迁 移"')
        self.assertEqual(build_match_query("性能优化"), '"性 能 优 化"')

    def test_build_match_query_ascii_prefix(self):
        self.assertEqual(build_match_query("Postgre"), '"Postgre"*')

    def test_build_match_query_mixed_drops_operators(self):
        # FTS5 boolean keywords are quoted into literal tokens, so the query
        # can never act as syntax (injection-proof); punctuation is dropped.
        q = build_match_query('数据库 OR 迁移" NEAR (foo:bar)')
        self.assertEqual(q, '"数 据 库" "OR"* "迁 移" "NEAR"* "foo"* "bar"*')
        # Every term is fully quoted — no bare FTS5 syntax can survive.
        self.assertRegex(q, r'^("[^"]*"\*?)( "[^"]*"\*?)*$')

    def test_build_match_query_matches_as_literal_in_fts(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(text, tokenize='unicode61')")
        conn.execute("INSERT INTO t VALUES (?)", (split_cjk("我们讨论了 OR 语法"),))
        # A bare "OR*" is fts5 syntax error; the quoted rewrite must match textually.
        n = conn.execute("SELECT count(*) FROM t WHERE t MATCH ?",
                         (build_match_query("OR"),)).fetchone()[0]
        self.assertEqual(n, 1)
        n = conn.execute("SELECT count(*) FROM t WHERE t MATCH ?",
                         (build_match_query('OR" NEAR (x:y'),)).fetchone()[0]
        self.assertEqual(n, 0, "multi-term AND query over absent terms misses, no crash")

    def test_build_match_query_empty_returns_none(self):
        self.assertIsNone(build_match_query(""))
        self.assertIsNone(build_match_query("   "))
        self.assertIsNone(build_match_query('*** ::: """'))
        self.assertIsNone(build_match_query(None))

    def test_build_match_query_caps_terms(self):
        q = build_match_query("a b c d e f g h i j k l m n o p")
        self.assertEqual(q.count('"*'), 12)


class FtsSearchSemanticsTest(unittest.TestCase):
    """sd-recall._fts_search against a real-shaped temp FTS db."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.db = Path(self._td.name) / "index.db"
        conn = sqlite3.connect(str(self.db))
        conn.execute("""CREATE VIRTUAL TABLE messages_fts USING fts5(
            session_id UNINDEXED, role UNINDEXED, timestamp UNINDEXED, text,
            tokenize='unicode61')""")
        conn.execute("""CREATE TABLE sessions (
            id TEXT PRIMARY KEY, agent TEXT, jsonl_path TEXT)""")
        rows = [
            ("zcode:sess_a", "claude 会话讲了数据库迁移方案"),
            ("zcode:sess_b", "今天只聊了午饭吃什么"),
            ("grok:x", "refactor the migration logic"),
        ]
        for sid, text in rows:
            conn.execute("INSERT INTO messages_fts VALUES (?,?,?,?)",
                         (sid, "USER", "2026-09-07T10:00:00", split_cjk(text)))
            conn.execute("INSERT INTO sessions VALUES (?,?,?)",
                         (sid, sid.split(":")[0], f"/tmp/{sid}.jsonl"))
        conn.commit()
        conn.close()

        # sd-recall.py has a hyphen in its name — load by path.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "sd_recall_under_test", _SCRIPT_DIR / "sd-recall.py")
        self._sd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self._sd)
        self._orig_db = self._sd.DB_PATH
        self._sd.DB_PATH = self.db
        self.addCleanup(setattr, self._sd, "DB_PATH", self._orig_db)

    def test_gold_chinese_queries_hit(self):
        for kw in ["迁移", "数据库", "迁移方案"]:
            got = self._sd._fts_search(kw, limit=5)
            self.assertIsInstance(got, list, f"{kw!r}: index answered, not fallback")
            self.assertTrue(any(sid == "zcode:sess_a" for sid, _p, _a in got),
                            f"{kw!r} must hit zcode:sess_a, got {got}")

    def test_genuine_miss_returns_empty_list_not_none(self):
        got = self._sd._fts_search("完全无关的词", limit=5)
        self.assertEqual(got, [], "miss must be [] so find_sessions does not rescan files")

    def test_injection_query_does_not_crash(self):
        got = self._sd._fts_search('" OR 1=1 --', limit=5)
        self.assertEqual(got, [])

    def test_ascii_prefix_hits(self):
        got = self._sd._fts_search("migrat", limit=5)
        self.assertTrue(any(sid == "grok:x" for sid, _p, _a in got))

    def test_missing_index_returns_none(self):
        self._sd.DB_PATH = Path(self._td.name) / "nonexistent.db"
        self.assertIsNone(self._sd._fts_search("迁移", limit=5))


if __name__ == "__main__":
    unittest.main()
