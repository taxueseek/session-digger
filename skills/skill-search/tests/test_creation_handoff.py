#!/usr/bin/env python3
"""Regression tests for persuasive but evidence-bound creation handoffs."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CreationHandoffTest(unittest.TestCase):
    def test_output_contract_requires_lineage_and_advantage_evidence(self) -> None:
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8").lower()
        for phrase in (
            "reference skills studied",
            "candidate-specific lessons",
            "design advantage",
            "validated advantage",
            "hypothesis",
        ):
            self.assertIn(phrase, text)

    def test_handoff_reference_contains_required_sections(self) -> None:
        text = (ROOT / "references" / "creation-handoff.md").read_text(encoding="utf-8")
        for heading in (
            "## 2. Reference skills studied",
            "## 3. Absorbed and rejected",
            "## 4. Advantages and highlights",
            "## 5. Verification and limits",
        ):
            self.assertIn(heading, text)


if __name__ == "__main__":
    unittest.main()
