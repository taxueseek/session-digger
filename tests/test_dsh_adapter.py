"""DSH adapter contracts: store selection and which user turns are real.

The DSH store is 16% of a live index (299 sessions) and had no test file, so
both rules below were only pinned by the machine they were written on:

1. A session dir carries one store file per format version
   (``session.jsonl.zstd`` = v0, ``session.v2.jsonl.zstd`` = v2, ...). The
   highest version is the live store; the lower ones are seeded copies and must
   not be double-counted.
2. ``user/message`` events carry machine-written context as well as typed
   input, separated by ``data.source.kind``. Getting that filter wrong is a
   silent recall hole: an allow-list dropped ``coordinator``, which is how a
   person talks to a child session (4 real messages in one measured store).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from echolib import _adapters_dsh as dsh  # noqa: E402


class TestStoreSelection(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _touch(self, name, size=10):
        p = self.dir / name
        p.write_bytes(b"x" * size)
        return p

    def test_highest_version_wins(self):
        self._touch("session.jsonl.zstd")
        self._touch("session.v2.jsonl.zstd")
        v3 = self._touch("session.v3.jsonl.zstd")
        self.assertEqual(dsh._dsh_store_file(self.dir), v3)

    def test_version_compared_as_a_number_not_a_string(self):
        """v10 must beat v9; string comparison would pick v9."""
        self._touch("session.v9.jsonl.zstd")
        v10 = self._touch("session.v10.jsonl.zstd")
        self.assertEqual(dsh._dsh_store_file(self.dir), v10,
                         "store version is a number: '10' > '9' is false as text")

    def test_store_family_is_matched_generically(self):
        """A future version must be picked up without editing a name list."""
        v7 = self._touch("session.v7.jsonl.zstd")
        self.assertEqual(dsh._dsh_store_file(self.dir), v7)

    def test_empty_store_is_not_a_store(self):
        self._touch("session.jsonl.zstd", size=0)
        self.assertIsNone(dsh._dsh_store_file(self.dir))

    def test_directory_without_a_store_is_not_a_session(self):
        (self.dir / "notes.txt").write_text("x", encoding="utf-8")
        self.assertIsNone(dsh._dsh_store_file(self.dir))

    def test_unrelated_names_are_ignored(self):
        self._touch("session.jsonl.zst")  # single t: a different family
        self._touch("session.jsonl.zstd.bak")
        self.assertIsNone(dsh._dsh_store_file(self.dir))


class TestUserTurnFilter(unittest.TestCase):
    def _msg(self, kind, text="内容"):
        data = {"content": [{"type": "text", "text": text}]}
        if kind is not None:
            data["source"] = {"kind": kind}
        return data

    def test_typed_input_is_counted(self):
        self.assertEqual(dsh._dsh_user_text(self._msg("user")), "内容")

    def test_coordinator_is_typed_input_not_injection(self):
        """Measured: 4 real user instructions in one store were dropped."""
        self.assertEqual(
            dsh._dsh_user_text(self._msg("coordinator")), "内容",
            "coordinator is how a person talks to a child session")

    def test_unknown_kind_is_counted(self):
        """A newer DSH feature is likelier than an injection."""
        self.assertEqual(dsh._dsh_user_text(self._msg("some-new-kind")), "内容")

    def test_absent_kind_is_counted(self):
        self.assertEqual(dsh._dsh_user_text(self._msg(None)), "内容")

    def test_known_injected_kinds_are_dropped(self):
        for kind in sorted(dsh._DSH_INJECTED_KINDS):
            self.assertEqual(dsh._dsh_user_text(self._msg(kind)), "",
                             "kind=%s is machine-written context" % kind)

    def test_the_measured_injected_set_is_covered(self):
        """Every kind the live stores actually carry, minus the typed ones."""
        for kind in ("plugin", "skill-catalog", "skill-invocation",
                     "agent-instructions", "agent-message", "subagent-report",
                     "subagent-settled"):
            self.assertIn(kind, dsh._DSH_INJECTED_KINDS)
        self.assertNotIn("user", dsh._DSH_INJECTED_KINDS)
        self.assertNotIn("coordinator", dsh._DSH_INJECTED_KINDS)


if __name__ == "__main__":
    unittest.main()
