#!/usr/bin/env python3
"""A filter must narrow the *selection*, not sweep its output.

Both defects here are false negatives with the same shape: a filter was applied
after the query had already been truncated, so the answer read as "that does not
exist" instead of "that was outside the page I looked at".

  * ``--scope current`` (the documented default) filtered the 20 distinct
    sessions BM25 had already ranked. Measured 2026-09-17 in this project:
    ``记忆`` has 51 in-project sessions and ``agent`` has 174, and neither term
    had a single one in the top 20 — both printed "No match ... in scope
    'current'" while ``--scope all`` returned 584 and 3567. The agent narrow had
    the same shape on a different axis.
  * The empty-result report counted ``messages_fts`` rows without the
    ``sessions`` join that ``_fts_search`` needs to return a path, so it could
    promise rows that widening the scope also cannot reach (4,542 such orphan
    rows were measured before a rebuild cleaned them up).

The default-exclusion assertions are the ones with teeth: putting the cwd or
agent filter back after ``_fts_search`` turns them red.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from index_builder._cjk import split_cjk  # noqa: E402
from index_builder._schema import init_db  # noqa: E402

NEEDLE = "needleword"


def _load_recall():
    spec = importlib.util.spec_from_file_location(
        "sd_recall_scope_under_test", str(SCRIPTS / "sd-recall.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _ProjectIndex:
    """An index whose in-project session ranks *below* a page of outsiders.

    Rank order is the whole point. BM25 normalizes by document length, so the
    outsiders win by term frequency on a short document (10 occurrences) while
    the project session carries a single occurrence buried in a longer one —
    every outsider ranks first, and the old post-filter saw a page with no
    project session in it at all.
    """

    OUTSIDERS = 25
    FILLER = "padding " * 200

    def __init__(self, out_agent="zcode_v2"):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.db = self.root / "index.db"
        self.project = self.root / "proj"
        self.project.mkdir()
        self.enter = self.project / "s-in.jsonl"
        # Claude-style dash encoding of the project path, which is what
        # session_in_cwd() reads.
        self.dash = str(self.project).replace("/", "-")
        self.enter_path = str(self.root / ".claude" / "projects" / self.dash / "s-in.jsonl")
        conn = sqlite3.connect(str(self.db))
        init_db(conn)
        for i in range(self.OUTSIDERS):
            sid = f"{out_agent}:out-{i}"
            conn.execute(
                "INSERT INTO sessions (id, agent, jsonl_path) VALUES (?,?,?)",
                (sid, out_agent, str(self.root / f"out-{i}.jsonl")))
            conn.execute(
                "INSERT INTO messages_fts (session_id, role, timestamp, text)"
                " VALUES (?,?,?,?)",
                (sid, "USER", "t", split_cjk(" ".join([NEEDLE] * 10))))
        conn.execute(
            "INSERT INTO sessions (id, agent, jsonl_path) VALUES (?,?,?)",
            ("claude:in-1", "claude", self.enter_path))
        conn.execute(
            "INSERT INTO messages_fts (session_id, role, timestamp, text)"
            " VALUES (?,?,?,?)",
            ("claude:in-1", "USER", "t", split_cjk(self.FILLER + NEEDLE)))
        conn.commit()
        conn.close()

    def __enter__(self):
        self._prev = os.getcwd()
        os.chdir(self.project)
        return self

    def __exit__(self, *exc):
        os.chdir(self._prev)
        self.tmp.cleanup()


class ScopeFilterIsSelectionTest(unittest.TestCase):
    def _find(self, fx, **kwargs):
        sd = _load_recall()
        orig = sd.DB_PATH
        sd.DB_PATH = fx.db
        try:
            return sd.find_sessions(keyword=NEEDLE, **kwargs)
        finally:
            sd.DB_PATH = orig

    def test_the_page_really_is_out_of_project(self):
        """Without the fix there is nothing left to find — pin the premise."""
        with _ProjectIndex() as fx:
            sd = _load_recall()
            orig = sd.DB_PATH
            sd.DB_PATH = fx.db
            try:
                page = sd._fts_search(NEEDLE, limit=20)
            finally:
                sd.DB_PATH = orig
            self.assertTrue(page)
            self.assertNotIn("claude:in-1", [sid for sid, _p, _a in page])

    def test_scope_current_returns_the_in_project_session(self):
        with _ProjectIndex() as fx:
            rows = self._find(fx, scope="current", limit=5)
            self.assertEqual(["claude:in-1"], [sid for sid, _p, _a in rows])

    def test_scope_all_still_ranks_the_project_session_below_the_page(self):
        with _ProjectIndex() as fx:
            rows = self._find(fx, scope="all", limit=5)
            self.assertEqual(5, len(rows))
            self.assertTrue(all("out-" in sid for sid, _p, _a in rows))

    def test_agent_narrow_returns_the_last_matching_session(self):
        """Same defect on another axis: a filter may not shorten a full set."""
        with _ProjectIndex(out_agent="zcode_v2") as fx:
            rows = self._find(fx, scope="all", limit=5, agent="claude")
            self.assertEqual(["claude:in-1"], [sid for sid, _p, _a in rows])

    def test_unfiltered_lookup_still_fills_the_page(self):
        """One session contributes many rows: the window must yield `limit`."""
        with _ProjectIndex() as fx:
            rows = self._find(fx, scope="all", limit=5)
            self.assertEqual(
                sorted(f"zcode_v2:out-{i}" for i in range(5)),
                sorted(sid for sid, _p, _a in rows))


class UnfilteredPathKeepsItsCheapWindowTest(unittest.TestCase):
    """A filter that filters nothing must not be passed as one.

    ``_fts_search`` widens its window to the whole ranked set whenever it is
    given a predicate, because a page chosen by BM25 and then filtered answers
    short. That widening is the *fix* for ``--scope current`` — but it was also
    paid on ``--scope all``, the default scope, where the predicate was
    always-true and nothing was ever dropped. Measured 2026-09-17 on the live
    index: 71.8 ms against 24.9 ms for ``agent`` (15,210 ranked rows) for an
    identical answer.

    These assertions read the bound parameter, so they fail if the always-true
    predicate comes back — which the answer-equality tests above cannot see,
    because both paths return the same sessions.
    """

    LIMIT = 8

    def _bound_params(self, fx, **kwargs):
        """Run find_sessions and return every bound parameter it sent."""
        import unittest.mock as mock

        seen = []
        real_connect = sqlite3.connect

        class _Conn:
            def __init__(self, real):
                self._real = real

            def execute(self, sql, params=()):
                seen.append(params)
                return self._real.execute(sql, params)

            def close(self):
                self._real.close()

        sd = _load_recall()
        orig = sd.DB_PATH
        sd.DB_PATH = fx.db
        try:
            with mock.patch("sqlite3.connect",
                            side_effect=lambda *a, **k: _Conn(real_connect(*a, **k))):
                sd.find_sessions(keyword=NEEDLE, limit=self.LIMIT, **kwargs)
        finally:
            sd.DB_PATH = orig
        return seen

    def test_no_filter_means_no_widening(self):
        with _ProjectIndex() as fx:
            self.assertIsNone(
                _load_recall()._session_filter("all", "cross"),
                "no cwd rule + no agent rule must read as 'no filter', not as "
                "an always-true predicate")
            params = self._bound_params(fx, scope="all", agent="cross")
        # find_sessions pre-widens to max(limit*3, 20) — one session contributes
        # many rows — and _fts_search multiplies by 5 on top. Neither widening
        # is the "whole ranked set" one, which is the claim under test.
        cheap = max(self.LIMIT * 3, 20) * 5
        self.assertIn(cheap, [p[-1] for p in params if p],
                      f"the unfiltered window must stay {cheap}, not the whole ranked set")
        self.assertNotIn(-1, [p[-1] for p in params if p],
                         "no filter was supplied, so nothing may widen to LIMIT -1")

    def test_a_real_filter_still_widens(self):
        with _ProjectIndex() as fx:
            self.assertIsNotNone(_load_recall()._session_filter("current", "cross"))
            params = self._bound_params(fx, scope="current", agent="cross")
        self.assertIn(-1, [p[-1] for p in params if p],
                      "a real filter must still see the whole ranked set")

    def test_agent_narrow_also_widens(self):
        with _ProjectIndex() as fx:
            self.assertIsNotNone(_load_recall()._session_filter("all", "claude"))
            params = self._bound_params(fx, scope="all", agent="claude")
        self.assertIn(-1, [p[-1] for p in params if p])


class EmptyReportDiagnosisTest(unittest.TestCase):
    """An empty result must name the reason it is empty.

    ``!!!`` tokenizes to nothing, so ``build_match_query`` returns None and no
    question was ever asked. The report used to answer with the generic "the
    index has no row for this term in any scope — narrow the term or check that
    the session was indexed", which sends the reader hunting for missing data.
    """

    def _report(self, sd, keyword):
        """Capture ``report_empty_search`` on an already-configured module.

        ``_load_recall()`` re-executes the script and hands back a *fresh*
        module, so a caller that patched ``DB_PATH`` on one instance must not
        have the report run on another — that silently reads the live index.
        """
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sd.report_empty_search(keyword, "current")
        return buf.getvalue()

    def test_unsearchable_term_says_so(self):
        text = self._report(_load_recall(), "!!!")
        self.assertIn("no searchable content", text)
        self.assertNotIn("no row for this term in any scope", text)

    def test_a_real_term_still_gets_the_index_diagnosis(self):
        """The new branch must not swallow the useful report for real terms."""
        sd = _load_recall()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "index.db"
        conn = sqlite3.connect(str(db))
        init_db(conn)
        conn.execute("INSERT INTO sessions (id, agent, jsonl_path) VALUES (?,?,?)",
                     ("claude:live", "claude", "/tmp/live.jsonl"))
        conn.execute("INSERT INTO messages_fts (session_id, role, timestamp, text)"
                     " VALUES (?,?,?,?)", ("claude:live", "USER", "t", split_cjk(NEEDLE)))
        conn.commit()
        conn.close()
        orig = sd.DB_PATH
        sd.DB_PATH = db
        try:
            text = self._report(sd, NEEDLE)
        finally:
            sd.DB_PATH = orig
        self.assertIn("row(s) elsewhere", text)
        self.assertNotIn("no searchable content", text)


class FacetCountsReachabilityTest(unittest.TestCase):
    """The report's "elsewhere" must be reachable by widening the scope."""

    def _index_with_orphan(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "index.db"
        conn = sqlite3.connect(str(db))
        init_db(conn)
        conn.execute("INSERT INTO sessions (id, agent, jsonl_path) VALUES (?,?,?)",
                     ("claude:live", "claude", "/tmp/live.jsonl"))
        conn.execute("INSERT INTO messages_fts (session_id, role, timestamp, text)"
                     " VALUES (?,?,?,?)", ("claude:live", "USER", "t", split_cjk(NEEDLE)))
        # An FTS row whose session row is gone: no JOIN can resolve a path for it.
        conn.execute("INSERT INTO messages_fts (session_id, role, timestamp, text)"
                     " VALUES (?,?,?,?)", ("claude:gone", "USER", "t", split_cjk(NEEDLE)))
        conn.commit()
        conn.close()
        return db

    def test_orphan_rows_are_not_counted_as_elsewhere(self):
        sd = _load_recall()
        orig = sd.DB_PATH
        sd.DB_PATH = self._index_with_orphan()
        try:
            counts = sd._facet_counts(NEEDLE)
        finally:
            sd.DB_PATH = orig
        self.assertEqual({"USER": 1}, counts)

    def test_unsearchable_keyword_is_not_an_error(self):
        sd = _load_recall()
        orig = sd.DB_PATH
        sd.DB_PATH = self._index_with_orphan()
        try:
            self.assertEqual({}, sd._facet_counts("***"))
        finally:
            sd.DB_PATH = orig


if __name__ == "__main__":
    unittest.main()
