"""Contracts of the session-listing layer (``sd-recall sessions`` and friends).

Three things this layer got wrong, each pinned here:

1. **The index join key.** The index stores ``{env}:{adapter_id}``; the listing
   layer returns the adapter's bare id. For the same 200 sessions that is 0 id
   matches and 200 path matches, so anything joining the two must use
   ``jsonl_path``. ``session_stats_by_path`` is that join.
2. **Deterministic order.** ``cross_tool_list_sessions`` merged results in
   ``as_completed()`` order, and that order decided the round-robin rotation —
   so the visible top-N reshuffled between identical invocations.
3. **Thread boundaries.** Adaptor calls happen on a thread pool, which does not
   inherit the caller's context, so a ``discovery_only`` block used to evaporate
   there.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import echolib  # noqa: E402
from echolib import _adapters as adapters  # noqa: E402
from echolib._models import SessionMeta  # noqa: E402
import index_builder._reader as reader  # noqa: E402
from index_builder._schema import init_db  # noqa: E402


class TestIndexJoinIsByPath(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db = Path(self._td.name) / "index.db"
        self.conn = sqlite3.connect(str(self.db))
        init_db(self.conn)
        self.conn.execute(
            "INSERT INTO sessions (id, jsonl_path, created, user_messages,"
            " assistant_messages, branch, jsonl_mtime) VALUES (?,?,?,?,?,?,?)",
            ("claude:abc", "/tmp/x/abc.jsonl", "2026-09-01T10:00:00Z", 3, 4,
             "main", 111.0))
        self.conn.commit()
        self.conn.close()
        self._saved = reader.DB_PATH
        reader.DB_PATH = self.db

    def tearDown(self):
        reader.DB_PATH = self._saved
        self._td.cleanup()

    def test_row_found_by_path_not_by_bare_id(self):
        got = reader.session_stats_by_path(["/tmp/x/abc.jsonl"])
        self.assertEqual(got["/tmp/x/abc.jsonl"]["msgs"], 7)
        self.assertEqual(got["/tmp/x/abc.jsonl"]["branch"], "main")
        self.assertEqual(got["/tmp/x/abc.jsonl"]["created"], "2026-09-01T10:00:00Z")
        self.assertEqual(got["/tmp/x/abc.jsonl"]["jsonl_mtime"], 111.0)

    def test_unknown_path_is_absent_not_zero(self):
        self.assertEqual(reader.session_stats_by_path(["/tmp/x/nope.jsonl"]), {})

    def test_empty_input_touches_no_database(self):
        self.assertEqual(reader.session_stats_by_path([]), {})

    def test_duplicate_paths_are_one_lookup(self):
        got = reader.session_stats_by_path(["/tmp/x/abc.jsonl"] * 5)
        self.assertEqual(len(got), 1)


class TestDeterministicListing(unittest.TestCase):
    """Same inputs must give the same list, run after run."""

    def setUp(self):
        import concurrent.futures as cf

        # Two agents whose adapters finish in a deliberately unstable order:
        # the first submission sleeps longest, so completion order is the
        # reverse of submission order.
        def slow(**kw):
            import time
            time.sleep(0.05)
            return [SessionMeta(session_id="a1", full_path="/a/1", created="2026-01-01",
                                modified="2026-01-01", message_count=1, git_branch="",
                                summary="", first_prompt="", project_path="")]

        def fast(**kw):
            return [SessionMeta(session_id="b1", full_path="/b/1", created="2026-01-01",
                                modified="2026-01-01", message_count=1, git_branch="",
                                summary="", first_prompt="", project_path="")]

        self._saved = adapters.ADAPTER_REGISTRY
        adapters.ADAPTER_REGISTRY = {
            "zzz_agent": {"list_sessions": slow, "display_name": "Z"},
            "aaa_agent": {"list_sessions": fast, "display_name": "A"},
        }

    def tearDown(self):
        adapters.ADAPTER_REGISTRY = self._saved

    def test_rotation_follows_registry_order_not_completion_order(self):
        order = []
        for _ in range(5):
            rows = adapters.cross_tool_list_sessions(limit=2)
            order.append([r["agent"] for r in rows])
        self.assertEqual(
            order, [["zzz_agent", "aaa_agent"]] * 5,
            "the round-robin must start from a stable agent order; got %r" % order)


class TestContextReachesThePool(unittest.TestCase):
    """A ``discovery_only`` block must still be in force inside the workers."""

    def setUp(self):
        seen = {}

        def probe(**kw):
            seen["flag"] = echolib.discovery_only_active()
            return [SessionMeta(session_id="s1", full_path="/s/1", created="",
                                modified="", message_count=0, git_branch="",
                                summary="", first_prompt="", project_path="")]

        self.seen = seen
        self._saved = adapters.ADAPTER_REGISTRY
        adapters.ADAPTER_REGISTRY = {
            "probe": {"list_sessions": probe, "display_name": "P"}}

    def tearDown(self):
        adapters.ADAPTER_REGISTRY = self._saved

    def test_flag_is_visible_inside_worker_threads(self):
        with echolib.discovery_only():
            adapters.cross_tool_list_sessions(limit=1)
        self.assertTrue(
            self.seen.get("flag"),
            "ThreadPoolExecutor does not inherit context; the worker saw the "
            "flag as off, so discovery_only was a silent no-op on this path")

    def test_every_worker_gets_its_own_context(self):
        """One shared Context object cannot be entered twice — six adapters must work."""
        calls = []

        def probe(**kw):
            calls.append(echolib.discovery_only_active())
            return []

        saved = adapters.ADAPTER_REGISTRY
        try:
            adapters.ADAPTER_REGISTRY = {
                "a%d" % i: {"list_sessions": probe, "display_name": "A%d" % i}
                for i in range(6)
            }
            with echolib.discovery_only():
                adapters.cross_tool_list_sessions(limit=1)
        finally:
            adapters.ADAPTER_REGISTRY = saved
        self.assertEqual(calls, [True] * 6,
                         "each concurrent worker must enter its own Context copy")

    def test_total_adapter_failure_is_loud(self):
        """An empty result because everything failed must not look like 'no sessions'."""
        import logging

        def boom(**kw):
            raise RuntimeError("shared failure")

        saved = adapters.ADAPTER_REGISTRY
        try:
            adapters.ADAPTER_REGISTRY = {
                "x": {"list_sessions": boom, "display_name": "X"},
                "y": {"list_sessions": boom, "display_name": "Y"},
            }
            with self.assertLogs("echolib", level="ERROR") as cap:
                rows = adapters.cross_tool_list_sessions(limit=5)
        finally:
            adapters.ADAPTER_REGISTRY = saved
        self.assertEqual(rows, [])
        self.assertTrue(any("ALL" in m for m in cap.output),
                        "expected an explicit all-adapters-failed ERROR: %r" % cap.output)


class TestEnvScanOrder(unittest.TestCase):
    def test_env_results_are_returned_in_registry_order(self):
        results = echolib.scan_all_environments_parallel()
        known = [r for r in results if r.get("env_id") in echolib.ENV_REGISTRY]
        registry_order = [e for e in echolib.ENV_REGISTRY
                          if e in {r.get("env_id") for r in known}]
        self.assertEqual([r.get("env_id") for r in known], registry_order)
        # And it is reproducible run to run.
        again = echolib.scan_all_environments_parallel()
        self.assertEqual([r.get("env_id") for r in results],
                         [r.get("env_id") for r in again])


if __name__ == "__main__":
    unittest.main()
