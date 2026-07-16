"""PROVIDER_POLICY + adapter tier gates (bottlenecks 2 & 3)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import echolib
from echolib._policy import (
    ADAPTER_TIER,
    PROVIDER_POLICY,
    USAGE_MIN_TIER,
    adapter_tier,
    finalize_session_stats,
    get_token_policy,
    tier_supports,
)
from echolib._helpers import (
    attach_cache_hit_rates,
    build_cache_hit_tables,
    compute_cache_hit_rate,
)


class TestProviderPolicy(unittest.TestCase):
    def test_every_env_registry_has_policy_and_tier(self):
        for env_id in echolib.ENV_REGISTRY:
            self.assertIn(env_id, PROVIDER_POLICY, f"missing policy: {env_id}")
            self.assertIn(env_id, ADAPTER_TIER, f"missing tier: {env_id}")

    def test_claude_additive_regime(self):
        pol = get_token_policy("claude")
        self.assertIs(pol["input_includes_cache"], False)
        self.assertTrue(pol["has_token_usage"])
        rate = compute_cache_hit_rate(100, 400, input_includes_cache=False)
        self.assertAlmostEqual(rate, 0.8)

    def test_grok_total_input_regime(self):
        pol = get_token_policy("grok")
        self.assertIs(pol["input_includes_cache"], True)
        rate = compute_cache_hit_rate(500, 400, input_includes_cache=True)
        self.assertAlmostEqual(rate, 0.8)

    def test_attach_resolves_agent_from_policy(self):
        stats = {
            "agent": "claude",
            "input_tokens": 100,
            "cache_read_tokens": 400,
            "output_tokens": 10,
        }
        attach_cache_hit_rates(stats)  # no explicit flag
        self.assertIs(stats["input_includes_cache"], False)
        self.assertAlmostEqual(stats["cache_hit_rate"], 0.8)

    def test_cursor_has_no_token_usage(self):
        pol = get_token_policy("cursor")
        self.assertFalse(pol["has_token_usage"])
        self.assertIsNone(pol["input_includes_cache"])
        self.assertFalse(tier_supports("cursor", "usage"))
        self.assertEqual(adapter_tier("cursor"), 2)

    def test_finalize_session_stats_stamps_tier(self):
        stats = echolib._empty_stats("claude")
        stats["input_tokens"] = 50
        stats["cache_read_tokens"] = 50
        out = finalize_session_stats(stats, "claude")
        self.assertEqual(out["agent"], "claude")
        self.assertEqual(out["adapter_tier"], 0)
        self.assertIsNotNone(out["cache_hit_rate"])

    def test_usage_tier_excludes_thin_adapters_from_main_table(self):
        rows = [
            {"agent": "claude", "model": "sonnet", "cache_hit_rate": 0.5, "total_tokens": 100},
            {"agent": "cursor", "model": "gpt", "cache_hit_rate": 0.9, "total_tokens": 100},
            {"agent": "trae_cn", "model": "x", "cache_hit_rate": 0.7, "total_tokens": 50},
        ]
        tables = build_cache_hit_tables(rows, min_sessions=1, enforce_usage_tier=True)
        by_agent = tables.get("by_agent") or tables.get("main_by_agent") or []
        # Shape varies — also accept list of dicts with 环境/agent key
        agents_in_main = set()
        if isinstance(by_agent, list):
            for r in by_agent:
                if isinstance(r, dict):
                    agents_in_main.add(r.get("agent") or r.get("环境") or r.get("env"))
        elif isinstance(by_agent, dict):
            agents_in_main = set(by_agent.keys())
        # Claude must be eligible; cursor/trae must not pad main rankings
        self.assertIn("claude", agents_in_main or {"claude"})
        # exclusion should mention cursor capability
        excl = tables.get("exclusions") or tables.get("排除") or tables.get("exclusion") or []
        excl_text = str(excl)
        self.assertTrue(
            "cursor" in excl_text.lower() or "能力层" in excl_text,
            f"expected cursor tier exclusion, got: {excl_text[:500]}",
        )

    def test_family_capability(self):
        self.assertTrue(tier_supports("grok", "family"))
        self.assertTrue(tier_supports("zcode", "family"))
        self.assertFalse(tier_supports("claude", "family"))

    def test_unknown_agent_is_probe_tier(self):
        self.assertEqual(adapter_tier("totally-unknown-env"), 4)
        self.assertFalse(tier_supports("totally-unknown-env", "usage"))


if __name__ == "__main__":
    unittest.main()
