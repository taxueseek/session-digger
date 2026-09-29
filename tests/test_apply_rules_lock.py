#!/usr/bin/env python3
"""apply-rules writes are serialized: concurrent appenders all survive.

Why this gate exists
--------------------
``apply-rules.sh append`` was a two-stage unlocked read-modify-write: the
shell created file and section with grep+echo, then a Python heredoc read
the whole file and rewrote it. Two concurrent appends (two sessions filing
lessons at the same time is the normal case, not an exotic one) raced in
two ways: each inserted its own ``## Session Learned Rules`` header, and
the read-modify-write lost updates — the later writer, built from the
pre-append snapshot, silently overwrote the earlier rule. The ``memory``
subcommand's sequence-number loop was the same check-then-act race, with a
truncating ``cat >`` as the loser-erasing-winner.

Both paths now run inside a single ``fcntl.flock`` critical section, and
the file/section creation lives inside it too. This test drives two real
concurrent subprocesses at the same target and asserts nothing was lost —
which the old code failed on enough runs to matter.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APPLY = REPO_ROOT / "scripts" / "apply-rules.sh"


def _spawn(rule: str, target: Path):
    return subprocess.Popen(
        ["bash", str(APPLY), "append", rule, "--target", str(target)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


class AppendLockTest(unittest.TestCase):
    def test_concurrent_appends_all_survive(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "CLAUDE.md"
            procs = [_spawn(f"并发规则{i}", target) for i in range(2)]
            for p in procs:
                self.assertEqual(p.wait(timeout=60), 0)
            text = target.read_text(encoding="utf-8")
            for i in range(2):
                self.assertIn(f"并发规则{i}", text,
                              f"rule {i} was lost — the append is not "
                              f"serialized; a read-modify-write is racing")
            # The TOCTOU half: both writers seeing a missing section used to
            # leave two headers behind.
            self.assertEqual(
                text.count("## Session Learned Rules"), 1,
                "duplicate section header — file/section creation escaped "
                "the lock",
            )

    def test_lock_file_is_hidden_and_persistent(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "CLAUDE.md"
            self.assertEqual(
                subprocess.run(
                    ["bash", str(APPLY), "append", "锁文件名", "--target", str(target)],
                    capture_output=True, text=True, timeout=60,
                ).returncode, 0)
            self.assertTrue((target.parent / ".CLAUDE.md.lock").exists(),
                            "the flock lockfile must persist (unlinking it "
                            "is itself racy) under a hidden name")


class MemoryLockTest(unittest.TestCase):
    def test_concurrent_memory_writes_all_survive(self):
        with tempfile.TemporaryDirectory() as td:
            procs = [
                subprocess.Popen(
                    ["bash", str(APPLY), "memory", f"并发教训{i}", "--project", td],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                for i in range(2)
            ]
            for p in procs:
                self.assertEqual(p.wait(timeout=60), 0)
            mem = Path(td) / ".claude" / "memory"
            lessons = sorted(mem.glob("lessons-*.md"))
            self.assertEqual(
                len(lessons), 2,
                "two same-second writes collided — the sequence-number "
                "check ran outside the lock and one lesson was truncated "
                "away",
            )


if __name__ == "__main__":
    unittest.main()
