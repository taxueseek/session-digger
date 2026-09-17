"""Contract tests for discovery-only mode (``echolib.discovery_only``).

The index builder enumerates sessions through the same ``list_sessions`` the UI
uses, then throws away every preview field. Discovery-only mode lets adapters
skip the per-file head parse that produced those fields — 4.35 s of a 5.40 s
``scan_sessions`` on the live install, and ~72% of an incremental build.

Two things can go wrong, and both are pinned here:

1. The enumeration must not change. Whatever the flag does to metadata, the
   ``session_id``/``full_path`` set for a given filesystem state must be
   identical to the preview-enabled run. Tests for that use a synthetic
   environment so they do not depend on this machine's session history.
2. The flag must not leak. ``cross_tool_list_sessions`` fans adapters out over
   threads and genuinely needs previews, so a module global would make a UI
   listing quietly lose its previews whenever it overlapped a build. The
   implementation is a ContextVar precisely so a thread cannot inherit it.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import echolib  # noqa: E402
import echolib._adapters as adapters  # noqa: E402


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    import json
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8")


def _user_record(text):
    return {"type": "user",
            "message": {"role": "user", "content": text}}


class TestFlagMechanics(unittest.TestCase):
    def test_default_is_off(self):
        self.assertFalse(echolib.discovery_only_active())

    def test_active_inside_the_block_only(self):
        with echolib.discovery_only():
            self.assertTrue(echolib.discovery_only_active())
        self.assertFalse(echolib.discovery_only_active())

    def test_flag_does_not_leak_into_other_threads(self):
        """A UI listing on another thread must keep its previews."""
        seen = {}

        def other_thread():
            seen["flag"] = echolib.discovery_only_active()

        with echolib.discovery_only():
            t = threading.Thread(target=other_thread)
            t.start()
            t.join()
        self.assertFalse(
            seen["flag"],
            "discovery-only leaked across threads: a concurrent UI listing "
            "would silently lose every preview")
        # And inside the block, the owning thread still sees it.
        with echolib.discovery_only():
            self.assertTrue(echolib.discovery_only_active())

    def test_nesting_restores_the_outer_value(self):
        with echolib.discovery_only():
            with echolib.discovery_only():
                self.assertTrue(echolib.discovery_only_active())
            self.assertTrue(echolib.discovery_only_active())
        self.assertFalse(echolib.discovery_only_active())

    def test_error_inside_the_block_still_restores(self):
        with self.assertRaises(ValueError):
            with echolib.discovery_only():
                raise ValueError("boom")
        self.assertFalse(echolib.discovery_only_active())


class TestPreviewsAreSkipped(unittest.TestCase):
    """Every adapter preview scanner must short-circuit in discovery-only mode."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_universal_quick_scan_returns_empty_without_reading(self):
        f = self.root / "s.jsonl"
        _write_jsonl(f, [_user_record("你好，帮我看看这个问题")])
        self.assertTrue(adapters._universal_quick_scan(f))
        with echolib.discovery_only():
            self.assertEqual(adapters._universal_quick_scan(f), "")

    def test_universal_quick_scan_does_not_touch_the_file(self):
        """The point is skipping I/O — a missing file must not even be reached."""
        missing = self.root / "not-there.jsonl"
        with echolib.discovery_only():
            self.assertEqual(adapters._universal_quick_scan(missing), "")

    def test_workbuddy_quick_scan_shape_is_preserved(self):
        import echolib._adapters_workbuddy as wb
        with echolib.discovery_only():
            out = wb._workbuddy_quick_scan(self.root / "nope.jsonl")
        self.assertEqual(out, (0, "", "", ""))
        self.assertEqual(len(out), 4, "callers unpack four values")


class TestEnumerationIsUnchanged(unittest.TestCase):
    """The session id/path set must not depend on the flag."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name) / "env"
        for name, text in (("a", "第一个会话"), ("b", "第二个会话")):
            _write_jsonl(self.root / "sessions" / name / "chat_history.jsonl",
                         [_user_record(text)])
        # A synthetic env, not the live ~/.claude install: asserting against the
        # operator's history would make this depend on their session count.

    def tearDown(self):
        self._td.cleanup()

    def _ids(self):
        return sorted(
            (s.session_id, s.full_path)
            for s in adapters.universal_list_sessions(
                home_dir=str(self.root), env_name="fixture", limit=0)
        )

    def test_ids_and_paths_identical_with_and_without_preview(self):
        with_preview = self._ids()
        with echolib.discovery_only():
            without_preview = self._ids()
        self.assertEqual(with_preview, without_preview)
        self.assertTrue(with_preview, "fixture must actually yield sessions")

    def test_metadata_degrades_but_ids_do_not(self):
        full = adapters.universal_list_sessions(
            home_dir=str(self.root), env_name="fixture", limit=0)
        self.assertTrue(any(s.first_prompt for s in full),
                        "preview mode still fills first_prompt")
        with echolib.discovery_only():
            lean = adapters.universal_list_sessions(
                home_dir=str(self.root), env_name="fixture", limit=0)
        self.assertTrue(all(not s.first_prompt for s in lean))
        self.assertTrue(all(s.summary for s in lean),
                        "summary falls back to a cheap label, not empty")


class TestBackupTreesAreNotSessions(unittest.TestCase):
    """Archived copies of transcripts must not be published as sessions."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name) / "env"

    def tearDown(self):
        self._td.cleanup()

    def test_backup_subtree_is_pruned(self):
        _write_jsonl(self.root / "backups" / "migration" / "jsonl"
                     / "sessions" / "rollout-x.jsonl", [_user_record("archived")])
        _write_jsonl(self.root / "sessions" / "live" / "chat_history.jsonl",
                     [_user_record("live one")])
        found = adapters.universal_list_sessions(
            home_dir=str(self.root), env_name="fixture", limit=0)
        paths = [s.full_path for s in found]
        self.assertEqual(len(paths), 1, paths)
        self.assertNotIn("backups", paths[0])

    def test_pruned_dirs_are_not_descended_into(self):
        _write_jsonl(self.root / "node_modules" / "pkg" / "x.jsonl",
                     [_user_record("vendored")])
        found = adapters.universal_list_sessions(
            home_dir=str(self.root), env_name="fixture", limit=0)
        self.assertEqual(found, [])


class TestLimitConvention(unittest.TestCase):
    """``limit=0`` means unlimited, in every adapter and in one place.

    Eleven adapters wrote ``items[:limit]``, which quietly returns ``[]`` for
    ``limit=0`` — so asking for everything gave nothing. Four adapters had been
    fixed one at a time; the rule now lives in ``cap`` so the next adapter
    inherits it instead of re-introducing it.
    """

    def test_zero_means_unlimited(self):
        self.assertEqual(echolib.cap([1, 2, 3], 0), [1, 2, 3])

    def test_none_means_unlimited(self):
        self.assertEqual(echolib.cap([1, 2, 3], None), [1, 2, 3])

    def test_positive_limit_still_caps(self):
        self.assertEqual(echolib.cap([1, 2, 3], 2), [1, 2])

    def test_every_list_sessions_agrees(self):
        """No adapter may reintroduce the bare ``[:limit]`` slice."""
        import re
        pattern = re.compile(r"return\s+\w+\[:limit\]\s*$")
        offenders = []
        for path in sorted((SCRIPTS / "echolib").glob("_adapters*.py")):
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line.strip()):
                    offenders.append(f"{path.name}:{n}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            "list_sessions must return cap(items, limit) so limit=0 stays "
            "'unlimited':\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
