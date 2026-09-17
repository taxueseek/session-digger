"""Robustness of the ingestion path against malformed third-party data.

Two failure classes, both measured on this machine:

1. **Non-numeric counter fields.** Token counters come out of a dozen agent
   JSONL dialects. ``"input_tokens": null`` in one transcript raised TypeError
   from ``_compute_session`` — and because those sums run *outside* the
   per-session error handling, the exception escaped ``pool.map`` and aborted
   the entire build rather than skipping one session.
2. **Unreadable compressed stores.** A truncated or half-written ``.zstd``
   yielded zero records and printed nothing, which is indistinguishable from an
   empty session and, in DSH's case, from a deleted transcript.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import index_builder._session_analysis as analysis  # noqa: E402
import index_builder._builder as builder  # noqa: E402
from echolib._helpers import _iter_jsonl  # noqa: E402


class TestCounterCoercion(unittest.TestCase):
    def test_as_int_never_raises(self):
        for value, expected in [
            (None, 0), (True, 0), (False, 0), ("", 0), ("abc", 0),
            (0, 0), (7, 7), (12.9, 12), ("12", 12), ("12.5", 12),
            (float("inf"), 0), ([1], 0), ({"a": 1}, 0),
        ]:
            got = analysis._as_int(value)
            self.assertEqual(got, expected, "value=%r" % (value,))
            self.assertIsInstance(got, int)


class TestMalformedUsageDoesNotAbortTheBuild(unittest.TestCase):
    """One bad field must skip one session, not the whole batch."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _session(self, usage, name="s.jsonl"):
        p = self.dir / name
        records = [
            {"type": "user", "message": {"role": "user", "content": "你好"},
             "timestamp": "2026-09-17T00:00:00Z"},
            {"type": "assistant",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "hi"}],
                         "usage": usage},
             "timestamp": "2026-09-17T00:00:01Z"},
        ]
        p.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                     encoding="utf-8")
        return str(p)

    def test_every_malformed_counter_is_survivable(self):
        cases = [
            {"input_tokens": None, "output_tokens": 5},
            {"input_tokens": "12", "output_tokens": "3"},
            {"input_tokens": "abc", "output_tokens": 5},
            {"input_tokens": 12.7, "output_tokens": 5},
            {"total_tokens": None},
            {"total_tokens": "not a number"},
            {"input_tokens": True, "output_tokens": 5},
            {"input_tokens": [3], "output_tokens": 5},
            {},
        ]
        for i, usage in enumerate(cases):
            path = self._session(usage, "c%d.jsonl" % i)
            result = builder._compute_session(
                ("x", path, "claude", 1.0, "h", "[]", None))
            self.assertIsNone(result[4], "usage=%r raised: %s" % (usage, result[4]))
            self.assertIsNotNone(result[1])

    def test_good_counters_still_add_up(self):
        path = self._session({"input_tokens": 10, "cache_read_input_tokens": 90,
                              "output_tokens": 5})
        row = builder._compute_session(("x", path, "claude", 1.0, "h", "[]", None))[1]
        # Row layout follows the INSERT column list; assert on the values the
        # coercion must preserve rather than on a substring of the blob.
        names = ["id", "project_path", "agent", "created", "modified",
                 "message_count", "user_messages", "assistant_messages",
                 "tool_calls", "errors", "compactions", "total_tokens", "branch"]
        rec = dict(zip(names, row))
        self.assertEqual(rec["total_tokens"], 15, "input 10 + output 5")
        self.assertEqual(rec["user_messages"], 1)
        self.assertIn(0.9, row, "cache hit rate 90/(10+90) must survive")


class TestCompressedReadFailuresAreLoud(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_non_zstd_content_warns_and_yields_nothing(self):
        p = self.dir / "fake.jsonl.zstd"
        p.write_bytes(b"definitely not a zstd frame")
        with self.assertLogs("echolib", level="WARNING") as cap:
            records = list(_iter_jsonl(p))
        self.assertEqual(records, [])
        self.assertTrue(any("zstd" in m for m in cap.output),
                        "a corrupt store must not be silent: %r" % cap.output)

    @unittest.skipUnless(shutil.which("zstd"), "zstd CLI not installed")
    def test_truncated_frame_warns(self):
        raw = self.dir / "raw.jsonl"
        raw.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
        good = self.dir / "good.jsonl.zstd"
        subprocess.run([shutil.which("zstd"), "-q", "-f", str(raw), "-o", str(good)],
                       check=True)
        data = good.read_bytes()
        good.write_bytes(data[:max(1, len(data) // 2)])
        with self.assertLogs("echolib", level="WARNING") as cap:
            records = list(_iter_jsonl(good))
        self.assertEqual(records, [])
        self.assertTrue(any("premature end" in m or "failed" in m for m in cap.output))

    @unittest.skipUnless(shutil.which("zstd"), "zstd CLI not installed")
    def test_a_healthy_store_still_reads_silently(self):
        raw = self.dir / "raw.jsonl"
        raw.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
        good = self.dir / "good.jsonl.zstd"
        subprocess.run([shutil.which("zstd"), "-q", "-f", str(raw), "-o", str(good)],
                       check=True)
        records = list(_iter_jsonl(good))
        self.assertEqual(len(records), 2)


if __name__ == "__main__":
    unittest.main()
