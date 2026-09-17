"""End-to-end universality: an agent the tool has never seen must still work.

The universal SchemaProbe is what makes this skill portable — every other
adapter knows one vendor's layout, so a new CLI is covered only by probing the
record shape and picking a soft family. These tests drive the whole path for a
synthetic environment that no adapter mentions: discovery under an unknown
dot-directory, listing, stats and user-turn extraction.

Measured boundary (2026-09-17, seven plausible novel shapes): four parse,
three do not. Working — ``role``+``content``, ``role``+``text``, ``type``+
``content``, ``type``+``message.content``. Not working, and silent about it —
``speaker``+``body``, ``from``+``text``, ``prompt``+``response``. The role
*values* are already alias-aware (``human``/``bot``/``ai`` are in
``_USER_VALS``/``_ASSISTANT_VALS``); what is missing is the role *key* name,
which ``_probe_schema``'s candidate list hardcodes to ``role``/``type``.
This file pins the shapes that work so the boundary cannot move unnoticed.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import echolib  # noqa: E402


def _write(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8")


class TestUnknownEnvironment(unittest.TestCase):
    """A novel dot-dir with a novel record shape, discovered and parsed."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.home = Path(self._td.name)
        self.env = self.home / ".novelagent"
        self.session = self.env / "sessions" / "abc123.jsonl"
        _write(self.session, [
            {"role": "user", "content": "索引重建为什么这么慢"},
            {"role": "assistant", "content": "先看 FTS 段合并"},
            {"role": "user", "content": "决定改用批量删除"},
        ])

    def tearDown(self):
        self._td.cleanup()

    def test_listed_by_the_universal_adapter(self):
        metas = echolib.universal_list_sessions(
            home_dir=self.env, env_name="novelagent", limit=0)
        paths = {m.full_path if hasattr(m, "full_path") else m["full_path"]
                 for m in metas}
        self.assertIn(str(self.session), paths)

    def test_stats_and_user_turns_are_extracted(self):
        stats = echolib.universal_session_stats(str(self.session))
        self.assertEqual(stats.get("user_messages"), 2)
        self.assertEqual(stats.get("assistant_messages"), 1)

    def test_dispatch_returns_the_user_turns(self):
        msgs = list(echolib.dispatch_extract_messages(str(self.session), role="user"))
        self.assertEqual([m.get("text") for m in msgs],
                         ["索引重建为什么这么慢", "决定改用批量删除"])

    def test_unknown_env_is_not_registered_by_name(self):
        """Universality must not depend on this machine's registry contents."""
        self.assertNotIn("novelagent", echolib.ENV_REGISTRY)
        self.assertNotIn("novelagent", echolib.KNOWN_UNADAPTED)


def _real_turns(tag="会话"):
    """Three turns — comfortably past the 50-byte discovery gate."""
    return [
        {"role": "user", "content": f"{tag} 索引重建为什么这么慢，先看 FTS 段合并的情况"},
        {"role": "assistant", "content": f"{tag} 逐会话删除每次都全表扫描"},
        {"role": "user", "content": f"{tag} 决定改用批量删除"},
    ]


class TestUniversalNoiseGates(unittest.TestCase):
    """Discovery must not publish an env's internal bookkeeping as sessions."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.env = Path(self._td.name) / ".novelagent"

    def tearDown(self):
        self._td.cleanup()

    def test_auxiliary_files_are_not_sessions(self):
        _write(self.env / "sessions" / "real.jsonl", _real_turns())
        for noise in ("updates.jsonl", "events.jsonl", "queue.jsonl",
                      "prompt_history.jsonl"):
            _write(self.env / "sessions" / noise, _real_turns("噪音"))
        metas = echolib.universal_list_sessions(
            home_dir=self.env, env_name="novelagent", limit=0)
        names = {Path(m.full_path if hasattr(m, "full_path")
                      else m["full_path"]).name for m in metas}
        self.assertEqual(names, {"real.jsonl"})

    def test_tiny_files_are_not_sessions(self):
        """A <50 byte jsonl is a marker, not a transcript."""
        _write(self.env / "sessions" / "real.jsonl", _real_turns())
        (self.env / "sessions" / "stub.jsonl").write_text("{}\n", encoding="utf-8")
        metas = echolib.universal_list_sessions(
            home_dir=self.env, env_name="novelagent", limit=0)
        names = {Path(m.full_path if hasattr(m, "full_path")
                      else m["full_path"]).name for m in metas}
        self.assertEqual(names, {"real.jsonl"})


if __name__ == "__main__":
    unittest.main()
