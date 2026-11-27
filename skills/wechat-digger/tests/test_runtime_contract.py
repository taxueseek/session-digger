#!/usr/bin/env python3
"""Tests for the runtime contract and cross-version portability.

Context: the suite previously passed 108/108 on Homebrew Python 3.14 while
failing 11 on the macOS system 3.9 interpreter, all from a single call to
``int.bit_count()`` (3.10+ only). Nothing in the tree declared which
interpreter was supported, so the failure surfaced as a mid-pipeline
AttributeError instead of a clear precondition error.

These tests lock in three properties:
  1. ``check_python`` classifies versions correctly at the boundaries.
  2. ``require_python`` aborts only for genuinely unsupported versions.
  3. The popcount fallback is numerically identical to ``bit_count``, and
     the acquire stack's duplicate floor declaration cannot silently drift
     from the canonical one.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from analyze import _popcount, hamming, simhash  # noqa: E402
from runtime_contract import (  # noqa: E402
    MIN_PYTHON,
    TESTED_THROUGH,
    check_python,
    require_python,
)


class TestCheckPython(unittest.TestCase):
    def test_running_interpreter_is_supported(self):
        verdict = check_python()
        self.assertTrue(verdict["supported"], verdict["detail"])
        self.assertEqual(verdict["status"], "ok", verdict["detail"])

    def test_reports_current_version(self):
        verdict = check_python((3, 12, 1))
        self.assertEqual(verdict["current"], "3.12")
        self.assertEqual(verdict["minimum"], "3.8")
        self.assertEqual(verdict["recommended"], "3.12")

    def test_old_interpreter_is_unsupported(self):
        for version in ((2, 7), (3, 6), (3, 7)):
            with self.subTest(version=version):
                verdict = check_python(version)
                self.assertFalse(verdict["supported"])
                self.assertEqual(verdict["status"], "unsupported")
                # The message must name both sides of the mismatch.
                self.assertIn(verdict["current"], verdict["detail"])
                self.assertIn(verdict["minimum"], verdict["detail"])

    def test_minimum_boundary_is_inclusive(self):
        """Exactly MIN_PYTHON must pass - off-by-one here would reject the
        oldest supported interpreter."""
        verdict = check_python((MIN_PYTHON[0], MIN_PYTHON[1], 0))
        self.assertTrue(verdict["supported"])
        self.assertEqual(verdict["status"], "ok")

    def test_one_below_minimum_is_rejected(self):
        verdict = check_python((MIN_PYTHON[0], MIN_PYTHON[1] - 1, 99))
        self.assertFalse(verdict["supported"])

    def test_newer_than_tested_is_allowed_but_flagged(self):
        newer = (TESTED_THROUGH[0], TESTED_THROUGH[1] + 1, 0)
        verdict = check_python(newer)
        self.assertTrue(verdict["supported"], "future versions must not hard-fail")
        self.assertFalse(verdict["tested"])
        self.assertEqual(verdict["status"], "untested")
        self.assertIn("not been validated", verdict["detail"].lower())

    def test_tested_ceiling_is_inclusive(self):
        verdict = check_python((TESTED_THROUGH[0], TESTED_THROUGH[1], 0))
        self.assertTrue(verdict["supported"])
        self.assertTrue(verdict["tested"])
        self.assertEqual(verdict["status"], "ok")

    def test_verdict_is_json_serializable(self):
        """doctor embeds this dict in its JSON output - a non-serializable
        value here would break the whole health report."""
        import json

        json.dumps(check_python())

    def test_accepts_explicit_version_tuple(self):
        """Callers may pass a synthetic version; tagging must use it, not
        the real interpreter."""
        self.assertEqual(check_python((3, 9))["current"], "3.9")
        self.assertEqual(check_python((3, 11, 7))["current"], "3.11")


class TestRequirePython(unittest.TestCase):
    def test_passes_on_supported_version(self):
        require_python((3, 12, 0))  # must not raise

    def test_passes_on_untested_newer_version(self):
        require_python((TESTED_THROUGH[0], TESTED_THROUGH[1] + 1, 0))

    def test_aborts_on_unsupported_version(self):
        with self.assertRaises(SystemExit) as ctx:
            require_python((3, 6, 0))
        message = str(ctx.exception)
        self.assertIn("3.6", message)
        self.assertIn("3.8", message)
        # Message must be actionable: it points at the venv escape hatch.
        self.assertIn(".venv", message)


class TestPopcountPortability(unittest.TestCase):
    """``_popcount`` replaced a bare ``int.bit_count()`` call, so its values
    must match the native implementation exactly."""

    def test_matches_native_bit_count(self):
        if not hasattr(int, "bit_count"):
            self.skipTest("native bit_count unavailable on this interpreter")
        for n in list(range(0, 4096)) + [2**k for k in range(0, 64)]:
            with self.subTest(n=n):
                self.assertEqual(_popcount(n), n.bit_count())

    def test_small_values(self):
        self.assertEqual(_popcount(0), 0)
        self.assertEqual(_popcount(1), 1)
        self.assertEqual(_popcount(7), 3)
        self.assertEqual(_popcount(255), 8)

    def test_hamming_known_distances(self):
        self.assertEqual(hamming(0, 0), 0)
        self.assertEqual(hamming(0, 7), 3)
        self.assertEqual(hamming(0b1011, 0b1101), 2)
        self.assertEqual(hamming(0xFF, 0x00), 8)

    def test_hamming_is_symmetric_and_zero_on_self(self):
        for a, b in ((12345, 67890), (0, 1), (2**40, 2**40 + 1)):
            with self.subTest(a=a, b=b):
                self.assertEqual(hamming(a, b), hamming(b, a))
                self.assertEqual(hamming(a, a), 0)

    def test_simhash_self_distance_is_zero(self):
        for text in ("测试文本", "hello world", "", "重复重复重复"):
            with self.subTest(text=text):
                self.assertEqual(hamming(simhash(text), simhash(text)), 0)

    def test_simhash_distinguishes_unrelated_text(self):
        distance = hamming(simhash("今天天气很好"), simhash("股票大涨了"))
        self.assertGreater(distance, 0, "unrelated text must not collide")


class TestAcquireFloorStaysInSync(unittest.TestCase):
    """Public build ships no key-extraction stack: ``extract_keys.py`` must
    stay absent. The internal build keeps a floor-check sync test for it;
    here we guard the opposite direction — the file must not silently come
    back into the public package."""

    def test_extract_keys_not_distributed(self):
        self.assertFalse(
            (_SCRIPTS / "acquire" / "extract_keys.py").exists(),
            "public build must not carry extract_keys.py",
        )

    def test_no_decrypt_stack_files(self):
        for name in ("decrypt_all_dbs.py", "list_contacts.py", "search_sns.py"):
            self.assertFalse(
                (_SCRIPTS / "acquire" / name).exists(),
                f"public build must not carry {name}",
            )

    def test_no_bare_bit_count_calls_outside_portability_helper(self):
        """Guards against reintroducing a direct 3.10-only call anywhere in
        the scripts tree.

        Uses AST rather than substring matching: a naive text scan flags
        ``int.bit_count()`` in docstrings and comments (which mention the
        API precisely because the portability shim exists). Counting
        keywords without checking semantic context produced false positives
        in an earlier audit, so this walks call nodes instead.
        """
        import ast

        offenders = []
        for path in sorted(_SCRIPTS.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute) or func.attr != "bit_count":
                    continue
                # The portability helper in analyze.py is the one sanctioned
                # call site.
                if path.name == "analyze.py" and isinstance(func.value, ast.Name):
                    if func.value.id == "n":
                        continue
                offenders.append(f"{path.relative_to(_SCRIPTS)}:{node.lineno}")
        self.assertEqual(
            offenders,
            [],
            "direct int.bit_count() calls break Python 3.9; use analyze._popcount",
        )


if __name__ == "__main__":
    unittest.main()
