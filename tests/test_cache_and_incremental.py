"""Cache-hit semantics + incremental fingerprint contracts."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from echolib._helpers import (  # noqa: E402
    attach_cache_hit_rates,
    cache_rate_eligible,
    compute_cache_hit_rate,
    filter_cache_models,
)
from index_builder import _builder as builder  # noqa: E402


class TestCacheHitRateSemantics(unittest.TestCase):
    def test_non_cached_explicit(self):
        # Claude-style: input is uncached-only; moderate hit rate must stay 30%
        self.assertEqual(
            compute_cache_hit_rate(7000, 3000, input_includes_cache=False),
            0.3,
        )
        self.assertEqual(
            compute_cache_hit_rate(1000, 9000, input_includes_cache=False),
            0.9,
        )

    def test_total_input_explicit(self):
        # Grok/ZCode/DimCode: cache is a subset of input
        self.assertEqual(
            compute_cache_hit_rate(10000, 3000, input_includes_cache=True),
            0.3,
        )
        self.assertEqual(
            compute_cache_hit_rate(1700, 1300, input_includes_cache=True),
            round(1300 / 1700, 4),
        )

    def test_attach_propagates_flag(self):
        stats = {
            "input_tokens": 7000,
            "cache_read_tokens": 3000,
            "model_usage": {
                "m": {"input_tokens": 7000, "cache_read_tokens": 3000},
            },
        }
        attach_cache_hit_rates(stats, input_includes_cache=False)
        self.assertEqual(stats["cache_hit_rate"], 0.3)
        self.assertFalse(stats["input_includes_cache"])
        self.assertEqual(stats["model_usage"]["m"]["cache_hit_rate"], 0.3)

    def test_equal_cache_input_not_100pct_when_non_cached(self):
        # Regression: auto total-input would report 1.0 for 50% Claude hits
        self.assertEqual(
            compute_cache_hit_rate(5000, 5000, input_includes_cache=False),
            0.5,
        )


class TestSinglePassGate(unittest.TestCase):
    def test_skips_formats_needing_adapters(self):
        self.assertTrue(
            builder._should_skip_single_pass(
                "/home/u/.zcode/cli/agents/sess_1/agent_x/transcript.jsonl"
            )
        )
        self.assertTrue(
            builder._should_skip_single_pass(
                "/home/u/.grok/sessions/p/sid/chat_history.jsonl"
            )
        )
        self.assertTrue(
            builder._should_skip_single_pass(
                "/home/u/.codex/sessions/2026/rollout-2026-01-01T00-00-00-uuid.jsonl"
            )
        )
        self.assertTrue(
            builder._should_skip_single_pass(
                "/home/u/.kimi-code/sessions/p/session_x/agents/main/wire.jsonl"
            )
        )
        self.assertTrue(
            builder._should_skip_single_pass(
                "/home/u/.workbuddy/projects/slug/abc.jsonl"
            )
        )
        self.assertFalse(
            builder._should_skip_single_pass(
                "/home/u/.claude/projects/p/abc-uuid.jsonl"
            )
        )


class TestCacheRankingFilters(unittest.TestCase):
    def test_drop_zero_and_null_rates(self):
        self.assertFalse(cache_rate_eligible(None))
        self.assertFalse(cache_rate_eligible(0.0))
        self.assertTrue(cache_rate_eligible(0.01))

    def test_filter_all_zero_cache_models(self):
        raw = {
            "LongCat-2.0": {"sess": 38, "input": 5e7, "cr": 0, "rates": [0.0] * 38},
            "kimi-for-coding": {"sess": 88, "input": 1e6, "cr": 5e5, "rates": [0.8] * 88},
            "tiny": {"sess": 0, "input": 1, "cr": 1, "rates": [0.9]},
        }
        out = filter_cache_models(raw, min_sessions=1, require_cache=True)
        self.assertNotIn("LongCat-2.0", out)
        self.assertIn("kimi-for-coding", out)
        self.assertNotIn("tiny", out)


class TestDimcodeFingerprint(unittest.TestCase):
    def test_per_session_not_whole_db(self):
        builder._DIMCODE_FP_MAP = {
            "sess_a": (100.0, "hash_a"),
            "sess_b": (200.0, "hash_b"),
        }
        m1, h1 = builder._file_fingerprint("dimcode://sess_a")
        m2, h2 = builder._file_fingerprint("dimcode://sess_b")
        self.assertEqual((m1, h1), (100.0, "hash_a"))
        self.assertEqual((m2, h2), (200.0, "hash_b"))
        self.assertNotEqual(h1, h2)
        builder._DIMCODE_FP_MAP = None


if __name__ == "__main__":
    unittest.main()
