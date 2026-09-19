#!/usr/bin/env python3
"""Tool-result facet: what it recovers, and what it must not cost.

Measured 2026-09-17 on a 21.6 MB subagent trace: after deduplicating its 53
request snapshots the distinct evidence is 670 KB, of which 576 KB (86%) is tool
results — none of it reachable, because only the assistant's ``[TOOL: name]``
key line was indexed. The facet closes that hole; the same measurement set the
price, and these tests pin both sides:

  recovery   — a tool-only needle is reachable through the TOOL facet
  price      — the default result set must NOT contain TOOL rows, because
               ranking them in put 7 tool digests into the top 20 for a term
               that appears in command output (35% of the page) while
               recovering only 25 of 250 tool-only needles (10%)

The default-exclusion assertions are the ones with teeth: deleting the
``role != 'TOOL'`` filter in either search path turns them red.
"""
from __future__ import annotations

import importlib.util
import io
import contextlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from index_builder._builder import TOOL_DIGEST_CAP, _tool_fts_rows  # noqa: E402
from index_builder._cjk import split_cjk  # noqa: E402
from index_builder._schema import init_db  # noqa: E402


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "index_builder_cli", str(SCRIPTS / "index-builder.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_recall():
    spec = importlib.util.spec_from_file_location(
        "sd_recall_cli", str(SCRIPTS / "sd-recall.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ToolDigestRowsTest(unittest.TestCase):
    def test_prefix_names_the_tool_and_facet_is_tool(self):
        rows = _tool_fts_rows("s1", [{"name": "Bash", "timestamp": "t",
                                      "result_preview": "boom: missing module"}])
        self.assertEqual(len(rows), 1)
        sid, role, ts, text = rows[0]
        self.assertEqual((sid, role, ts), ("s1", "TOOL", "t"))
        self.assertTrue(text.startswith("[TOOL Bash] "), text)
        self.assertIn("missing module", text)

    def test_digest_is_capped(self):
        rows = _tool_fts_rows("s1", [{"name": "Read", "result_preview": "x" * 5000}])
        self.assertLessEqual(len(rows[0][3]), TOOL_DIGEST_CAP)

    def test_identical_previews_collapse(self):
        tools = [{"name": "Bash", "result_preview": "same output"},
                 {"name": "Bash", "result_preview": "same output"}]
        self.assertEqual(len(_tool_fts_rows("s1", tools)), 1)

    def test_placeholder_and_empty_previews_are_not_evidence(self):
        tools = [{"name": "Bash", "result_preview": "(no result captured)"},
                 {"name": "Bash", "result_preview": "   "},
                 {"name": "Bash"},
                 {"name": "Bash", "result_preview": "real output"}]
        rows = _tool_fts_rows("s1", tools)
        self.assertEqual(len(rows), 1)
        self.assertIn("real output", rows[0][3])

    def test_missing_tools_is_not_an_error(self):
        self.assertEqual(_tool_fts_rows("s1", None), [])
        self.assertEqual(_tool_fts_rows("s1", []), [])


class _FacetIndex:
    """A throwaway index carrying one prose hit and one tool-only hit."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "index.db"
        conn = sqlite3.connect(str(self.db))
        init_db(conn)
        conn.execute(
            "INSERT INTO sessions (id, agent, project_name, model, message_count, jsonl_path)"
            " VALUES (?,?,?,?,?,?)", ("s-prose", "claude", "GPT", "m-1", 3, "/tmp/s-prose.jsonl"))
        conn.execute(
            "INSERT INTO sessions (id, agent, project_name, model, message_count, jsonl_path)"
            " VALUES (?,?,?,?,?,?)", ("s-tool", "zcode_v2", "GPT", "m-2", 2, "/tmp/s-tool.jsonl"))
        conn.execute(
            "INSERT INTO messages_fts (session_id, role, timestamp, text) VALUES (?,?,?,?)",
            ("s-prose", "ASSISTANT", "t1", split_cjk("我们讨论了唯一针 needleprose")))
        conn.execute(
            "INSERT INTO messages_fts (session_id, role, timestamp, text) VALUES (?,?,?,?)",
            ("s-tool", "TOOL", "t2", split_cjk("[TOOL Bash] 唯一针 needleshell")))
        conn.commit()
        conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.tmp.cleanup()


class SearchFacetDefaultTest(unittest.TestCase):
    def test_default_result_set_excludes_tool_rows(self):
        cli = _load_cli()
        with _FacetIndex() as fx:
            orig = cli.DB_PATH
            cli.DB_PATH = fx.db
            try:
                hits = cli.search_fts("needle", limit=10)
            finally:
                cli.DB_PATH = orig
        self.assertTrue(hits)
        self.assertNotIn("TOOL", {h["facet"] for h in hits})

    def test_tool_only_returns_just_the_facet(self):
        cli = _load_cli()
        with _FacetIndex() as fx:
            orig = cli.DB_PATH
            cli.DB_PATH = fx.db
            try:
                hits = cli.search_fts("needle", limit=10, tools_only=True)
            finally:
                cli.DB_PATH = orig
        self.assertTrue(hits)
        self.assertEqual({"TOOL"}, {h["facet"] for h in hits})

    def test_include_tools_yields_both_facets(self):
        cli = _load_cli()
        with _FacetIndex() as fx:
            orig = cli.DB_PATH
            cli.DB_PATH = fx.db
            try:
                hits = cli.search_fts("needle", limit=10, include_tools=True)
            finally:
                cli.DB_PATH = orig
        self.assertEqual({"TOOL", "ASSISTANT"}, {h["facet"] for h in hits})

    def test_hit_carries_the_session_identity_fields(self):
        """One query answers "which session, and does it count".

        Before this, a hit was session_id/role/timestamp/text/score: agent,
        project, model and outcome each cost a follow-up lookup.
        """
        cli = _load_cli()
        with _FacetIndex() as fx:
            orig = cli.DB_PATH
            cli.DB_PATH = fx.db
            try:
                hits = cli.search_fts("needleprose", limit=5)
            finally:
                cli.DB_PATH = orig
        hit = hits[0]
        self.assertEqual(hit["agent"], "claude")
        self.assertEqual(hit["project"], "GPT")
        self.assertEqual(hit["model"], "m-1")
        self.assertEqual(hit["messages"], 3)


class RecallSearchFacetTest(unittest.TestCase):
    def test_sd_recall_default_excludes_tool_sessions(self):
        recall = _load_recall()
        with _FacetIndex() as fx:
            orig = recall.DB_PATH
            recall.DB_PATH = fx.db
            try:
                default = recall._fts_search("needle", limit=10)
                widened = recall._fts_search("needle", limit=10, include_tools=True)
            finally:
                recall.DB_PATH = orig
        self.assertEqual(["s-prose"], [r[0] for r in default])
        self.assertEqual({"s-prose", "s-tool"}, {r[0] for r in widened})


class EmptySearchReportTest(unittest.TestCase):
    """An empty result must read as a state, not as "it never happened".

    Live case that motivated it: ``第一性原理`` returns 0 sessions under
    ``--scope current`` while the index holds 1960 rows for the term, and the
    old one-liner printed "No matching sessions found" — indistinguishable from
    a genuine absence.
    """

    def _run(self, counts, scope="current"):
        recall = _load_recall()
        buf = io.StringIO()
        orig = recall._facet_counts
        recall._facet_counts = lambda kw: counts
        try:
            with contextlib.redirect_stdout(buf):
                recall.report_empty_search("第一性原理", scope)
        finally:
            recall._facet_counts = orig
        return buf.getvalue()

    def test_reports_scope_index_and_widening(self):
        out = self._run({"USER": 1287, "ASSISTANT": 673})
        self.assertIn("scope 'current'", out)
        self.assertIn("Index:", out)
        self.assertIn("1960 row(s) elsewhere", out)
        self.assertIn("--scope all", out)

    def test_names_the_tool_facet_when_it_holds_the_only_hits(self):
        out = self._run({"TOOL": 9})
        self.assertIn("tool result digests", out)
        self.assertIn("--tools-only", out)

    def test_scope_all_does_not_tell_you_to_widen(self):
        out = self._run({"USER": 3}, scope="all")
        self.assertNotIn("--scope all", out)

    def test_total_absence_says_so_without_pretending_to_widen(self):
        out = self._run({})
        self.assertIn("no row for this term in any scope", out)
        self.assertNotIn("--scope all", out)


if __name__ == "__main__":
    unittest.main()
