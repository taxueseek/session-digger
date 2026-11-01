"""Trae (Global) adapter contract and tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import echolib  # noqa: E402


class TestTraeGlobalRegistration(unittest.TestCase):
    def test_registered_in_env_and_adapter_registries(self):
        self.assertIn("trae", echolib.ENV_REGISTRY)
        self.assertIn("trae", echolib.ADAPTER_REGISTRY)
        self.assertEqual(echolib.ENV_REGISTRY["trae"]["adapter"], "trae")
        self.assertEqual(echolib.ENV_REGISTRY["trae"]["root"], "~/.trae/memory/projects/")

    def test_policy_and_tier(self):
        policy = echolib.get_token_policy("trae")
        self.assertFalse(policy["has_token_usage"])
        self.assertEqual(echolib.adapter_tier("trae"), echolib.TIER_THIN)


if __name__ == "__main__":
    unittest.main()
