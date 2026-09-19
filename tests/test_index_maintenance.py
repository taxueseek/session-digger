"""Index maintenance contracts: scoped deletes and FTS compaction.

Both exist because FTS5 makes the obvious operation quadratic. `session_id` is
UNINDEXED, so deleting one session's rows scans the whole content table
(measured 28.9 ms — 3 minutes for a 6287-session rebuild), and the segment
b-tree only grows: `messages_fts_data` reached 219 MB against 51 MB for the
same content before it was merged. These tests pin the batched delete, the
compaction and the guarantee that neither loses rows.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from index_builder import _builder as builder  # noqa: E402
from index_builder._cjk import split_cjk  # noqa: E402
from index_builder._schema import init_db  # noqa: E402


def _seed(conn, ids, msgs_per=3):
    for sid in ids:
        conn.execute("INSERT OR REPLACE INTO sessions (id) VALUES (?)", (sid,))
        for i in range(msgs_per):
            conn.execute(
                "INSERT INTO messages_fts (session_id, role, timestamp, text) "
                "VALUES (?,?,?,?)",
                (sid, "USER", "t", split_cjk(f"会话 {sid} 的第 {i} 条内容")))
        conn.execute(
            "INSERT INTO topic_boundaries (session_id, message_index, timestamp,"
            " topic_label, confidence) VALUES (?,?,?,?,?)",
            (sid, 0, "t", "topic_1", 0.9))
    conn.commit()


class TestScopedDelete(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db = Path(self._td.name) / "index.db"
        self.conn = __import__("sqlite3").connect(str(self.db))
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self._td.cleanup()

    def _ids_in_fts(self):
        return {r[0] for r in self.conn.execute(
            "SELECT DISTINCT session_id FROM messages_fts")}

    def test_deletes_exactly_the_given_ids(self):
        _seed(self.conn, ["a", "b", "c", "d"])
        builder._delete_scoped_rows(self.conn, ["b", "d"])
        self.assertEqual(self._ids_in_fts(), {"a", "c"})

    def test_deletes_topic_rows_too(self):
        _seed(self.conn, ["a", "b"])
        builder._delete_scoped_rows(self.conn, ["a"])
        left = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT session_id FROM topic_boundaries")}
        self.assertEqual(left, {"b"})

    def test_empty_list_is_a_no_op(self):
        _seed(self.conn, ["a"])
        builder._delete_scoped_rows(self.conn, [])
        self.assertEqual(self._ids_in_fts(), {"a"})

    def test_missing_and_blank_ids_are_tolerated(self):
        _seed(self.conn, ["a"])
        builder._delete_scoped_rows(self.conn, ["nope", "", None])
        self.assertEqual(self._ids_in_fts(), {"a"})

    def test_chunks_stay_under_the_bind_limit(self):
        """More ids than SQLite's 999 bound-parameter limit must still work."""
        ids = [f"s{i}" for i in range(1200)]
        _seed(self.conn, ids, msgs_per=1)
        self.assertEqual(len(self._ids_in_fts()), 1200)
        builder._delete_scoped_rows(self.conn, ids)
        self.assertEqual(self._ids_in_fts(), set())


class TestCompaction(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db = Path(self._td.name) / "index.db"
        self.conn = __import__("sqlite3").connect(str(self.db))
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self._td.cleanup()

    def _churn(self, rounds=40):
        """Rewrite the same sessions repeatedly, as incremental builds do."""
        for r in range(rounds):
            ids = [f"s{i}" for i in range(30)]
            builder._delete_scoped_rows(self.conn, ids)
            for sid in ids:
                for i in range(20):
                    self.conn.execute(
                        "INSERT INTO messages_fts (session_id, role, timestamp, text)"
                        " VALUES (?,?,?,?)",
                        (sid, "USER", "t", split_cjk(f"第 {r} 轮 {sid} 内容 {i}")))
            self.conn.commit()

    def test_optimize_merges_and_vacuum_reclaims(self):
        self._churn()
        before_rows = self.conn.execute(
            "SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        before_pages = self.conn.execute(
            "SELECT page_count FROM pragma_page_count").fetchone()[0]
        stats = builder._compact_index(self.conn)
        after_rows = self.conn.execute(
            "SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        self.assertEqual(before_rows, after_rows, "compaction must not lose rows")
        self.assertEqual(stats["pages_before"], before_pages)
        self.assertLess(stats["pages_after"], before_pages,
                        "compaction must shrink the file")
        self.assertEqual(
            self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_compaction_preserves_searchability(self):
        self._churn()
        builder._compact_index(self.conn)
        hit = self.conn.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?",
            ('"内 容"',)).fetchone()[0]
        self.assertGreater(hit, 0, "merged index must still match")


class TestCompactTrigger(unittest.TestCase):
    """The auto-trigger must fire on mass rewrites and stay off otherwise."""

    def test_thresholds_are_sane(self):
        self.assertGreaterEqual(builder._COMPACT_MIN_REWRITES, 1)
        self.assertLessEqual(builder._COMPACT_REWRITE_RATIO, 1.0)

    def test_small_incremental_build_does_not_trigger(self):
        indexed, rewritten = 5, 6000
        self.assertFalse(
            indexed >= builder._COMPACT_MIN_REWRITES
            and indexed >= rewritten * builder._COMPACT_REWRITE_RATIO)

    def test_mass_rewrite_triggers(self):
        indexed, rewritten = 6000, 6300
        self.assertTrue(
            indexed >= builder._COMPACT_MIN_REWRITES
            and indexed >= rewritten * builder._COMPACT_REWRITE_RATIO)


class TestStaleSessionPruning(unittest.TestCase):
    """The index must be able to shrink, without ever deleting live history.

    Every guard here exists because getting it wrong destroys the user's
    records: a scoped build must not touch other environments, a DB-backed
    session has no file to check, and a listing that *failed* is not evidence
    that the user deleted anything.
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.live_root = self.tmp / "live-env"
        self.gone_root = self.tmp / "gone-env"
        self.live_root.mkdir()
        self.conn = __import__("sqlite3").connect(str(self.tmp / "index.db"))
        init_db(self.conn)

        self._saved = (builder.echolib.ENV_REGISTRY, builder.echolib.KNOWN_UNADAPTED,
                       builder.echolib.ADAPTER_REGISTRY, builder._SCAN_FAILED_ENVS)
        builder.echolib.ENV_REGISTRY = {"live": {"root": str(self.live_root)}}
        builder.echolib.KNOWN_UNADAPTED = {"gone": {"root": str(self.gone_root)}}
        builder.echolib.ADAPTER_REGISTRY = {}
        builder._SCAN_FAILED_ENVS = set()

    def tearDown(self):
        (builder.echolib.ENV_REGISTRY, builder.echolib.KNOWN_UNADAPTED,
         builder.echolib.ADAPTER_REGISTRY, builder._SCAN_FAILED_ENVS) = self._saved
        self.conn.close()
        self._td.cleanup()

    def _insert(self, sid, path):
        self.conn.execute(
            "INSERT OR REPLACE INTO sessions (id, jsonl_path) VALUES (?,?)",
            (sid, path))
        self.conn.execute(
            "INSERT INTO messages_fts (session_id, role, timestamp, text) VALUES (?,?,?,?)",
            (sid, "USER", "t", split_cjk("正文")))
        self.conn.commit()

    def _ids(self):
        return {r[0] for r in self.conn.execute("SELECT id FROM sessions")}

    def _fts_ids(self):
        return {r[0] for r in self.conn.execute(
            "SELECT DISTINCT session_id FROM messages_fts")}

    def test_row_for_a_vanished_file_is_removed(self):
        real = self.live_root / "s1.jsonl"
        real.write_text("{}", encoding="utf-8")
        self._insert("live:s1", str(real))
        self._insert("live:deleted", str(self.live_root / "was-here.jsonl"))
        self.assertEqual(builder._prune_stale_sessions(self.conn, {"live:s1"}), 1)
        self.assertEqual(self._ids(), {"live:s1"})
        self.assertEqual(self._fts_ids(), {"live:s1"},
                         "FTS rows must go with the session row")

    def test_a_scoped_build_never_prunes(self):
        self._insert("other:one", str(self.live_root / "nope.jsonl"))
        self.assertEqual(
            builder._prune_stale_sessions(self.conn, set(), "universal"), 0)
        self.assertEqual(self._ids(), {"other:one"})

    def test_virtual_paths_are_never_pruned(self):
        """DB-backed sessions have no file to stat — absence is not evidence."""
        self._insert("dimcode:sess_x", "dimcode://sess_x")
        self.assertEqual(builder._prune_stale_sessions(self.conn, set()), 0)
        self.assertEqual(self._ids(), {"dimcode:sess_x"})

    def test_paths_outside_every_live_root_are_kept(self):
        self._insert("elsewhere:one", "/somewhere/else/s.jsonl")
        self.assertEqual(builder._prune_stale_sessions(self.conn, set()), 0)
        self.assertEqual(self._ids(), {"elsewhere:one"})

    def test_a_failed_environment_is_skipped_entirely(self):
        """An unmounted volume or a locked DB must not look like a mass delete."""
        self._insert("gone:a", str(self.gone_root / "a.jsonl"))
        self._insert("gone:b", str(self.gone_root / "b.jsonl"))
        builder._SCAN_FAILED_ENVS = {"gone"}
        self.assertEqual(builder._prune_stale_sessions(self.conn, set()), 0)
        self.assertEqual(self._ids(), {"gone:a", "gone:b"})

    def test_pruning_is_idempotent(self):
        self._insert("live:dead", str(self.live_root / "dead.jsonl"))
        self.assertEqual(builder._prune_stale_sessions(self.conn, set()), 1)
        self.assertEqual(builder._prune_stale_sessions(self.conn, set()), 0)

    def test_scan_sessions_reports_a_failed_environment(self):
        """The failure flag that protects the prune must actually get set."""
        builder.echolib.ENV_REGISTRY = {"live": {"root": str(self.live_root),
                                                 "adapter": "boom"}}
        builder.echolib.ADAPTER_REGISTRY = {
            "boom": {"list_sessions": lambda **kw: (_ for _ in ()).throw(
                RuntimeError("listing exploded"))}}
        builder.scan_sessions("cross")
        self.assertIn("live", builder._SCAN_FAILED_ENVS)


class TestSessionDetailFields(unittest.TestCase):
    """`index-builder.py detail` must key its payload by column name.

    It zipped PRAGMA table_info's *ordinal* onto each value, so every field
    came back as "0", "1", "2", ... — the payload was structurally valid JSON
    and useless to any caller reading it by name.
    """

    def test_payload_is_keyed_by_column_name(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "index_builder_cli", str(SCRIPTS / "index-builder.py"))
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "index.db"
            conn = __import__("sqlite3").connect(str(db))
            init_db(conn)
            conn.execute(
                "INSERT INTO sessions (id, agent, message_count, model) VALUES (?,?,?,?)",
                ("s1", "claude", 7, "m-1"))
            conn.commit()
            conn.close()

            orig = cli.DB_PATH
            cli.DB_PATH = db
            try:
                detail = cli.session_detail("s1")
            finally:
                cli.DB_PATH = orig

            self.assertEqual(detail["id"], "s1")
            self.assertEqual(detail["agent"], "claude")
            self.assertEqual(detail["message_count"], 7)
            self.assertEqual(detail["model"], "m-1")
            self.assertIn("topics", detail)
            self.assertNotIn(0, detail, "ordinal keys must not come back")


class TestSessionRowImpliesSearchability(unittest.TestCase):
    """A ``sessions`` row without its FTS rows is a permanently invisible session.

    ``build_index`` deletes each rewritten session's FTS rows in one batched pass
    *before* inserting the new ones. If that insert then fails, the old rows are
    already gone: keeping the row makes the next build skip the session (its
    fingerprint matches), so "listed but unsearchable" is permanent and was not
    counted in ``errors`` either. The row must die instead, so the next build
    sees ``prior is None`` and retries.
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db_dir = Path(self._td.name)
        self.db = self.db_dir / "index.db"
        self._orig_db_path = builder.DB_PATH
        self._orig_db_dir = builder.DB_DIR
        self._orig_scan = builder.scan_sessions
        self._orig_fp = builder._file_fingerprint
        self._orig_compute = builder._compute_session
        builder.DB_PATH = self.db
        builder.DB_DIR = self.db_dir
        self.sid = "claude:boom"
        builder.scan_sessions = lambda *a, **k: [(self.sid, "/tmp/fake.jsonl", "claude")]
        builder._file_fingerprint = lambda p: (1234.0, "hash")
        builder._compute_session = lambda task: (
            self.sid, (self.sid,) + (None,) * 29,
            # 3 binds against a 4-placeholder INSERT → sqlite3.ProgrammingError
            [("a", "b", "c")], [], None,
        )

    def tearDown(self):
        builder.DB_PATH = self._orig_db_path
        builder.DB_DIR = self._orig_db_dir
        builder.scan_sessions = self._orig_scan
        builder._file_fingerprint = self._orig_fp
        builder._compute_session = self._orig_compute
        self._td.cleanup()

    def _counts(self):
        import sqlite3
        conn = sqlite3.connect(str(self.db))
        try:
            return (conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                    conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0])
        finally:
            conn.close()

    def test_failed_fts_insert_leaves_no_session_row(self):
        result = builder.build_index(rebuild=False, agent_filter="cross")
        self.assertEqual(result["errors"], 1, "the failure must be counted")
        self.assertEqual(result["indexed"], 0, "a failed session is not indexed")
        sessions, fts = self._counts()
        self.assertEqual(sessions, 0, "row kept → permanently unsearchable")
        self.assertEqual(fts, 0)

    def test_the_session_is_retried_on_the_next_build(self):
        builder.build_index(rebuild=False, agent_filter="cross")
        second = builder.build_index(rebuild=False, agent_filter="cross")
        self.assertEqual(second["skipped"], 0,
                         "a dropped row must not be skipped as unchanged")
        self.assertEqual(second["errors"], 1, "must be attempted again, not skipped")


class TestScanFailureIsRecorded(unittest.TestCase):
    """Every discovery failure must land in ``_SCAN_FAILED_ENVS``.

    ``_prune_stale_sessions`` spares environments whose listing raised, so an
    unmounted volume cannot be read as "the user deleted everything". The
    adapter branch honoured that; the glob fallback swallowed the exception and
    returned nothing, which makes an entire environment look deleted — and its
    rows (plus FTS and boundaries) are then deleted for real.
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        # Patch through ``builder.echolib``, not a fresh ``import echolib``:
        # several test modules set ``sys.modules["echolib"]`` to their own
        # importlib-loaded copy, so a fresh import can hand back a *different*
        # module object than the one ``_builder`` captured. Patching that copy
        # leaves the real registry in place and the test silently scans the
        # user's whole history.
        self.echolib = builder.echolib
        self._orig_registry = self.echolib.ENV_REGISTRY
        self._orig_unadapted = self.echolib.KNOWN_UNADAPTED
        self._orig_find = builder._find_jsonl_files

    def tearDown(self):
        self.echolib.ENV_REGISTRY = self._orig_registry
        self.echolib.KNOWN_UNADAPTED = self._orig_unadapted
        builder._find_jsonl_files = self._orig_find
        self._td.cleanup()

    def test_glob_fallback_failure_is_recorded(self):
        # An adapter name nothing is registered under forces the glob branch.
        self.echolib.ENV_REGISTRY = {
            "synth": {"name": "Synth", "root": self._td.name,
                      "format": "jsonl", "adapter": "no_such_adapter"},
        }
        self.echolib.KNOWN_UNADAPTED = {}

        def boom(_root, _env_id):
            raise OSError("transient listing failure")

        builder._find_jsonl_files = boom
        entries = builder.scan_sessions("cross")
        self.assertEqual(entries, [])
        self.assertIn("synth", builder._SCAN_FAILED_ENVS)


class TestGrokChildBackfill(unittest.TestCase):
    """Child marking must not cost one table scan per child.

    Each child used to get its own ``UPDATE`` with three LIKE predicates and no
    usable index, i.e. one full pass over ``sessions`` per child — 29 of them on
    the live install, measured as the largest single item in the role backfill
    (85 ms of a 0.67 s no-change build). The children are all marked with the
    same value, so one scan plus one statement per bind-limit chunk is
    equivalent — and this test counts the statements rather than trusting it.
    """

    CHILDREN = 29

    def setUp(self):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        init_db(self.conn)
        self._orig_children = builder._grok_child_session_ids
        self.child_ids = {f"child-{i}" for i in range(self.CHILDREN)}
        builder._grok_child_session_ids = lambda: set(self.child_ids)
        self.addCleanup(setattr, builder, "_grok_child_session_ids", self._orig_children)
        for cid in sorted(self.child_ids):
            self.conn.execute(
                "INSERT INTO sessions (id, agent, jsonl_path, session_role)"
                " VALUES (?,?,?,?)",
                (f"grok:{cid}", "grok", f"/tmp/grok/{cid}.jsonl", "unknown"))
        self.conn.execute(
            "INSERT INTO sessions (id, agent, jsonl_path, session_role)"
            " VALUES (?,?,?,?)", ("grok:parent", "grok", "/tmp/grok/parent.jsonl", "unknown"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _role(self, sid):
        return self.conn.execute(
            "SELECT session_role FROM sessions WHERE id = ?", (sid,)).fetchone()[0]

    def test_every_child_is_marked_subagent(self):
        builder._backfill_session_roles(self.conn)
        for cid in self.child_ids:
            self.assertEqual("subagent", self._role(f"grok:{cid}"), cid)
        self.assertEqual("main", self._role("grok:parent"))

    def test_children_are_marked_by_a_single_statement(self):
        seen = []
        self.conn.set_trace_callback(seen.append)
        builder._backfill_session_roles(self.conn)
        self.conn.set_trace_callback(None)
        child_updates = [
            sql for sql in seen
            if sql.upper().startswith("UPDATE SESSIONS")
            and "SESSION_ROLE = 'SUBAGENT' WHERE ID IN" in sql.upper()
        ]
        self.assertEqual(1, len(child_updates),
                         f"{self.CHILDREN} children must not mean {self.CHILDREN} scans")


if __name__ == "__main__":
    unittest.main()
