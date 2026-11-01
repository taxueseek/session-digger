"""Tests for cache_audit.py — cache health check and reporting contracts."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cache_audit as ca  # noqa: E402


class TestCacheAuditEngine(unittest.TestCase):
    def test_canonical_model(self):
        self.assertEqual(ca.canon_model("gemini-3.8-flash-preview"), "Gemini 3.8 Flash")
        self.assertEqual(ca.canon_model("Gemini 4 Argon (Medium)"), "Gemini 4 Argon")
        self.assertEqual(ca.canon_model("LongCat-2.5-Preview"), "LongCat-2.5-Preview")
        self.assertEqual(ca.canon_model("deepseek-v3"), "DeepSeek-V3")
        self.assertEqual(ca.canon_model("glm-5.3-flash"), "GLM-5.3-Flash")
        self.assertEqual(ca.canon_model("claude-3-5-sonnet-20241022"), "Claude 3.5 Sonnet")

    def test_audit_cache_environment_aggregation(self):
        # 3 sessions: two eligible (0.8 and 0.6), one zero cache (0.0)
        rows = [
            {
                "id": "s1",
                "agent": "antigravity",
                "model": "Gemini 3.8 Flash (Medium)",
                "cache_hit_rate": 0.8,
                "input_tokens": 1000,
                "cache_read_tokens": 4000,
                "output_tokens": 500,
                "total_tokens": 5500,
                "session_role": "main",
            },
            {
                "id": "s2",
                "agent": "antigravity",
                "model": "Gemini 3.8 Flash (Medium)",
                "cache_hit_rate": 0.6,
                "input_tokens": 2000,
                "cache_read_tokens": 3000,
                "output_tokens": 500,
                "total_tokens": 5500,
                "session_role": "subagent",
            },
            {
                "id": "s3",
                "agent": "antigravity",
                "model": "Gemini 3.8 Flash (Medium)",
                "cache_hit_rate": 0.0,
                "input_tokens": 1000,
                "cache_read_tokens": 0,
                "output_tokens": 100,
                "total_tokens": 1100,
                "session_role": "main",
            },
        ]

        res = ca.audit_cache(rows)
        table_a = res["table_a"]
        self.assertEqual(len(table_a), 1)
        ag_entry = table_a[0]
        self.assertEqual(ag_entry["agent"], "antigravity")
        self.assertEqual(ag_entry["total_sessions"], 3)
        self.assertEqual(ag_entry["eligible_sessions"], 2)
        self.assertEqual(ag_entry["zero_cache_sessions"], 1)

        # Rule check: mean cache hit rate MUST be arithmetic mean of eligible sessions (0.8 + 0.6) / 2 = 0.70
        self.assertAlmostEqual(ag_entry["mean_rate"], 0.70)

        # Token weighted rate: total cache (4000+3000=7000) / total input (1000+2000+1000=4000 + 7000) = 7000/11000 ~= 0.6363
        self.assertAlmostEqual(ag_entry["weighted_rate"], 7000 / 11000)

        # Role breakdown
        self.assertEqual(ag_entry["main_sessions"], 2)
        self.assertEqual(ag_entry["subagent_sessions"], 1)

    def test_markdown_report_formatting(self):
        rows = [
            {
                "id": f"s{i}",
                "agent": "antigravity",
                "model": "Gemini 3.8 Flash (Medium)",
                "cache_hit_rate": 0.85,
                "input_tokens": 1000,
                "cache_read_tokens": 4000,
                "output_tokens": 200,
                "total_tokens": 5200,
                "session_role": "main",
            }
            for i in range(5)
        ]
        audit = ca.audit_cache(rows)
        md = ca.format_markdown_report(audit)

        self.assertIn("缓存命中体检与诊断报告", md)
        self.assertIn("Antigravity (Google)", md)
        self.assertIn("85.00%", md)
        self.assertIn("Table A", md)
        self.assertIn("Table B", md)


if __name__ == "__main__":
    unittest.main()
