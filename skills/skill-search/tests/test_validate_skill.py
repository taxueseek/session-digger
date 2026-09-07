#!/usr/bin/env python3
"""Regression tests for the lightweight package validator."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("validate_skill", ROOT / "scripts" / "validate_skill.py")
if SPEC is None or SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("unable to load scripts/validate_skill.py")
VALIDATE_SKILL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATE_SKILL)


class DiscoverSkillEntrypointsTest(unittest.TestCase):
    def test_nested_exact_skill_md_is_discoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "SKILL.md").write_text("root\n", encoding="utf-8")
            nested = root / "examples" / "demo"
            nested.mkdir(parents=True)
            (nested / "SKILL.md").write_text("nested\n", encoding="utf-8")
            (nested / "SKILL.example.md").write_text("safe example\n", encoding="utf-8")

            entries = VALIDATE_SKILL.discover_skill_entrypoints(root)

            self.assertEqual(entries, [Path("SKILL.md"), Path("examples/demo/SKILL.md")])

    def test_noncanonical_example_and_fixture_names_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "SKILL.md").write_text("root\n", encoding="utf-8")
            nested = root / "examples" / "demo"
            nested.mkdir(parents=True)
            (nested / "SKILL.example.md").write_text("example\n", encoding="utf-8")
            (nested / "SKILL.fixture.md").write_text("fixture\n", encoding="utf-8")

            entries = VALIDATE_SKILL.discover_skill_entrypoints(root)

            self.assertEqual(entries, [Path("SKILL.md")])

    def test_evidence_report_version_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "reports").mkdir()
            (root / "reports" / "skill-ir.json").write_text(
                json.dumps({"package": {"name": "demo-skill", "version": "1.0.0"}}), encoding="utf-8"
            )
            (root / "reports" / "trigger-eval.json").write_text(
                json.dumps({"ok": True, "summary": {"total": 1, "passed": 1}}), encoding="utf-8"
            )
            (root / "reports" / "prior-art-research.md").write_text("research", encoding="utf-8")
            (root / "reports" / "creation-handoff.md").write_text("demo-skill 2.0.0", encoding="utf-8")
            failures: list[str] = []
            warnings: list[str] = []

            VALIDATE_SKILL.validate_evidence_reports(
                root,
                {"name": "demo-skill", "version": "2.0.0", "maturity_tier": "governed"},
                failures,
                warnings,
            )

            self.assertTrue(any("package.version" in item for item in failures))


if __name__ == "__main__":
    unittest.main()
