#!/usr/bin/env python3
"""One rule, one home: no re-derived contract copies in the scripts tree.

Why this gate exists
--------------------
The v0.9.30/v0.9.31 rounds each closed a batch of "one line fixes a class"
defects whose root cause was always the same: a rule implemented once and
re-derived at N call sites, where the copies drift. This round found the
drift had already happened twice while nobody was looking:

  * ``normalize_model_name``: reflect-report's copy cleaned raw
    ``{'modelId': …}`` dict reprs, UUID provider ids and agent names; the
    canonical in ``echolib._models`` knew the space variants and gpt
    aliases. The same model string displayed differently in the report and
    in the stats, depending on which copy saw it first.
  * ``_tool_key``: an acknowledged mirror ("Mirrors ``_tool_key`` in
    ``echolib._claude``") — identical today, free to diverge tomorrow.

Both are single-source now. This file pins the single-source property
itself, so the next re-derivation fails here instead of shipping.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from echolib._models import normalize_model_name  # noqa: E402


def read(*parts: str) -> str:
    return (SCRIPTS.joinpath(*parts)).read_text(encoding="utf-8")


class ModelNameSingleSourceTest(unittest.TestCase):
    def test_reflect_report_imports_the_canonical(self):
        src = read("reflect-report.py")
        self.assertNotIn(
            "def normalize_model_name", src,
            "reflect-report re-derived normalize_model_name — import it from "
            "echolib._models instead; the copies have drifted once already",
        )
        self.assertNotIn("_MODEL_ALIASES = {", src,
                         "a second alias table is the drift vector that "
                         "made report and stats disagree")
        self.assertIn("from echolib._models import normalize_model_name", src)

    def test_dict_repr_model_is_cleaned_everywhere(self):
        # The case only reflect's copy used to handle. It must behave the
        # same through the canonical, since that is now the only copy.
        self.assertEqual(normalize_model_name("{'modelId': 'glm-5.2'}"), "GLM-5.2")

    def test_space_variants_resolve_through_the_canonical(self):
        # The case only the canonical's alias table knew.
        self.assertEqual(normalize_model_name("deepseek v4 flash"), "deepseek-v4-flash")
        self.assertEqual(normalize_model_name("glm 5.2"), "GLM-5.2")

    def test_non_models_return_empty(self):
        # UUID provider ids and agent names that land in the model field.
        self.assertEqual(normalize_model_name("0f1e2d3c-4b5a-6789-cdef"), "")
        self.assertEqual(normalize_model_name("claude"), "")
        self.assertEqual(normalize_model_name(""), "")
        self.assertEqual(normalize_model_name(None), "")


class ToolKeySingleSourceTest(unittest.TestCase):
    def test_session_analysis_imports_the_canonical(self):
        src = read("index_builder", "_session_analysis.py")
        self.assertNotIn(
            "def _tool_key", src,
            "the mirror copy is back; import _tool_key from echolib._claude "
            "— identical today is not identical forever",
        )
        self.assertIn("from echolib._claude import _tool_key", src)


if __name__ == "__main__":
    unittest.main()
