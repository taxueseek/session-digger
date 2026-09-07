#!/usr/bin/env python3
"""Rebuild scoping tests: `--rebuild --agent X` must not wipe other envs.

Regression for a real incident: benchmarking with --rebuild --agent zcode
deleted every other environment's rows (DELETE FROM sessions was global).
"""
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_BUILDER = _SCRIPTS / "index_builder" / "_builder.py"

echolib_spec = importlib.util.spec_from_file_location(
    "echolib", str(_SCRIPTS / "echolib" / "__init__.py"))
echolib_mod = importlib.util.module_from_spec(echolib_spec)
sys.modules["echolib"] = echolib_mod
echolib_spec.loader.exec_module(echolib_mod)

_pkg = _SCRIPTS / "index_builder"
ib_init = importlib.util.spec_from_file_location(
    "index_builder", str(_pkg / "__init__.py"),
    submodule_search_locations=[str(_pkg)])
ib_mod = importlib.util.module_from_spec(ib_init)
sys.modules["index_builder"] = ib_mod
ib_init.loader.exec_module(ib_mod)

_spec = importlib.util.spec_from_file_location(
    "index_builder._builder", str(_BUILDER),
    submodule_search_locations=[])
builder = importlib.util.module_from_spec(_spec)
sys.modules["index_builder._builder"] = builder
_spec.loader.exec_module(builder)


class RebuildScopeTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)

        # One parseable claude-format session (env A) + one bare row for env B.
        proj = root / "proj-a"
        proj.mkdir()
        self.session_a = proj / "abc123.jsonl"
        self.session_a.write_text(
            json.dumps({"type": "user", "timestamp": "2026-09-07T10:00:00Z",
                        "message": {"role": "user", "content": "hello world"}}) + "\n",
            encoding="utf-8")

        self.db = root / "index.db"
        self._orig_db = builder.DB_PATH
        self._orig_dbdir = builder.DB_DIR
        builder.DB_PATH = self.db
        builder.DB_DIR = root
        self.addCleanup(setattr, builder, "DB_PATH", self._orig_db)
        self.addCleanup(setattr, builder, "DB_DIR", self._orig_dbdir)

        conn = sqlite3.connect(str(self.db))
        builder.init_db(conn)
        conn.execute(
            "INSERT INTO sessions (id, agent, jsonl_path) VALUES (?,?,?)",
            ("claude:other", "claude", str(root / "elsewhere.jsonl")))
        conn.execute(
            "INSERT INTO sessions (id, agent, jsonl_path) VALUES (?,?,?)",
            ("grok:s1", "grok", str(root / "grok-x.jsonl")))
        conn.execute("INSERT INTO messages_fts VALUES ('grok:s1','USER','t','legacy text')")
        conn.commit()
        conn.close()

    def _run(self, entries, rebuild, agent_filter):
        orig_scan = builder.scan_sessions
        builder.scan_sessions = lambda flt="cross": entries
        try:
            return builder.build_index(rebuild=rebuild, agent_filter=agent_filter)
        finally:
            builder.scan_sessions = orig_scan

    def test_scoped_rebuild_keeps_other_agents(self):
        entries = [(f"claude:{self.session_a.stem}", str(self.session_a), "claude")]
        result = self._run(entries, rebuild=True, agent_filter="claude")
        self.assertEqual(result["indexed"], 1)

        conn = sqlite3.connect(str(self.db))
        agents = dict(conn.execute("SELECT agent, COUNT(*) FROM sessions GROUP BY agent"))
        fts_ids = {r[0] for r in conn.execute("SELECT session_id FROM messages_fts")}
        conn.close()
        self.assertEqual(agents.get("grok"), 1, "grok rows must survive a claude-scoped rebuild")
        self.assertIn("grok:s1", fts_ids, "grok FTS rows must survive")
        self.assertNotIn("legacy text", "", "sanity")

    def test_full_rebuild_clears_everything(self):
        entries = [(f"claude:{self.session_a.stem}", str(self.session_a), "claude")]
        self._run(entries, rebuild=True, agent_filter="cross")
        conn = sqlite3.connect(str(self.db))
        agents = dict(conn.execute("SELECT agent, COUNT(*) FROM sessions GROUP BY agent"))
        conn.close()
        self.assertEqual(agents, {"claude": 1}, "full rebuild replaces the whole table")

    def test_scoped_rebuild_drops_own_stale_fts(self):
        # A stale FTS row pointing at the rebuilt env must not survive.
        conn = sqlite3.connect(str(self.db))
        conn.execute("INSERT INTO messages_fts VALUES (?,?,?,?)",
                     (f"claude:{self.session_a.stem}", "USER", "t", "stale row"))
        conn.commit()
        conn.close()
        entries = [(f"claude:{self.session_a.stem}", str(self.session_a), "claude")]
        self._run(entries, rebuild=True, agent_filter="claude")
        conn = sqlite3.connect(str(self.db))
        stale = conn.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE text = 'stale row'").fetchone()[0]
        conn.close()
        self.assertEqual(stale, 0, "stale FTS rows of the rebuilt env must be purged")


if __name__ == "__main__":
    unittest.main()
