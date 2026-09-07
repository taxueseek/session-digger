#!/usr/bin/env python3
"""Tests for the local prior-art engines (local_meta / local_body / usage)."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("skill_seek_local", ROOT / "scripts" / "skill_seek_local.py")
if SPEC is None or SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("unable to load skill_seek_local.py")
SEEK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SEEK)


class LocalEngineTest(unittest.TestCase):
    def test_tokenize_and_jaccard(self) -> None:
        self.assertIn("pdf", SEEK.token_set("rotate a pdf skill"))
        self.assertGreater(SEEK.jaccard(SEEK.token_set("pdf rotate"), SEEK.token_set("pdf rotate merge")), 0.5)
        self.assertEqual(SEEK.jaccard(set(), set()), 0.0)

    def test_parse_skill_md_extracts_name_and_description(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "demo-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: demo-skill\ndescription: |\n  Rotate PDFs and merge forms.\n---\n\n# Demo\n",
                encoding="utf-8",
            )
            name, description, _ = SEEK.parse_skill_md(skill_dir)
        self.assertEqual(name, "demo-skill")
        self.assertEqual(description, "Rotate PDFs and merge forms.")

    def test_parse_skill_md_rejects_missing_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "bad"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                SEEK.parse_skill_md(skill_dir)

    def test_scan_inventory_finds_matching_installed_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            match = root / "pdf-rotate"
            match.mkdir()
            (match / "SKILL.md").write_text(
                "---\nname: pdf-rotate\ndescription: Rotate and reorder PDF pages.\n---\n",
                encoding="utf-8",
            )
            unrelated = root / "seo-audit"
            unrelated.mkdir()
            (unrelated / "SKILL.md").write_text(
                "---\nname: seo-audit\ndescription: Audit site structure and backlinks.\n---\n",
                encoding="utf-8",
            )
            result = SEEK.scan_inventory([root], "pdf rotate", max_hits=12)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["scanned"], 2)
        self.assertEqual(len(result["hits"]), 1)
        self.assertEqual(result["hits"][0]["name"], "pdf-rotate")

    def test_scan_inventory_exact_name_boost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "agent-relay"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: agent-relay\ndescription: Relay tasks between agents.\n---\n",
                encoding="utf-8",
            )
            result = SEEK.scan_inventory([root], "agent relay", proposed_name="agent-relay", max_hits=12)
        self.assertEqual(result["hits"][0]["exact_name"], True)
        self.assertEqual(result["hits"][0]["score"], 1.0)

    def test_local_seek_returns_missing_when_script_absent(self) -> None:
        from unittest.mock import patch

        with patch.object(SEEK, "resolve_local_seek", return_value=None):
            result = SEEK.channel_local_seek("pdf", [Path("/nonexistent")], max_hits=5, timeout=1.0)
        self.assertEqual(result["status"], "missing")

    def test_digger_returns_missing_when_scripts_absent(self) -> None:
        from unittest.mock import patch

        with patch.object(SEEK, "resolve_digger_script", return_value=None):
            result = SEEK.channel_digger("pdf", timeout=1.0, skills_dirs=[Path("/nonexistent")])
        self.assertEqual(result["status"], "missing")
        self.assertEqual(result["opportunity_hits"], [])
        self.assertEqual(result["gap_hits"], [])

    def test_digger_parses_opportunity_and_gap_output(self) -> None:
        import json
        from unittest.mock import patch

        opp_payload = json.dumps(
            {
                "sessions_analyzed": 120,
                "skills_scanned": 30,
                "proposals": [{"theme": "pdf form filling", "matching_skill": "pdf-forms", "coverage": "partial", "priority": "P1"}],
            }
        )
        gap_payload = json.dumps(
            {
                "sessions_analyzed": 120,
                "patterns_found": 5,
                "proposals": [{"problem": "no pdf merge", "matched_skill": "none", "suggested_skill_md_addition": "add merge", "note": "x"}],
            }
        )

        def fake_run_cmd(argv, *, timeout, cwd=None):
            script = str(argv[1])
            if "opportunity" in script:
                return 0, opp_payload, ""
            return 0, gap_payload, ""

        with patch.object(SEEK, "resolve_digger_script", side_effect=lambda name: Path("/fake/session-digger/scripts") / name), patch.object(
            SEEK, "run_cmd", side_effect=fake_run_cmd
        ):
            result = SEEK.channel_digger("pdf", timeout=1.0, skills_dirs=[Path("/nonexistent")])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["opportunity_hits"]), 1)
        self.assertEqual(len(result["gap_hits"]), 1)
        self.assertEqual(result["meta"]["opportunity"]["sessions_analyzed"], 120)
        self.assertNotIn("evidence_sessions", result["opportunity_hits"][0])

    def test_digger_strips_raw_evidence_sessions(self) -> None:
        hits = SEEK._filter_relevant(
            [
                {
                    "theme": "pdf form filling",
                    "matching_skill": "pdf-forms",
                    "coverage": "partial",
                    "priority": "P1",
                    "evidence_sessions": ["s1", "s2", "s3", "s4"],
                    "top_examples": ["a", "b", "c"],
                }
            ],
            "pdf form",
            text_keys=("theme", "matching_skill", "coverage", "priority"),
            max_items=6,
        )
        self.assertEqual(len(hits), 1)
        row = hits[0]
        self.assertNotIn("evidence_sessions", row)
        self.assertEqual(row["evidence_sessions_count"], 4)
        self.assertEqual(len(row["evidence_sessions_sample"]), 3)
        self.assertEqual(len(row["top_examples"]), 2)


if __name__ == "__main__":
    unittest.main()
