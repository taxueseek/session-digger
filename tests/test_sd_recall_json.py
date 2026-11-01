#!/usr/bin/env python3
"""--json output contract tests for sd-recall.py.

The JSON form feeds the DSH plugin's native tools (and any script
caller). The contract being pinned:

  1. single assembly — text and JSON render from the same
     ``_search_data`` / ``_sessions_data`` / ``_stats_data`` result,
     so they cannot drift: found count, session ids and totals agree
     by construction, and these tests prove it on a real-shaped
     fixture (the repo's standing war on mirror drift)
  2. envelope shape — every command emits a parseable object with
     the keys a tool caller reads
  3. clipping bounds — JSON gets the same 150/80/120-char windows
     the text renderer has always applied, never a wider one
  4. empty is a state — an empty search carries index status and
     facet counts in the envelope, mirroring the text hints
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPT_DIR))

from index_builder._cjk import split_cjk  # noqa: E402
from index_builder._schema import _SCHEMA_VERSION  # noqa: E402
import index_builder._reader as _reader_mod  # noqa: E402


def _load_sd_recall():
    """sd-recall.py has a hyphen in its name — load by path."""
    spec = importlib.util.spec_from_file_location(
        "sd_recall_json_under_test", _SCRIPT_DIR / "sd-recall.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ns(**overrides):
    """Namespace with every field the three refactored commands read."""
    base = dict(command="search", keyword="数据库", scope="all",
                limit=5, agent="cross", decisions=False, deep=False,
                include_tools=False, json=False)
    base.update(overrides)
    return argparse.Namespace(**base)


class JsonContractTest(unittest.TestCase):
    """One real-shaped fixture; text and JSON rendered from it."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.proj = self.root / "proj"
        self.proj.mkdir()
        self.db = self.root / "index.db"

        # A real transcript file: sessions freshness compares the
        # index's jsonl_mtime against the file's mtime.
        self.jsonl = self.proj / "a.jsonl"
        self.jsonl.write_text("[]", encoding="utf-8")

        conn = sqlite3.connect(str(self.db))
        conn.execute("""CREATE VIRTUAL TABLE messages_fts USING fts5(
            session_id UNINDEXED, role UNINDEXED, timestamp UNINDEXED, text,
            tokenize='unicode61')""")
        conn.execute("""CREATE TABLE sessions (
            id TEXT PRIMARY KEY, project_path TEXT, agent TEXT,
            created TEXT, modified TEXT, message_count INTEGER,
            user_messages INTEGER, assistant_messages INTEGER,
            tool_calls INTEGER, errors INTEGER, compactions INTEGER,
            total_tokens INTEGER, branch TEXT, summary TEXT,
            first_prompt TEXT, jsonl_mtime REAL, indexed_at REAL,
            jsonl_path TEXT, user_evidence_json TEXT,
            tool_errors_json TEXT)""")
        conn.execute("CREATE TABLE index_meta (key TEXT, value TEXT)")

        # 300-char user message: the JSON renderer must clip it to
        # the text renderer's 150-char window, no wider.
        long_text = "用户讨论了数据库迁移方案。" * 15
        evidence = json.dumps(
            [{"ts": "2026-10-01T10:00:00", "text": long_text,
              "decision": True}], ensure_ascii=False)
        conn.execute(
            """INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("test:sess_a", str(self.proj), "test",
             "2026-10-01T10:00:00", "2026-10-01T10:00:00", 5,
             3, 2, 1, 0, 0, 100, "main", "测试会话摘要",
             "first prompt text", self.jsonl.stat().st_mtime, 0.0,
             str(self.jsonl), evidence, "{}"))
        conn.execute(
            "INSERT INTO messages_fts VALUES (?,?,?,?)",
            ("test:sess_a", "USER", "2026-10-01T10:00:00",
             split_cjk("我们讨论了数据库迁移方案")))
        conn.execute(
            "INSERT INTO index_meta VALUES ('schema_version', ?)",
            (_SCHEMA_VERSION,))
        conn.commit()
        conn.close()

        self.sd = _load_sd_recall()
        # sd-recall and the canonical reader layer each bind their own
        # DB_PATH from index_builder._schema at import time; both must
        # point at the fixture or the two layers read different DBs.
        self._orig_sd_db = self.sd.DB_PATH
        self._orig_reader_db = _reader_mod.DB_PATH
        self.sd.DB_PATH = self.db
        _reader_mod.DB_PATH = self.db
        self.addCleanup(self._restore)

    def _restore(self):
        self.sd.DB_PATH = self._orig_sd_db
        _reader_mod.DB_PATH = self._orig_reader_db

    def _render_text(self, data):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.sd._print_search_text(data)
        return buf.getvalue()

    # ── 1. single assembly: text and JSON cannot drift ──────────

    def test_search_json_and_text_agree(self):
        data = self.sd._search_data(_ns())
        text = self._render_text(data)
        self.assertEqual(data["found"], 1)
        self.assertIn("Found 1 session(s)", text)
        row = data["sessions"][0]
        self.assertEqual(row["session_id"], "test:sess_a")
        self.assertIn("test:sess_a", text)
        self.assertEqual(row["messages"], 3)
        self.assertIn("Messages: 3", text)
        self.assertEqual(row["summary"], "测试会话摘要")
        self.assertIn("Summary: 测试会话摘要", text)

    def test_current_scope_matches_sessions_under_cwd(self):
        cwd = os.getcwd()
        os.chdir(self.proj)
        try:
            data = self.sd._search_data(_ns(scope="current"))
        finally:
            os.chdir(cwd)
        self.assertEqual(data["found"], 1)

    # ── 2. envelope shape ───────────────────────────────────────

    def test_cmd_search_json_envelope_parses(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.sd.cmd_search(_ns(json=True))
        envelope = json.loads(buf.getvalue())
        self.assertEqual(envelope["command"], "search")
        self.assertEqual(envelope["query"], "数据库")
        self.assertEqual(envelope["scope"], "all")
        self.assertEqual(envelope["found"], 1)
        self.assertIn("index", envelope)
        self.assertTrue(envelope["index"].startswith("HIT"))
        self.assertIn("elapsed_s", envelope)

    def test_sessions_envelope_has_tsv_columns(self):
        # keyword routes the listing through the fixture's FTS
        # index; without one the command enumerates the real
        # environment directories, which a hermetic test cannot
        # (and should not) point at.
        data = self.sd._sessions_data(
            argparse.Namespace(command="sessions", scope="all",
                               limit=10, agent="cross",
                               keyword="数据库", json=True))
        self.assertEqual(data["found"], 1)
        self.assertEqual(
            sorted(data["sessions"][0]),
            ["agent", "branch", "created", "messages", "modified",
             "path", "session_id"])

    def test_cmd_sessions_json_parses(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.sd.cmd_sessions(argparse.Namespace(
                command="sessions", scope="all", limit=10,
                agent="cross", keyword="数据库", json=True))
        envelope = json.loads(buf.getvalue())
        self.assertEqual(envelope["found"], 1)
        self.assertEqual(envelope["sessions"][0]["session_id"], "test:sess_a")

    def test_stats_envelope_matches_text_totals(self):
        data = self.sd._stats_data(
            argparse.Namespace(command="stats", agent="cross", json=False))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.sd.cmd_stats(argparse.Namespace(
                command="stats", agent="cross", json=False))
        text = buf.getvalue()
        self.assertIn(f"Total sessions: {data['sessions']}", text)
        self.assertIn(f"Total messages: {data['messages']}", text)
        self.assertEqual(data["sessions"], 1)
        self.assertEqual(data["messages"], 5)
        self.assertEqual(data["tool_calls"], 1)
        self.assertEqual(data["total_tokens"], 100)
        self.assertEqual(data["index"]["messages_indexed"], 1)

    def test_cmd_stats_json_parses(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.sd.cmd_stats(argparse.Namespace(
                command="stats", agent="cross", json=True))
        envelope = json.loads(buf.getvalue())
        self.assertEqual(envelope["command"], "stats")
        self.assertEqual(envelope["sessions"], 1)

    # ── 3. clipping bounds: JSON gets the text windows, not wider ─

    def test_json_clips_evidence_to_text_window(self):
        data = self.sd._search_data(_ns())
        evidence = data["sessions"][0]["evidence"]["user_messages"]
        self.assertEqual(len(evidence), 1)
        # Fixture message is 300 chars; the text renderer has always
        # shown 150 — the JSON form must not smuggle a wider window.
        self.assertEqual(len(evidence[0]["text"]), 150)

    def test_json_decisions_share_the_same_clip(self):
        data = self.sd._search_data(_ns(decisions=True))
        decisions = data["sessions"][0]["evidence"]["decisions"]
        self.assertEqual(len(decisions), 1)
        self.assertLessEqual(len(decisions[0]["text"]), 120)

    # ── 4. empty is a state, in both renderers ──────────────────

    def test_empty_search_json_carries_facts(self):
        data = self.sd._search_data(_ns(keyword="完全无关的词"))
        self.assertEqual(data["found"], 0)
        empty = data["empty"]
        self.assertEqual(empty["rejection"], "")
        self.assertEqual(empty["facet_counts"], {})
        self.assertEqual(empty["facet_total"], 0)
        self.assertTrue(empty["index"].startswith("HIT"))
        text = self._render_text(data)
        self.assertIn("No match for '完全无关的词'", text)

    def test_json_envelope_for_empty_search_parses(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.sd.cmd_search(_ns(keyword="完全无关的词", json=True))
        envelope = json.loads(buf.getvalue())
        self.assertEqual(envelope["found"], 0)
        self.assertEqual(envelope["sessions"], [])
        self.assertIn("empty", envelope)


if __name__ == "__main__":
    unittest.main()
