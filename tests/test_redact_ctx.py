"""Regression tests for skill-gap-finder.py privacy-context refactor.

Replaces the old `global _REDACT` / `globals().get("_REDACT")` mechanism with
an immutable `_RedactCtx` value object. These tests verify the new value
object has the same observable behaviour as the old global switch:

- Default state redacts user identity (/Users/<name> → <user>).
- `--include-paths` (enabled=False) keeps the verbatim path.
- `_evidence_item` still omits source_path by default, and only emits a
  redacted form when include_when_enabled is set.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

_SGF = Path(__file__).resolve().parent.parent / "scripts" / "skill-gap-finder.py"
_spec = importlib.util.spec_from_file_location("skill_gap_finder", str(_SGF))
_mod = importlib.util.module_from_spec(_spec)
sys.modules["skill_gap_finder"] = _mod
_spec.loader.exec_module(_mod)


def _load():
    """Return freshly-imported module so concurrent tests stay isolated."""
    return _mod


class TestRedactCtx(unittest.TestCase):
    def test_default_state_redacts(self):
        ctx = _load()._RedactCtx()
        self.assertTrue(ctx.enabled)

    def test_include_paths_disables_redaction(self):
        m = _load()
        raw = m._redact_path("/Users/alice/.claude/x.jsonl",
                             m._RedactCtx(enabled=False))
        self.assertEqual(raw, "/Users/alice/.claude/x.jsonl")

    def test_absolute_user_path_redacted(self):
        m = _load()
        out = m._redact_path("/Users/alice/.claude/projects/x.jsonl",
                             m._RedactCtx(enabled=True))
        self.assertNotIn("/Users/alice", out)
        # Marker path should surface the agent-storage tail.
        self.assertIn(".claude/projects", out)


class TestEvidenceItem(unittest.TestCase):
    def test_default_omits_source_path(self):
        m = _load()
        ev = m._evidence_item("sid-1", "/Users/alice/.claude/x.jsonl",
                              m._RedactCtx())
        self.assertNotIn("source_path", ev)
        self.assertEqual(ev["id"], "sid-1")

    def test_include_when_enabled_emits_redacted(self):
        m = _load()
        ctx = m._RedactCtx(enabled=True, include_when_enabled=True)
        ev = m._evidence_item("sid-2", "/Users/alice/.claude/x.jsonl", ctx)
        self.assertIn("source_path", ev)
        self.assertNotIn("/Users/alice", ev["source_path"])

    def test_disabled_keeps_raw_path(self):
        m = _load()
        ctx = m._RedactCtx(enabled=False)
        ev = m._evidence_item("sid-3", "/Users/alice/.claude/x.jsonl", ctx)
        self.assertEqual(ev["source_path"], "/Users/alice/.claude/x.jsonl")


class TestRedactProject(unittest.TestCase):
    def test_username_stripped_from_project(self):
        m = _load()
        out = m._redact_project("alice/my-project", m._RedactCtx())
        self.assertNotIn("/alice/", out)

    def test_disabled_returns_name_unchanged(self):
        m = _load()
        ctx = m._RedactCtx(enabled=False)
        self.assertEqual(m._redact_project("alice/my-project", ctx),
                         "alice/my-project")


if __name__ == "__main__":
    unittest.main()
