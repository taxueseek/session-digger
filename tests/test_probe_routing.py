#!/usr/bin/env python3
"""Routing must not fail silently.

Two real, measured failures on this machine (2026-09-17), both of which indexed
a session with **zero** searchable text and no error anywhere:

  * A codebuddy transcript (1.3 MB, 54 messages, 75 KB of prose) whose records
    are flat ``role``+``content`` with one stray ``message`` dict among them.
    The probe's head sample saw the stray dict, chose the ``nested_message``
    family, read ``message.content`` — and extracted nothing. Correct answer:
    54 messages.
  * A commandcode transcript (0.9 MB, 111 messages) that content-detection
    scored as ``claude`` on the strength of ``cwd``+``sessionId`` alone, so the
    Claude reader was handed a file with none of its fields. Correct answer:
    111 messages.

Both are the same defect class the repo already treats as a bug elsewhere: a
value that is silently wrong is worse than a loud failure. The tests below pin
the repairs and, just as importantly, the shape of the repaired default — an
unreadable file keeps its historical label rather than being relabelled by the
new fall-through.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from echolib import _adapters as A  # noqa: E402


def _write(records) -> str:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    for rec in records:
        tmp.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.close()
    return tmp.name


def _telemetry(n, i0=0):
    """Hook/telemetry stubs: no role, no type, no content."""
    return [{"hookName": f"h{i0 + i}", "eventName": "PreToolUse", "exitCode": 0,
             "duration": 12, "blocked": False, "timestamp": 1785000000 + i0 + i}
            for i in range(n)]


class ProbeCostTest(unittest.TestCase):
    """The probe reads a bounded head — deliberately, and by measurement.

    A signal-seeking window (keep scanning until N schema-bearing records are
    found, up to 4000) was implemented, measured and **removed**: across all
    2,027 file-backed sessions the first record carrying schema sits at
    position 1 (median) and 2 (max), so nothing was recovered by scanning
    deeper, while 147 large non-transcript files paid a full pass and rebuild
    time went 22.2 s → 34.1 s. This test keeps that bound from being re-broken.
    """

    def test_probe_does_not_scan_a_whole_non_transcript(self):
        path = _write(_telemetry(2000))
        seen = {"n": 0}
        real = A._iter_jsonl

        def counting(p):
            for rec in real(p):
                seen["n"] += 1
                yield rec

        A._iter_jsonl = counting
        try:
            A._probe_schema(path, force=True)
        finally:
            A._iter_jsonl = real
        self.assertLessEqual(seen["n"], 40)

    def test_pure_telemetry_file_stays_unreadable(self):
        """A non-transcript must not be given a schema it does not have."""
        path = _write(_telemetry(400))
        schema = A._probe_schema(path, force=True)
        self.assertEqual("unknown", schema["style"])
        self.assertEqual([], list(A.universal_extract_messages(path)))


class FamilyVerificationTest(unittest.TestCase):
    """A family that reads nothing is the wrong family."""

    def test_stray_message_dict_does_not_hijack_the_family(self):
        records = [
            {"role": "user", "content": "第一句", "timestamp": 1},
            {"role": "assistant", "content": "第二句", "timestamp": 2},
            {"role": "user", "content": "第三句", "timestamp": 3, "message": {"id": "x"}},
        ]
        path = _write(records)
        schema = A._probe_schema(path, force=True)
        self.assertEqual("flat_role", schema["style"])
        self.assertEqual(3, len(list(A.universal_extract_messages(path))))

    def test_genuine_nested_message_family_kept(self):
        """The verification must not push a real nested format to flat_role."""
        records = [
            {"type": "user", "message": {"role": "user", "content": "嵌套格式的提问"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "嵌套格式的回答"}},
        ]
        path = _write(records)
        schema = A._probe_schema(path, force=True)
        self.assertEqual("nested_message", schema["style"])
        self.assertEqual(2, len(list(A.universal_extract_messages(path))))

    def test_accepted_schema_is_verified_to_read_text(self):
        path = _write([
            {"role": "user", "content": "有正文", "timestamp": 1},
            {"role": "assistant", "content": "也有", "timestamp": 2},
        ])
        schema = A._probe_schema(path, force=True)
        samples = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
        self.assertTrue(A._schema_reads_text(schema, samples))

    def test_unreadable_file_keeps_its_historical_label(self):
        """Repair, not a new default: a hook-only file must not be relabelled."""
        path = _write(_telemetry(60) + [{"intent": ""}])
        schema = A._probe_schema(path, force=True)
        self.assertIn(schema["style"], ("unknown", "flat"))
        self.assertEqual([], list(A.universal_extract_messages(path)))


BASE_SCHEMA = {
    "style": "unknown", "type_path": ["type"], "content_path": ["content"],
    "timestamp_field": "timestamp", "model_field": None, "tool_style": None,
    "family": "unknown", "skip_system": True,
}


class FamilyAttemptIsolationTest(unittest.TestCase):
    """A rejected family must not leave a field behind for the accepted one.

    ``_probe_schema`` walks families until one reads text. Editing one shared
    dict made the fall-through inherit the rejected family's fields: after
    ``nested_message`` was rejected, ``model_field`` still pointed at
    ``message.model`` while ``flat_role`` read flat records — and the generic
    "find a model key" pass is skipped whenever ``model_field`` is set, so the
    session reported a model scraped from a stray nested field instead of the
    one its records actually carry (measured: ``nested-model`` vs
    ``top-level-model`` on the same file).
    """

    SAMPLES = [
        {"role": "user", "content": "flat one", "model": "top-level-model"},
        {"role": "assistant", "content": "flat two", "model": "top-level-model",
         "message": {"model": "nested-model"}},
        {"role": "user", "content": "flat three", "model": "top-level-model"},
    ]

    def test_apply_family_returns_a_new_schema(self):
        out = A._apply_family(BASE_SCHEMA, "summary_card", [])
        self.assertEqual("summary_card", out["style"])
        self.assertEqual("message_summary_time", out["timestamp_field"])

    def test_apply_family_leaves_its_base_untouched(self):
        A._apply_family(BASE_SCHEMA, "summary_card", self.SAMPLES)
        self.assertEqual("unknown", BASE_SCHEMA["style"])
        self.assertEqual("timestamp", BASE_SCHEMA["timestamp_field"])
        self.assertIsNone(BASE_SCHEMA["model_field"])

    def test_rejected_family_does_not_donate_its_model_path(self):
        rejected = A._apply_family(BASE_SCHEMA, "nested_message", self.SAMPLES)
        self.assertEqual(["message", "model"], rejected["model_field"])
        accepted = A._apply_family(BASE_SCHEMA, "flat_role", self.SAMPLES)
        self.assertIsNone(accepted["model_field"])

    def test_accepted_family_reads_the_model_its_own_records_carry(self):
        path = _write(self.SAMPLES)
        schema = A._probe_schema(path, force=True)
        self.assertEqual("flat_role", schema["style"])
        self.assertEqual("model", schema["model_field"])
        self.assertEqual("top-level-model", A._schema_get_model(schema, self.SAMPLES[1]))
        self.assertEqual(3, len(list(A.universal_extract_messages(path))))


class ClaudeGateTest(unittest.TestCase):
    """`type_field` is a documented requirement of the Claude signature."""

    COD_BASE = {"sessionId": "s-1", "cwd": "/Users/x/project", "id": "r-1", "parentId": "r-0"}

    def test_cwd_and_session_id_alone_are_not_claude(self):
        records = [
            dict(self.COD_BASE, role="user", content="命令码格式的提问", gitBranch="main"),
            dict(self.COD_BASE, role="assistant", content="命令码格式的回答", gitBranch="main"),
        ]
        path = _write(records)
        self.assertNotEqual("claude", A._detect_format_from_content(path))
        self.assertEqual("universal", A.dispatch_resolve_agent(path))

    def test_real_claude_shape_still_resolves_to_claude(self):
        records = [
            {"type": "user", "sessionId": "s", "cwd": "/p", "uuid": "u1",
             "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "sessionId": "s", "cwd": "/p", "uuid": "u2",
             "message": {"role": "assistant", "model": "claude-sonnet-4-5",
                         "content": [{"type": "text", "text": "hello"}]}},
        ]
        path = _write(records)
        self.assertEqual("claude", A._detect_format_from_content(path))


if __name__ == "__main__":
    unittest.main()
