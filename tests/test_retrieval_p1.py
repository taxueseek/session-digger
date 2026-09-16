#!/usr/bin/env python3
"""Unit gates for retrieval P1 helpers (scripts/retrieval_utils.py).

stream_contains    : P1-A — whole-file bounded search with head fast path,
                     byte-seam safety, early exit, per-file cap.
collapse_near_dups : P1-B — byte-identical folding only; the keep-longest
                     heuristic stays banned by a regression test because it
                     dropped the smaller evidence-bearing original in the
                     2026-09-16 ablation.
"""
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from retrieval_utils import (  # noqa: E402
    HEAD_WINDOW,
    MAX_EXTRA_BYTES,
    collapse_near_dups,
    stream_contains,
)


def _write(tmp, name, payload: bytes):
    p = Path(tmp) / name
    p.write_bytes(payload)
    return str(p)


class TestStreamContains(unittest.TestCase):
    def test_head_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(tmp, "a.jsonl", b"x" * 100 + b"NEEDLE" + b"y" * 10)
            self.assertTrue(stream_contains(p, "needle"))

    def test_deep_beyond_head_window_found(self):
        # the exact corpus-s2 shape: marker past the 50KB head
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(tmp, "long.jsonl", b"x" * (HEAD_WINDOW + 24_000) + b"deep-evidence-42")
            self.assertTrue(stream_contains(p, "deep-evidence-42"))
            self.assertFalse(stream_contains(p, "not-present-anywhere"))

    def test_match_spanning_chunk_seam_found(self):
        # needle straddles the 256KB chunk boundary — carry must not eat it
        with tempfile.TemporaryDirectory() as tmp:
            filler = b"x" * (HEAD_WINDOW + 262_144 - 4)
            p = _write(tmp, "seam.jsonl", filler + b"seam-hit-99" + b"z" * 32)
            self.assertTrue(stream_contains(p, "seam-hit-99"))

    def test_beyond_cap_is_honest_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = b"x" * (HEAD_WINDOW + MAX_EXTRA_BYTES + 4096) + b"tail-evidence-7"
            p = _write(tmp, "huge.jsonl", payload)
            self.assertFalse(stream_contains(p, "tail-evidence-7"))

    def test_missing_file_is_false_not_raise(self):
        self.assertFalse(stream_contains("/nonexistent/path.jsonl", "anything"))

    def test_cjk_needle_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(tmp, "cjk.jsonl", "前缀填充内容".encode() + "证据-中文-ZETA".encode() + b"x" * 16)
            self.assertTrue(stream_contains(p, "证据-中文-ZETA"))


class TestCollapseNearDups(unittest.TestCase):
    def test_byte_identical_fold_keeps_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = b"same content" * 20
            a = _write(tmp, "a.jsonl", body)
            b = _write(tmp, "b.jsonl", body)
            c = _write(tmp, "c.jsonl", body + b" different tail")
            kept, collapsed = collapse_near_dups(
                [("a", a, "x"), ("b", b, "x"), ("c", c, "x")])
            self.assertEqual([k[0] for k in kept], ["a", "c"])
            self.assertEqual(collapsed, 1)

    def test_pseudo_dups_kept(self):
        # similar prefix, different content → NOT folded
        with tempfile.TemporaryDirectory() as tmp:
            a = _write(tmp, "o.jsonl", b"base" * 200 + b" original-evidence")
            b = _write(tmp, "c.jsonl", b"base" * 200 + b" copy-variant")
            kept, collapsed = collapse_near_dups([("o", a, "x"), ("c", b, "x")])
            self.assertEqual(len(kept), 2)
            self.assertEqual(collapsed, 0)

    def test_keep_longest_heuristic_stays_banned(self):
        """2026-09-16 ablation: the evidence-bearing original was SMALLER than
        its padding copies; size-based folding dropped evidence (PR#1 rule).
        This test pins the rejection — if anyone reintroduces size/freshness
        ranking into collapse, this fails."""
        with tempfile.TemporaryDirectory() as tmp:
            orig = _write(tmp, "orig.jsonl", b"base" * 200 + b" marker-EVID")
            copy = _write(tmp, "copy.jsonl", b"base" * 200 + b" padding-padding-padding-x")
            self.assertLess(Path(orig).stat().st_size, Path(copy).stat().st_size)
            kept, collapsed = collapse_near_dups([("orig", orig, "x"), ("copy", copy, "x")])
            self.assertEqual([k[0] for k in kept], ["orig", "copy"])
            self.assertEqual(collapsed, 0)

    def test_original_order_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = b"identical-body"
            a = _write(tmp, "a.jsonl", body)
            b = _write(tmp, "b.jsonl", body)
            c = _write(tmp, "c.jsonl", body)
            kept, collapsed = collapse_near_dups(
                [("b", b, "x"), ("a", a, "x"), ("c", c, "x")])
            self.assertEqual([k[0] for k in kept], ["b"])
            self.assertEqual(collapsed, 2)


if __name__ == "__main__":
    unittest.main()
