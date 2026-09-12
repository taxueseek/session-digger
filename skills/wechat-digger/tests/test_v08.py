#!/usr/bin/env python3
"""v0.8.0 回归：群质量分层（噪音识别）+ roundup 整合成文。"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from chat_quality import _norm_dup, classify_chats  # noqa: E402
from roundup import dedup_messages, render_group_article, render_mine_article  # noqa: E402

NOW = int(time.time())


def _msg(text, sender, ts, chat="群", cid="c1", ctype="group"):
    return {"id": f"{cid}:{ts}", "chat_id": cid, "chat_name": chat, "chat_type": ctype,
            "sender": sender, "nickname": sender, "ts": ts, "text": text, "msg_type": "text", "source": "fts"}


def _spam_chat(cid, name, n=20, start=NOW - 3600):
    """典型线报群：单发送者、模板开头、口令链接、广告 emoji。"""
    sender = "线报机器人"
    msgs = []
    for i in range(n):
        msgs.append(_msg(
            f"【京东】🔻伊可新好娃娃维生素滴剂神价，复制这条￥ABCD{i}￥到淘宝下单 https://s.jd.com/{i}",
            sender, start + i * 60, chat=name, cid=cid,
        ))
    return msgs


def _normal_chat(cid, name, n=20, start=NOW - 3600):
    """真实对话：多人、问答、无模板。"""
    msgs = []
    people = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛"]
    for i in range(n):
        sender = people[i % len(people)]
        text = f"这个方案第{i}步怎么走？" if i % 3 == 0 else f"第{i}步先跑通再说"
        msgs.append(_msg(text, sender, start + i * 90, chat=name, cid=cid))
    return msgs


class TestNormDup(unittest.TestCase):
    def test_digits_and_punct_ignored(self):
        # 优惠码数字不同不影响同判
        self.assertEqual(_norm_dup("神价 复制￥123￥"), _norm_dup("神价 复制￥456￥"))

    def test_short_text_not_normalized(self):
        self.assertEqual(len(_norm_dup("好的")), 2)


class TestClassifyChats(unittest.TestCase):
    def test_spam_chat_is_noise(self):
        out = classify_chats({"c1": _spam_chat("c1", "京东线报群1")})
        info = out["c1"]
        self.assertEqual(info["level"], "noise", f"reasons={info['reasons']}")
        self.assertEqual(info["label"], "线报/广告群")
        self.assertTrue(any("群名特征词" in r for r in info["reasons"]))
        self.assertTrue(any("模板化刷屏" in r or "口令/链接" in r for r in info["reasons"]))

    def test_normal_chat_stays_normal(self):
        out = classify_chats({"c2": _normal_chat("c2", "老友技术闲聊")})
        info = out["c2"]
        self.assertEqual(info["level"], "normal", f"score={info['score']} reasons={info['reasons']}")

    def test_cross_group_duplication_detected(self):
        same = "🔥美团外卖大额券失效快，速撸 https://meituan.com/x"
        msgs = {}
        for cid, name in (("a", "优惠群A"), ("b", "优惠群B"), ("c", "福利群C")):
            msgs[cid] = [ _msg(f"{same} {i}", "发单员", NOW - 600 + i, chat=name, cid=cid) for i in range(12) ]
        out = classify_chats(msgs)
        for cid in msgs:
            self.assertTrue(
                any("跨群同发" in r for r in out[cid]["reasons"]),
                f"{cid} 应命中跨群同发: {out[cid]['reasons']}",
            )

    def test_few_messages_skipped(self):
        out = classify_chats({"c3": _spam_chat("c3", "线报群", n=5)})
        self.assertEqual(out["c3"]["label"], "样本不足")
        self.assertEqual(out["c3"]["level"], "normal")

    def test_qa_counters_broadcast(self):
        # 名字像但内容是真问答：不应进噪音（避免单点误报）
        msgs = _normal_chat("c4", "信用卡交流群")
        out = classify_chats({"c4": msgs})
        self.assertNotEqual(out["c4"]["level"], "noise", f"reasons={out['c4']['reasons']}")


class TestDedupMessages(unittest.TestCase):
    def test_same_text_across_chats_deduped(self):
        text = "🔥大额券速撸失效快 https://x.com/abc"
        msgs = [
            _msg(text, "发单员", 1000, chat="A群", cid="a"),
            _msg(text, "发单员", 1100, chat="B群", cid="b"),
            _msg(text, "发单员", 1200, chat="C群", cid="c"),
            _msg("正常聊天内容不重复", "甲", 1300, chat="A群", cid="a"),
        ]
        kept, removed, seen = dedup_messages(msgs)
        self.assertEqual(removed, 2)
        self.assertEqual(len(kept), 2)
        self.assertEqual(list(seen.values())[0]["count"], 3)
        self.assertEqual(seen[list(seen)[0]]["chats"], ["A群", "B群", "C群"])

    def test_digit_differing_spam_deduped(self):
        # 只有优惠码数字不同的同模板消息也要合并
        msgs = [
            _msg(f"神价速抢 https://x.com/p{i}", "机器人", 1000 + i, chat="线报", cid="a")
            for i in range(5)
        ]
        kept, removed, _ = dedup_messages(msgs)
        self.assertEqual(len(kept), 1)
        self.assertEqual(removed, 4)


class TestRenderRoundup(unittest.TestCase):
    def test_group_article_structure(self):
        msgs = _spam_chat("c1", "线报群A", n=6) + _normal_chat("c2", "技术群", n=8)
        text = render_group_article(
            "测试汇总", msgs, {"days": 7},
            excluded_noise={"线报群A": {"label": "线报/广告群", "score": 85, "reasons": ["模板化刷屏"]}},
        )
        self.assertIn("# 测试汇总", text)
        self.assertIn("## 附：已按噪音群跳过", text)
        self.assertIn("线报/广告群", text)
        self.assertIn("生成", text)

    def test_empty_window(self):
        text = render_group_article("空", [], {"days": 7})
        self.assertIn("窗口内无匹配内容", text)

    def test_mine_article_marks_self_with_context(self):
        msgs = [
            _msg("上次说的资料什么时候给我？", "客户", NOW - 500, chat="客户群", cid="c1"),
            _msg("我明天给你整理好", "我本人", NOW - 400, chat="客户群", cid="c1"),
            _msg("好的等你", "客户", NOW - 300, chat="客户群", cid="c1"),
        ]
        text = render_mine_article({"c1": msgs}, "我本人", {"days": 90})
        self.assertIn("👉", text)
        self.assertIn("我明天给你整理好", text)
        self.assertIn("客户群", text)

    def test_mine_article_empty_hint(self):
        text = render_mine_article({"c1": _normal_chat("c1", "群")}, "不存在的人", {"days": 90})
        self.assertIn("--self", text)


if __name__ == "__main__":
    unittest.main()
