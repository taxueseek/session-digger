"""Adopted environments: rows the index already has must stay refreshable.

The freeze this guards against: the builder only iterates ``ENV_REGISTRY`` +
``KNOWN_UNADAPTED`` while the report layer also sweeps unknown dot-directories,
so a session indexed by an earlier build is never revisited once its environment
leaves the registry. Its ``indexed_at`` stops, its stats keep that day's values,
and its evidence projection stays ``''`` forever. Measured on the live index:
183 rows across 6 environments, files all still present.

The fix derives those environments back from the index itself. Nothing is
persisted and nothing widens the discovery surface: an environment is adopted
only if rows for it already exist *and* its directory is still there.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import echolib  # noqa: E402
import index_builder._builder as builder  # noqa: E402
from index_builder._schema import init_db  # noqa: E402


def _write_session(path: Path, text="索引重建为什么这么慢，先看 FTS 段合并"):
    path.parent.mkdir(parents=True, exist_ok=True)
    recs = [
        {"role": "user", "content": text},
        {"role": "assistant", "content": "逐会话删除每次都全表扫描，改成批量"},
    ]
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
        encoding="utf-8")


class TestAdoptedEnvResolution(unittest.TestCase):
    """``adopted_envs`` is pure: ids in, registry-shaped entries out."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.home = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_id_maps_to_its_dot_directory(self):
        (self.home / ".pi").mkdir()
        got = echolib.adopted_envs({"pi"}, home=self.home)
        self.assertEqual(list(got), ["pi"])
        self.assertEqual(got["pi"]["root"], str(self.home / ".pi"))
        self.assertEqual(got["pi"]["adapter"], "universal")

    def test_missing_directory_is_not_adopted(self):
        """Converges downward: a removed environment stops being adopted."""
        self.assertEqual(echolib.adopted_envs({"gone"}, home=self.home), {})

    def test_registered_environments_are_never_re_adopted(self):
        """A registry entry already carries its own root and adapter."""
        (self.home / ".claude").mkdir()
        for env_id in list(echolib.ENV_REGISTRY) + list(echolib.KNOWN_UNADAPTED):
            (self.home / f".{env_id}").mkdir(exist_ok=True)
        self.assertEqual(echolib.adopted_envs(set(echolib.ENV_REGISTRY)
                                              | set(echolib.KNOWN_UNADAPTED),
                                              home=self.home), {})

    def test_blank_and_empty_inputs(self):
        self.assertEqual(echolib.adopted_envs(set(), home=self.home), {})
        self.assertEqual(echolib.adopted_envs(None, home=self.home), {})
        self.assertEqual(echolib.adopted_envs({""}, home=self.home), {})

    def test_a_file_named_like_the_dir_is_not_an_environment(self):
        (self.home / ".notadir").write_text("x", encoding="utf-8")
        self.assertEqual(echolib.adopted_envs({"notadir"}, home=self.home), {})

    def test_ids_that_could_escape_home_are_refused(self):
        """The id comes from index rows, so it is untrusted input."""
        outside = self.home.parent / "escaped"
        outside.mkdir(exist_ok=True)
        try:
            for bad in ("../escaped", "..", "a/b", "a\\b", ".hidden"):
                self.assertEqual(
                    echolib.adopted_envs({bad}, home=self.home), {},
                    f"{bad!r} must not become a path under home")
        finally:
            outside.rmdir()


class TestFrozenRowsBecomeRefreshable(unittest.TestCase):
    """End to end: a row whose environment left the registry is scanned again."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.home = Path(self._td.name)
        self.db = self.home / "index.db"
        self.session = self.home / ".pi" / "agent" / "sessions" / "slug" / "abc.jsonl"

        self._orig_db_path = builder.DB_PATH
        self._orig_registry = builder.echolib.ENV_REGISTRY
        self._orig_unadapted = builder.echolib.KNOWN_UNADAPTED
        builder.DB_PATH = self.db
        # Only the adopted environment is in play: the registry points at this
        # machine's real directories, which would make the test depend on it.
        builder.echolib.ENV_REGISTRY = {}
        builder.echolib.KNOWN_UNADAPTED = {}
        self._home_patch = mock.patch("pathlib.Path.home", return_value=self.home)
        self._home_patch.start()

        _write_session(self.session)
        conn = sqlite3.connect(str(self.db))
        init_db(conn)
        conn.execute("INSERT INTO sessions (id, jsonl_path) VALUES (?,?)",
                     ("pi:abc", str(self.session)))
        conn.commit()
        conn.close()

    def tearDown(self):
        self._home_patch.stop()
        builder.DB_PATH = self._orig_db_path
        builder.echolib.ENV_REGISTRY = self._orig_registry
        builder.echolib.KNOWN_UNADAPTED = self._orig_unadapted
        self._td.cleanup()

    def test_indexed_env_ids_are_read_back(self):
        self.assertEqual(builder._indexed_env_ids(), {"pi"})

    def test_the_scan_set_includes_the_adopted_environment(self):
        self.assertIn("pi", builder._scan_envs())

    def test_scan_sessions_finds_the_frozen_session_again(self):
        entries = builder.scan_sessions("cross")
        self.assertEqual([e[0] for e in entries], ["pi:abc"])
        self.assertEqual(entries[0][1], str(self.session))

    def test_the_build_refreshes_it_instead_of_skipping_it(self):
        """The row's evidence was never computed; the build must fill it."""
        conn = sqlite3.connect(str(self.db))
        before = conn.execute(
            "SELECT user_evidence_json FROM sessions WHERE id = 'pi:abc'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(before, "", "precondition: un-migrated row")

        result = builder.build_index(rebuild=False, agent_filter="cross")
        self.assertEqual(result["indexed"], 1)
        self.assertEqual(result["skipped"], 0)

        conn = sqlite3.connect(str(self.db))
        after, mtime = conn.execute(
            "SELECT user_evidence_json, jsonl_mtime FROM sessions WHERE id = 'pi:abc'"
        ).fetchone()
        conn.close()
        self.assertNotEqual(after, "", "evidence projection must be computed now")
        self.assertTrue(json.loads(after), "the session has user turns")
        self.assertIsNotNone(mtime)

    def test_prune_keeps_it(self):
        """The prune set must agree with the scan set, or the row is deleted."""
        self.assertEqual(builder._prune_stale_sessions(
            sqlite3.connect(str(self.db)), {"pi:abc"}, "cross"), 0)

    def test_a_row_for_a_vanished_environment_is_still_left_alone(self):
        """Deleting is destructive; a directory we cannot see is not proof."""
        self.session.unlink()
        (self.home / ".pi").rename(self.home / ".pi-moved")
        self.assertEqual(builder._prune_stale_sessions(
            sqlite3.connect(str(self.db)), set(), "cross"), 0)


if __name__ == "__main__":
    unittest.main()
