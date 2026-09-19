"""The parse-once scope: replay a complete read, never a partial one.

The adapter path walks one transcript three to four times per session (stats,
tools, messages, identity). ``parse_once`` lets the later walks replay the
first one instead of re-parsing. That is only safe under two rules, and both
are regression-prone in opposite directions:

* a **complete** read must be replayed, or the scope buys nothing;
* an **incomplete** read must never be replayed. ``_probe_schema`` deliberately
  reads 40 records and breaks. Caching that head would make every later read of
  the session see 40 records — a silent recall hole, no error anywhere, which
  is the failure mode this codebase has already paid for twice.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from echolib import _helpers
from echolib._helpers import _iter_jsonl, parse_once


def _write_jsonl(path, n):
    with open(path, "w", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({"type": "user", "i": i}) + "\n")


class ParseOnceScopeTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "session.jsonl")
        _write_jsonl(self.path, 10)
        self.parses = 0
        real = _helpers._read_jsonl

        def counting(path):
            self.parses += 1
            return real(path)

        self._real = real
        _helpers._read_jsonl = counting
        self.addCleanup(setattr, _helpers, "_read_jsonl", real)

    def test_complete_read_is_replayed(self):
        with parse_once():
            first = list(_iter_jsonl(self.path))
            second = list(_iter_jsonl(self.path))
        self.assertEqual(10, len(first))
        self.assertEqual(first, second)
        self.assertEqual(1, self.parses,
                         "a complete pass inside the scope must parse the file once")

    def test_without_a_scope_every_read_reparses(self):
        list(_iter_jsonl(self.path))
        list(_iter_jsonl(self.path))
        self.assertEqual(2, self.parses, "no scope must mean no caching")

    def test_partial_read_is_not_replayed(self):
        """The _probe_schema shape: read a head, break, then read for real."""
        with parse_once():
            head = []
            for rec in _iter_jsonl(self.path):
                head.append(rec)
                if len(head) >= 3:
                    break
            self.assertEqual(3, len(head))
            full = list(_iter_jsonl(self.path))
        self.assertEqual(10, len(full),
                         "a partial head must not become the session's content")

    def test_partial_read_does_not_shadow_an_earlier_complete_read(self):
        with parse_once():
            full = list(_iter_jsonl(self.path))
            head = []
            for rec in _iter_jsonl(self.path):
                head.append(rec)
                if len(head) >= 3:
                    break
            again = list(_iter_jsonl(self.path))
        self.assertEqual(10, len(full))
        self.assertEqual(3, len(head))
        self.assertEqual(10, len(again))

    def test_cache_does_not_survive_the_scope(self):
        with parse_once():
            list(_iter_jsonl(self.path))
        self.assertEqual(1, self.parses)
        list(_iter_jsonl(self.path))
        self.assertEqual(2, self.parses, "the cache must be dropped on scope exit")

    def test_unreadable_file_caches_nothing(self):
        """An OSError must not be remembered as 'this file is empty'."""
        missing = os.path.join(self.dir.name, "gone.jsonl")
        with parse_once():
            self.assertEqual([], list(_iter_jsonl(missing)))
            _write_jsonl(missing, 4)  # appears mid-scope
            self.assertEqual(4, len(list(_iter_jsonl(missing))),
                             "an unreadable pass must not be cached as empty")
        os.remove(missing)
        self.assertEqual([], list(_iter_jsonl(missing)),
                         "outside the scope the silent-empty contract is unchanged")

    def test_oserror_contract_is_unchanged_outside_a_scope(self):
        missing = os.path.join(self.dir.name, "nope.jsonl")
        self.assertEqual([], list(_iter_jsonl(missing)))

    def test_scope_nests_and_restores(self):
        with parse_once():
            list(_iter_jsonl(self.path))
            with parse_once():
                list(_iter_jsonl(self.path))
                list(_iter_jsonl(self.path))
            list(_iter_jsonl(self.path))
        self.assertEqual(2, self.parses)

    def test_a_different_file_is_parsed_separately(self):
        other = os.path.join(self.dir.name, "other.jsonl")
        _write_jsonl(other, 7)
        with parse_once():
            self.assertEqual(10, len(list(_iter_jsonl(self.path))))
            self.assertEqual(7, len(list(_iter_jsonl(other))))
            self.assertEqual(10, len(list(_iter_jsonl(self.path))))
        self.assertEqual(2, self.parses)


class AdapterPathParsesOnceTest(unittest.TestCase):
    """End-to-end: one adapter-path session must parse its transcript once.

    ``_compute_session`` reads the same file for stats, then tools, then
    messages. The unit tests above pin the scope's contract; this one pins that
    the builder actually uses it, which is the part that delivers the rebuild
    speedup and the part a future edit could quietly drop.
    """

    def test_one_session_is_parsed_once(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        from index_builder import _builder, _session_analysis
        from echolib import _helpers

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = os.path.join(td.name, "session.jsonl")
        _write_jsonl(path, 40)

        # A layout the single-pass reader must refuse, so the adapter path runs.
        self.assertTrue(
            _session_analysis._should_skip_single_pass(path, "universal"),
            "this test only means something on the adapter path")

        # Count *completed* passes only. A read the caller abandons never
        # reaches the append, which is exactly the distinction that matters:
        # the schema probe is allowed to read a 40-record head cheaply, and it
        # must not be mistaken for a full pass.
        completed = []
        started = []
        real = _helpers._read_jsonl

        def counting(p):
            started.append(str(p))

            def run():
                n = 0
                for rec in real(p):
                    n += 1
                    yield rec
                completed.append(n)

            return run()

        _helpers._read_jsonl = counting
        self.addCleanup(setattr, _helpers, "_read_jsonl", real)

        task = ("universal:fixture", path, "universal", 12345.0, "fp", None, None)
        _builder._compute_session(task)

        self.assertEqual([40], completed,
                         f"adapter path made {len(completed)} full passes "
                         f"{completed}; the parse-once scope is not in effect")
        self.assertLess(len(started), 5,
                        "the adapter path should not open the transcript repeatedly")


class RealTranscriptTest(unittest.TestCase):
    """The scope must not change what a real adapter reads."""

    def test_records_are_identical_inside_and_outside(self):
        entries = []
        for root, _dirs, files in os.walk(os.path.expanduser("~/.claude/projects")):
            for name in files:
                if name.endswith(".jsonl"):
                    entries.append(os.path.join(root, name))
            if len(entries) >= 12:
                break
        if not entries:
            self.skipTest("no Claude transcripts on this machine")

        for path in entries:
            outside = list(_iter_jsonl(path))
            with parse_once():
                inside = list(_iter_jsonl(path))
                replay = list(_iter_jsonl(path))
            self.assertEqual(outside, inside, path)
            self.assertEqual(outside, replay, path)


if __name__ == "__main__":
    unittest.main()
