#!/usr/bin/env python3
"""Tests for index_builder._reader and deep_analyze (index-only data pack)."""
import importlib.util
import json
import datetime as dt
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from index_builder._schema import DB_PATH as _REAL_DB, init_db  # noqa: E402
from index_builder._cjk import split_cjk  # noqa: E402


def _load_module(name, relpath):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class ReaderPackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        cls.db = Path(cls._td.name) / "index.db"
        _REAL_DB.__class__  # noqa: B018 — keep import used
        import index_builder._schema as schema
        import index_builder._reader as reader
        cls._orig = (schema.DB_PATH, reader.DB_PATH)
        schema.DB_PATH = cls.db
        reader.DB_PATH = cls.db
        conn = sqlite3.connect(str(cls.db))
        init_db(conn)
        # Two sessions: one heavy clean one, one noise-heavy, one stale agent.
        # relative dates: day-window tests must not rot as wall-clock moves
        def _iso(**kw):
            return (dt.datetime.now() - dt.timedelta(**kw)).isoformat(timespec="seconds")

        rows = [
            ("zcode:sess_a", "zcode", "/tmp/a.jsonl", _iso(hours=12),
             60, 40, 20, 5, 2, "做了半调海报技能 v1.8"),
            ("zcode:sess_b", "zcode", "/tmp/b.jsonl", _iso(hours=36),
             10, 6, 4, 12, 5, "琐碎尝试"),
            ("grok:sess_c", "grok", "/tmp/c.jsonl", _iso(days=30),
             30, 10, 20, 8, 1, "argo PR review"),
        ]
        for sid, agent, path, created, mc, um, am, tc, er, summ in rows:
            conn.execute(
                """INSERT INTO sessions (id, agent, jsonl_path, created, message_count,
                   user_messages, assistant_messages, tool_calls, errors, summary,
                   tool_errors_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (sid, agent, path, created, mc, um, am, tc, er, summ, '{"Bash": 3}'))
            conn.execute("INSERT INTO messages_fts VALUES (?,?,?,?)",
                         (sid, "USER", _iso(hours=12) + "Z",
                          split_cjk("为海报技能做减法 " if "a" in sid else "<notification> 系统注入")))
        conn.commit()
        conn.close()
        cls.reader = reader

    @classmethod
    def tearDownClass(cls):
        import index_builder._schema as schema
        import index_builder._reader as reader
        schema.DB_PATH, reader.DB_PATH = cls._orig
        cls._td.cleanup()

    def test_recent_sessions_quality_order_and_agent_filter(self):
        got = self.reader.recent_sessions(days=0, limit=3)
        self.assertEqual(got[0]["id"], "zcode:sess_a", "heaviest first")
        got = self.reader.recent_sessions(days=0, agent="grok")
        self.assertEqual([s["id"] for s in got], ["grok:sess_c"])

    def test_recent_sessions_days_window(self):
        got = self.reader.recent_sessions(days=2, limit=10)
        self.assertEqual([s["id"] for s in got], ["zcode:sess_a", "zcode:sess_b"])

    def test_recent_sessions_keyword_fts(self):
        # sample text is "为海报技能做减法" — phrase query "海 报" must hit only sess_a
        got = self.reader.recent_sessions(days=0, keyword="海报", limit=5)
        self.assertEqual([s["id"] for s in got], ["zcode:sess_a"])

    def test_global_aggregates_shape(self):
        aggs = self.reader.global_aggregates(days=0)
        self.assertEqual(aggs["totals"]["sessions"], 3)
        self.assertTrue(any(e["agent"] == "zcode" for e in aggs["envs"]))

    def test_global_aggregates_window_scoping(self):
        # days=2 → only sessions created within the window count in totals;
        # the old bug was totals ignoring the window entirely.
        aggs = self.reader.global_aggregates(days=2)
        self.assertEqual(aggs["totals"]["sessions"], 2)
        self.assertTrue(aggs["daily"], "windowed call includes per-day breakdown")

    def test_has_summary_cache_skips_virtual(self):
        got = self.reader.summary_cache_info(["zcode:sess_a", "grok:sess_c"])
        self.assertEqual(got, {}, "no cache files written in tmp")

    def test_summary_cache_info_reads_records(self):
        # Write a real .summary.jsonl next to a file-backed session, then
        # verify count/latest fields and mtime-based freshness signal.
        da = _load_module("deep_analyze_cache", "deep_analyze.py")
        import os
        sess_file = "/tmp/a.jsonl"  # matches zcode:sess_a jsonl_path in setUpClass
        Path(sess_file).write_text('{"type":"user"}\n', encoding="utf-8")
        sp = sess_file + ".summary.jsonl"
        mtime = os.path.getmtime(sess_file)
        Path(sp).write_text(
            json.dumps({"schema": "session-digger-summary/v1",
                        "analyzed_at": "2026-09-06T10:00:00",
                        "query_intent": "deep-analyze",
                        "source_mtime": mtime - 100,  # session grew after
                        "analysis": "旧结论要点"}) + "\n" +
            json.dumps({"schema": "session-digger-summary/v1",
                        "analyzed_at": "2026-09-07T09:00:00",
                        "query_intent": "recap",
                        "source_mtime": mtime,  # fresh analysis
                        "analysis": "新结论要点"}) + "\n", encoding="utf-8")
        self.addCleanup(lambda: (os.remove(sp), os.remove(sess_file)))
        conn = sqlite3.connect(str(self.db))
        conn.execute("UPDATE sessions SET jsonl_mtime = ? WHERE id = 'zcode:sess_a'",
                     (mtime + 200,))  # session file newer than latest analysis
        conn.commit()
        conn.close()

        got = self.reader.summary_cache_info(["zcode:sess_a"])
        self.assertEqual(got["zcode:sess_a"]["count"], 2)
        self.assertEqual(got["zcode:sess_a"]["latest_intent"], "recap")
        self.assertIn("新结论要点", got["zcode:sess_a"]["latest_analysis"])

        pack, err = da.build_pack(days=0, agent="zcode", keyword=None, top=2,
                                  sort="quality", min_messages=2, budget=20000)
        self.assertIsNone(err)
        self.assertIn("先向用户展示以上结论并询问是否重新分析", pack,
                      "stale-cache wording must ask the user, never auto-skip")
        self.assertIn("⚠ 会话在此分析之后又有新内容", pack,
                      "grew-after-analysis must be flagged for re-analysis")

    def test_deep_analyze_pack_filters_injected_lines(self):
        da = _load_module("deep_analyze_under_test", "deep_analyze.py")
        pack, err = da.build_pack(days=0, agent=None, keyword=None, top=5,
                                  sort="quality", min_messages=2, budget=20000)
        self.assertIsNone(err)
        self.assertIn("全局概况", pack)
        self.assertIn("回存接口", pack)
        self.assertIn("做了半调海报技能", pack)
        self.assertNotIn("<notification>", pack.split("回存接口")[0],
                         "injected system lines must not appear as user samples")

    def test_deep_analyze_budget_bounds(self):
        da = _load_module("deep_analyze_bounded", "deep_analyze.py")
        pack, err = da.build_pack(days=0, agent=None, keyword=None, top=5,
                                  sort="quality", min_messages=2, budget=2500)
        self.assertIsNone(err)
        self.assertLess(len(pack), 4000, "budget keeps the pack bounded")


if __name__ == "__main__":
    unittest.main()
