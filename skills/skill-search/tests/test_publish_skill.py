#!/usr/bin/env python3
"""Regression tests for the self-contained safe skill publisher."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("publish_skill", ROOT / "scripts" / "publish_skill.py")
if SPEC is None or SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("unable to load scripts/publish_skill.py")
PUBLISH = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PUBLISH
SPEC.loader.exec_module(PUBLISH)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], _cwd: Path, **_kwargs: object):
        self.calls.append(args)
        if args[:4] == ["git", "remote", "get-url", "origin"]:
            return PUBLISH.CommandResult(args, 2, "", "no origin")
        if args[:3] == ["gh", "api", "user"]:
            return PUBLISH.CommandResult(args, 0, "demo-user", "")
        if args[:3] == ["gh", "repo", "view"]:
            return PUBLISH.CommandResult(args, 1, "", "not found")
        return PUBLISH.CommandResult(args, 0, "ok", "")


class PublishSkillTest(unittest.TestCase):
    def test_origin_parser_supports_https_and_ssh(self) -> None:
        self.assertEqual(
            PUBLISH.parse_origin("https://github.com/acme/demo-skill.git"),
            ("acme", "demo-skill"),
        )
        self.assertEqual(
            PUBLISH.parse_origin("git@github.com:acme/demo-skill.git"),
            ("acme", "demo-skill"),
        )

    def test_generated_readme_passes_public_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream = "https://github.com/example/upstream"
            text = PUBLISH.generated_readme(
                {
                    "name": "demo-skill",
                    "description": "把重复工作流整理成可验证的技能包。",
                    "version": "1.0.0",
                    "owner": "acme",
                },
                "acme",
                "demo-skill",
                upstream,
            )
            (root / "README.md").write_text(text, encoding="utf-8")
            self.assertEqual(PUBLISH.check_readme(root, upstream), [])
            self.assertIn("validate_skill.py", text)

    def test_generated_readme_license_uses_manifest_owner(self) -> None:
        text = PUBLISH.generated_readme(
            {"name": "demo-skill", "description": "demo.", "version": "1.0.0", "owner": "acme"},
            "acme",
            "demo-skill",
            "",
        )
        self.assertIn("Copyright (c) acme", text)
        self.assertNotIn("X:", text)  # no hardcoded personal identity

    def test_default_branch_push_is_rejected(self) -> None:
        for branch in ("", "main", "master"):
            with self.assertRaises(PUBLISH.PublishError):
                PUBLISH.assert_feature_branch(branch, "main")
        PUBLISH.assert_feature_branch("codex/publish-demo-v1-0-0", "main")
        with self.assertRaises(PUBLISH.PublishError):
            PUBLISH.assert_feature_branch("feature/publish-demo", "main")

    def test_failed_or_pending_checks_block_merge(self) -> None:
        ok, blockers = PUBLISH.pr_is_mergeable(
            {
                "mergeable": "MERGEABLE",
                "reviewDecision": "",
                "statusCheckRollup": [{"name": "test", "status": "IN_PROGRESS", "conclusion": None}],
            }
        )
        self.assertFalse(ok)
        self.assertTrue(any("pending" in item for item in blockers))

    def test_requested_changes_block_merge(self) -> None:
        ok, blockers = PUBLISH.pr_is_mergeable(
            {
                "mergeable": "MERGEABLE",
                "reviewDecision": "",
                "reviews": [{"state": "CHANGES_REQUESTED"}],
                "statusCheckRollup": [],
            }
        )
        self.assertFalse(ok)
        self.assertTrue(any("requested changes" in item for item in blockers))

    def test_dry_run_is_read_only_and_reports_planned_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "SKILL.md").write_text(
                "---\nname: demo-skill\ndescription: |\n  把重复工作流整理成可验证的 agent skill。\n---\n",
                encoding="utf-8",
            )
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "name": "demo-skill",
                        "version": "1.0.0",
                        "owner": "acme",
                        "upstream": "https://github.com/example/upstream",
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                skill_dir=str(root),
                github_user=None,
                repo_name=None,
                branch=None,
                private=False,
                dry_run=True,
                prepare_only=False,
                verify_only=False,
                no_merge=False,
                no_sync_local=True,
            )
            result = PUBLISH.publish(args, FakeRunner())
            self.assertTrue(result["ok"])
            self.assertIn("LICENSE", result["would_change"])
            self.assertIn("README.md", result["would_change"])
            self.assertFalse((root / "LICENSE").exists())
            self.assertFalse((root / "README.md").exists())
            self.assertEqual(result["default_branch_push"], "forbidden")

    def test_prepare_package_writes_license_and_readme(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(
                json.dumps({"upstream": "https://github.com/example/upstream"}),
                encoding="utf-8",
            )
            result = PUBLISH.prepare_package(
                root,
                {
                    "name": "demo-skill",
                    "description": "把重复工作流整理成可验证的 agent skill。",
                    "version": "1.0.0",
                    "owner": "acme",
                },
                "acme",
                "demo-skill",
                write=True,
            )
            self.assertEqual(result["failures"], [])
            self.assertTrue((root / "LICENSE").is_file())
            self.assertIn("npx skills add", (root / "README.md").read_text(encoding="utf-8"))

    def test_local_sync_preserves_previous_copy_outside_skill_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            source = base / "source"
            target = home / ".agents" / "skills" / "demo-skill"
            source.mkdir(parents=True)
            target.mkdir(parents=True)
            (source / "SKILL.md").write_text("new\n", encoding="utf-8")
            (target / "SKILL.md").write_text("old\n", encoding="utf-8")
            with patch.object(PUBLISH.Path, "home", return_value=home):
                result = PUBLISH.sync_local(source, "demo-skill")
            self.assertEqual(result["status"], "updated")
            self.assertEqual((target / "SKILL.md").read_text(encoding="utf-8"), "new\n")
            backup = Path(result["backup"])
            self.assertIn("skill-backups", backup.parts)
            self.assertEqual((backup / "SKILL.md").read_text(encoding="utf-8"), "old\n")


if __name__ == "__main__":
    unittest.main()
