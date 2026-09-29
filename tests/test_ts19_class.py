#!/usr/bin/env python3
"""Every timestamp truncation routes through ``_common.ts19``.

Why this gate exists
--------------------
The idiom ``ts[:19]`` was re-derived at a dozen call sites across eight
files, and each site rolled its own guard — none of them complete:

  * a JSON ``null`` timestamp crashed ``cmd_tools`` and ``--decisions``
    rendering (``dict.get(k, "")`` returns None for an explicit null, and
    slicing None raises);
  * an integer epoch timestamp is *truthy*, so the popular
    ``ts[:19] if ts else ""`` guard fell straight through and died on the
    slice — that took down the whole claude two-pass tool extraction and
    the session-analysis tools assembly for such a session;
  * the surviving copies were benign only because their callers happened
    to wrap them in ``try/except (ValueError, TypeError)`` — the guard
    lived at the call site, so the next copy could forget it.

``ts19`` is now the one implementation, and this file pins both halves of
that: the function's behaviour on every shape it must survive, and the
source-tree invariant that no raw timestamp slice exists outside it.
Removing the helper or re-deriving the slice at a new call site turns
this gate red — which is the point.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _common import ts19  # noqa: E402


class Ts19BehaviourTest(unittest.TestCase):
    """The shapes that broke real renders, plus the pass-through contract."""

    def test_valid_iso_string_is_truncated_to_seconds(self):
        self.assertEqual(ts19("2026-09-29T12:34:56.789Z"), "2026-09-29T12:34:56")

    def test_plain_date_only_string_passes_through(self):
        # Shorter than 19 chars: returned as-is, not padded, not rejected.
        self.assertEqual(ts19("2026-09-29"), "2026-09-29")

    def test_json_null_returns_default_not_crash(self):
        # The shape that took down the --decisions render pre-v0.9.30.
        self.assertEqual(ts19(None), "")

    def test_integer_epoch_returns_default_not_crash(self):
        # The shape that took down the claude tool extraction pre-v0.9.33:
        # truthy, so the old truthiness guard let it through to the slice.
        self.assertEqual(ts19(1759104000), "")
        self.assertEqual(ts19(1759104000.5), "")

    def test_empty_string_returns_default(self):
        self.assertEqual(ts19(""), "")

    def test_explicit_default_is_honoured(self):
        # sd-recall's displays were built on "?" for unknown timestamps.
        self.assertEqual(ts19(None, "?"), "?")
        self.assertEqual(ts19(1759104000, "?"), "?")

    def test_non_timestamp_types_return_default(self):
        # Anything else a schema might smuggle into the field.
        self.assertEqual(ts19({"iso": "2026"}), "")
        self.assertEqual(ts19(["2026"]), "")
        self.assertEqual(ts19(b"2026-09-29T12:34:56"), "")


class SingleDefinitionGateTest(unittest.TestCase):
    """No raw timestamp slice may exist outside ``_common.ts19``.

    The class died twelve times across eight files; the gate keeps it dead.
    Scanning the source (not the AST) is deliberate, same as the other
    contract gates in this repo: the pattern is a two-character idiom that
    an AST walker would only see after the file parses, and a half-written
    edit should fail loudly too.
    """

    def test_no_raw_slice_outside_the_helper(self):
        offenders = []
        for path in sorted(SCRIPTS.rglob("*.py")):
            if path.resolve() == (SCRIPTS / "_common.py").resolve():
                continue
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                if "[:19]" in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            "raw timestamp slice(s) found outside _common.ts19 — route them "
            "through ts19() so the next malformed shape is one fix, not a "
            "thirteenth copy:\n" + "\n".join(offenders),
        )

    def test_echolib_reexports_the_canonical_helper(self):
        # The echolib family's spelling must be the same object, not a copy.
        from echolib import _helpers

        self.assertIs(_helpers.ts19, ts19)


if __name__ == "__main__":
    unittest.main()
