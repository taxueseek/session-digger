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
    build_cache_hit_tables,
    cache_rate_eligible,
    classify_session_role,
    compute_cache_hit_rate,
    filter_cache_models,
    mean_cache_hit_rate,
)
from index_builder import _builder as builder  # noqa: E402
from index_builder import _session_analysis as analysis  # noqa: E402


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
    """The gate is fail-closed: adapter must be allow-listed AND path unvetoed.

    The single-pass reader returns zeros for a layout it cannot parse, so a
    missing deny rule silently empties a whole environment. Every case below is
    paired with the allow-listed agent name it must be evaluated against.
    """

    def _skips(self, path, agent):
        return analysis._should_skip_single_pass(path, agent)

    def test_skips_formats_needing_adapters(self):
        self.assertTrue(
            self._skips("/home/u/.zcode/cli/agents/sess_1/agent_x/transcript.jsonl",
                        "zcode")
        )
        self.assertTrue(
            self._skips("/home/u/.grok/sessions/p/sid/chat_history.jsonl", "grok")
        )
        self.assertTrue(
            self._skips(
                "/home/u/.codex/sessions/2026/rollout-2026-01-01T00-00-00-uuid.jsonl",
                "codex")
        )
        self.assertTrue(
            self._skips(
                "/home/u/.kimi-code/sessions/p/session_x/agents/main/wire.jsonl",
                "kimi_code")
        )
        self.assertTrue(
            self._skips("/home/u/.workbuddy/projects/slug/abc.jsonl", "workbuddy")
        )
        self.assertFalse(
            self._skips("/home/u/.claude/projects/p/abc-uuid.jsonl", "claude")
        )

    def test_unknown_agent_is_never_allow_listed(self):
        """A layout with no declared Claude-like format must not be read here."""
        for agent in ("dsh", "universal", "dim", "grok", "kimi", "kimix",
                      "codex", "kimi_code", "workbuddy", "dimcode", ""):
            self.assertTrue(
                self._skips("/home/u/.claude/projects/p/abc-uuid.jsonl", agent),
                f"{agent!r} must fall back to adapter dispatch",
            )

    def test_allow_list_is_declared_in_the_registry(self):
        from echolib._registry_data import single_pass_adapters
        self.assertIn("claude", single_pass_adapters())
        self.assertIn("zcode_v2", single_pass_adapters())
        self.assertNotIn("universal", single_pass_adapters())
        self.assertNotIn("dimcode", single_pass_adapters())

    def test_unrecognized_record_shape_fails_closed(self):
        """Parses to records, but none is a known type → adapter, not zeros."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "weird.jsonl"
            p.write_text('{"alpha": 1}\n{"beta": 2}\n', encoding="utf-8")
            self.assertIsNone(analysis._single_pass_analyze(str(p), "claude"))

    def test_single_pass_matches_adapter_on_claude_shaped_log(self):
        """Acceptance for wiring the pass in: same fields, one read not three.

        The adapter path for the same file is `_dispatch_session_stats` +
        `_compute_rich_stats` + `_dispatch_extract_messages` + an identity scan;
        the values below are what those produce for this log.
        """
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sess.jsonl"
            p.write_text(
                '{"type":"user","timestamp":"2026-09-16T10:00:00.000Z",'
                '"message":{"content":"第一轮问题"}}\n'
                '{"type":"assistant","timestamp":"2026-09-16T10:00:05.000Z",'
                '"message":{"model":"m-1","usage":{"input_tokens":100,'
                '"output_tokens":10,"cache_read_input_tokens":900},'
                '"content":[{"type":"text","text":"回答"},'
                '{"type":"tool_use","id":"t1","name":"Bash",'
                '"input":{"command":"ls -la"}}]}}\n'
                '{"type":"user","timestamp":"2026-09-16T10:00:10.000Z",'
                '"message":{"content":[{"type":"tool_result","tool_use_id":"t1",'
                '"is_error":true,"content":"boom"}]}}\n'
                '{"type":"user","timestamp":"2026-09-16T10:00:20.000Z",'
                '"message":{"content":"第二轮问题"}}\n',
                encoding="utf-8",
            )
            out = analysis._single_pass_analyze(str(p), "claude")
            self.assertIsNotNone(out, "claude-shaped log must take the fast path")
            stats, tools, msgs, identity = out
            self.assertEqual(stats["user_messages"], 2)
            self.assertEqual(stats["assistant_messages"], 1)
            self.assertEqual(stats["tool_calls"], 1)
            self.assertEqual(stats["errors"], 1)
            self.assertEqual(identity["model"], "m-1")
            self.assertEqual(identity["first_prompt"], "第一轮问题")
            self.assertEqual([m["role"] for m in msgs], ["USER", "ASSISTANT", "USER"])
            self.assertEqual(tools[0]["name"], "Bash")
            self.assertEqual(tools[0]["status"], "error")


class TestCacheRankingFilters(unittest.TestCase):
    def test_drop_zero_and_null_rates(self):
        self.assertFalse(cache_rate_eligible(None))
        self.assertFalse(cache_rate_eligible(0.0))
        self.assertTrue(cache_rate_eligible(0.01))

    def test_mean_is_simple_not_weighted(self):
        # one small 20% + one large would weight high; we take simple mean
        self.assertEqual(mean_cache_hit_rate([0.2, 0.8]), 0.5)
        self.assertIsNone(mean_cache_hit_rate([0.0, None]))

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

    def test_build_cache_hit_tables_excludes_zeros(self):
        rows = [
            {"agent": "kimi_code", "model": "LongCat-2.0", "cache_hit_rate": 0.0, "total_tokens": 1e7},
            {"agent": "kimi_code", "model": "kimi-for-coding", "cache_hit_rate": 0.8, "total_tokens": 1e5},
            {"agent": "kimi_code", "model": "kimi-for-coding", "cache_hit_rate": 0.7, "total_tokens": 1e5},
            {"agent": "kimi_code", "model": "kimi-for-coding", "cache_hit_rate": 0.9, "total_tokens": 1e5},
            {"agent": "workbuddy", "model": "", "cache_hit_rate": 0.5, "total_tokens": 1e6},
        ]
        t = build_cache_hit_tables(rows, min_sessions=3)
        self.assertEqual(t["global"]["n_eligible"], 4)  # 3 labeled + 1 unlabeled
        self.assertAlmostEqual(t["global"]["cache_hit_rate"], 0.725)
        agents = {r["agent"]: r for r in t["by_agent"]}
        self.assertEqual(agents["kimi_code"]["n_eligible"], 3)
        self.assertAlmostEqual(agents["kimi_code"]["cache_hit_rate"], 0.8)
        models = {(r["model"], r["agent"]) for r in t["by_model_env"]}
        self.assertIn(("kimi-for-coding", "kimi_code"), models)
        self.assertNotIn(("LongCat-2.0", "kimi_code"), models)
        reasons = {(e["model"], e["reason"]) for e in t["exclusions"]}
        self.assertIn(("LongCat-2.0", "命中率=0(无缓存信号)"), reasons)

    def test_classify_session_role(self):
        self.assertEqual(
            classify_session_role("dimcode", "dimcode:subagent_123", "dimcode://subagent_123"),
            "subagent",
        )
        self.assertEqual(
            classify_session_role("dimcode", "dimcode:sess_123", "dimcode://sess_123"),
            "main",
        )
        self.assertEqual(
            classify_session_role(
                "claude", "claude:x",
                "/home/u/.claude/projects/p/sid/subagents/agent-abc.jsonl",
            ),
            "subagent",
        )
        self.assertEqual(
            classify_session_role(
                "grok", "grok:child",
                "/x/chat_history.jsonl",
                summary={"session_kind": "subagent"},
            ),
            "subagent",
        )

    def test_split_role_tables(self):
        rows = [
            {"agent": "dimcode", "model": "m", "cache_hit_rate": 0.5,
             "total_tokens": 10, "id": "dimcode:sess_1", "session_role": "main"},
            {"agent": "dimcode", "model": "m", "cache_hit_rate": 0.5,
             "total_tokens": 10, "id": "dimcode:sess_2", "session_role": "main"},
            {"agent": "dimcode", "model": "m", "cache_hit_rate": 0.5,
             "total_tokens": 10, "id": "dimcode:sess_3", "session_role": "main"},
            {"agent": "dimcode", "model": "m", "cache_hit_rate": 0.9,
             "total_tokens": 10, "id": "dimcode:subagent_1", "session_role": "subagent"},
            {"agent": "dimcode", "model": "m", "cache_hit_rate": 0.9,
             "total_tokens": 10, "id": "dimcode:subagent_2", "session_role": "subagent"},
            {"agent": "dimcode", "model": "m", "cache_hit_rate": 0.9,
             "total_tokens": 10, "id": "dimcode:subagent_3", "session_role": "subagent"},
        ]
        t = build_cache_hit_tables(rows, min_sessions=3, split_role=True)
        # User-facing role labels only — never raw "main"/"subagent"
        roles = {(r["角色"], r["cache_hit_rate"]) for r in t["by_agent_role"]}
        self.assertIn(("主对话", 0.5), roles)
        self.assertIn(("子代理", 0.9), roles)
        for r in t["by_agent_role"]:
            self.assertNotIn(r["角色"], ("main", "subagent", "unknown"))
            self.assertNotIn("jsonl_path", r)
            self.assertNotIn("path", r)
        only_main = build_cache_hit_tables(rows, min_sessions=3, role_filter="主对话")
        self.assertEqual(only_main["global"]["n_eligible"], 3)
        self.assertEqual(only_main["global"]["cache_hit_rate"], 0.5)


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
