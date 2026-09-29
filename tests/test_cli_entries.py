#!/usr/bin/env python3
"""Every CLI entry opens the door: --help exits 0 with real usage.

Why this gate exists
--------------------
Two failures this round proved nobody was watching the doors:

  * ``chat-profiles.py`` died on *import* with a traceback for any argument
    at all — v0.9.31's same-name-merge fix rewrote its import block into
    ``from _common import hashlib`` (hashlib is stdlib, not _common) plus
    ``import read_json, write_json, ...`` (function names imported as
    modules). Dead for a full release cycle because no test imported it.
  * ``herdr-search.sh --help`` treated "--help" as a search keyword,
    searched the corpus for it, and reported rc=0 — the help request was
    indistinguishable from success.

So the contract is uniform: every entry point responds to ``--help`` with
exit 0, non-empty output, and no traceback. The sweep itself runs in
``cli_help_sweep.sh`` (see its header for the exclusion list and the reason
each excluded file is not a CLI entry); this file runs that script — one
fixed argv, no interpolation — and asserts the result.
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SWEEP = REPO_ROOT / "tests" / "cli_help_sweep.sh"


class CliHelpGateTest(unittest.TestCase):
    def test_every_entry_answers_help(self):
        proc = subprocess.run(
            ["/usr/bin/env", "bash", str(SWEEP)],
            capture_output=True,
            text=True,
            timeout=300,
            shell=False,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"some CLI entries fail the --help contract:\n{proc.stdout}\n{proc.stderr}",
        )
        self.assertIn("ALL-OK", proc.stdout)

        # The gate goes blind if discovery finds almost nothing — say so
        # instead of passing vacuously.
        ok_lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("OK ")]
        self.assertGreaterEqual(
            len(ok_lines), 20,
            f"only {len(ok_lines)} entries discovered; check the sweep's "
            f"exclusion list and the scripts/ layout",
        )


if __name__ == "__main__":
    unittest.main()
