#!/usr/bin/env python3
"""topic-scan 的两条回归门（v0.9.31 性能轮）。

背景（实测 2026-09-26，6948 会话真库）：``get_all_sessions`` 裸调
``cross_tool_list_sessions`` 时，16 个适配器为 ~400 行列表逐文件重读转录
（workbuddy 一家解析 10 万行 JSONL），topic-scan 端到端 4.2s；包一层
``discovery_only`` 后 0.66s，主题归属/成本逐项一致。

两条门都有牙：

* 摘掉 ``discovery_only`` 包裹 → test_listing_enters_discovery_only 红；
* 摘掉 ``scan_and_classify`` 里的两级回填（索引行 first_prompt →
  用户消息首条）→ test_first_prompt_backfilled 红（渲染回退为空字符串）。
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load_topic_classify():
    spec = importlib.util.spec_from_file_location(
        "topic_classify_under_test", str(SCRIPTS / "topic_classify.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class ListingEntersDiscoveryOnly(unittest.TestCase):
    def test_listing_enters_discovery_only(self):
        tc = _load_topic_classify()
        observed = {}
        real = tc.echolib.cross_tool_list_sessions

        def spy(*args, **kwargs):
            observed["discovery_only_active"] = tc.echolib.discovery_only_active()
            return []

        tc.echolib.cross_tool_list_sessions = spy
        try:
            tc.get_all_sessions(limit=10)
        finally:
            tc.echolib.cross_tool_list_sessions = real
        self.assertTrue(observed["discovery_only_active"])


class FirstPromptBackfill(unittest.TestCase):
    """listing 退化后 first_prompt 的两级回填。

    一级：索引行 bundle 里 first_prompt 有真值 → 渲染与分类都用它；
    二级：bundle 也没有 → 分类已取回的用户消息首条补位。
    两级都空才是空——不允许再回到「为渲染回读整个转录」。
    """

    PATH = "zcode://sess_backfill"  # 虚拟路径，跳过 os.path.exists

    def _run(self, bundle_prompt, user_messages):
        tc = _load_topic_classify()
        bundle = {
            "created": "2026-09-26T10:00:00", "modified": "2026-09-26T11:00:00",
            "user_messages": 2, "assistant_messages": 2, "tool_calls": 0,
            "errors": 0, "total_tokens": 100, "input_tokens": 80,
            "output_tokens": 20, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "summary": "", "first_prompt": bundle_prompt,
            "model": "m", "branch": "", "tool_usage_json": "{}",
            "user_evidence_json": None, "jsonl_mtime": 0.0,
        }
        tc._BUNDLES.clear()
        tc._BUNDLES_ASKED.clear()
        tc._BUNDLES[self.PATH] = bundle

        meta = {"agent": "zcode", "agent_display": "zcode",
                "session_id": "sess_backfill", "created": "2026-09-26T10:00:00",
                "summary": "", "first_prompt": "", "msg_count": 4,
                "full_path": self.PATH}
        tc.get_all_sessions = lambda limit=500: [meta]
        tc.dispatch_tools = lambda path, limit=30: []
        tc.dispatch_messages = lambda path, role="user", limit=5: user_messages
        sessions, _stats = tc.scan_and_classify(limit=10, since_date="")
        # 未分类桶也是桶：按路径取回这个会话的渲染行
        for rows in sessions.values():
            for row in rows:
                if row["path"] == self.PATH:
                    return row["first_prompt"]
        return None

    def test_backfilled_from_index_bundle(self):
        got = self._run(bundle_prompt="索引里的首问", user_messages=[])
        self.assertEqual(got, "索引里的首问")

    def test_backfilled_from_user_messages_when_bundle_lacks_it(self):
        got = self._run(bundle_prompt="",
                        user_messages=[{"role": "USER", "timestamp": "",
                                        "text": "用户消息首条：这句话的长度一定超过二十个字符的采纳门槛"}])
        self.assertEqual(got, "用户消息首条：这句话的长度一定超过二十个字符的采纳门槛")


if __name__ == "__main__":
    unittest.main()


class StockCodeCJKBoundary(unittest.TestCase):
    """「茅台600519估值」必须命中：汉字与数字之间没有 \\b 词边界。

    旧正则 \\b[036]\\d{5}\\b 依赖 ASCII 词边界语义，而 Python 把汉字算 \\w，
    中文+代码这种最常见写法整个漏掉（投资分析主题被低估、重复分析提示
    永不触发）。环视版 (?<![0-9])\\d{6}(?![0-9]) 在 CJK 邻接下仍工作，
    且不把 a1603230x 的尾串当代码。标的提取还须过确认关键词门——
    时间「093015」、薪资「300000」这类六位数字不再天然是股票。
    """

    @classmethod
    def setUpClass(cls):
        cls.tc = _load_topic_classify()

    def test_topic_pattern_matches_cjk_adjacent_code(self):
        re_ = None  # noqa: F841 — 可读性：模式来自 TOPIC_RULES[0]
        pat = self.tc.TOPIC_RULES[0]["_code_re"]
        self.assertIsNotNone(pat)
        self.assertTrue(pat.search("茅台600519估值"))
        self.assertTrue(pat.search("五粮液000858财报"))
        self.assertTrue(pat.search("股票 300750"))
        self.assertFalse(pat.search("a1603230x"))
        self.assertFalse(pat.search("1234567"))

    def test_extraction_requires_confirmation_keywords(self):
        classify = self.tc.classify_session_with_messages
        # 有确认词：代码被收进标的
        hit = classify("/tmp/x", {"summary": "估值分析"}, "茅台600519估值 股票")
        self.assertIn("600519", hit["stock_codes"])
        # 无确认词：时间样六位数不再天然是标的
        miss = classify("/tmp/x", {"summary": "会议记录"}, "时间 093015 ok")
        self.assertEqual(miss["stock_codes"], [])
