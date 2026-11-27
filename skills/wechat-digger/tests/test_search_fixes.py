#!/usr/bin/env python3
"""搜索正确性回归（v0.5.1 建立，v0.6.0 修订 R3 真源）。

三层根因与真源（全部实证）：
R1 召回：limit 全链透传 + total/has_more 显式截断（v0.5.1 修复）。
R2 会话归属：c4=session_id 真源 = fts 库自带 name2id（与 c4 同库同体系）。
R3 发送者归属：c5=sender_id 与 c4 **同属** fts name2id 域（v0.6.0 纠正；
   v0.5.1 曾以 2715≠2837 跨域比较误判 c5 不可靠）。c5→fts.name2id 恢复后
   归档层发送者全部可解析（300/300 与 message_0.db Msg 表真值交叉验证），
   无需 (local_id,sort_seq) 反查机制（已删除）。
锚点案例（历史消息，内容稳定）：「示例单聊A」→ 示例客服单聊单聊；
「示例群B」→ 示例项目群。任何回归=这些锚点失效。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

# 发布版：live 锚点测试默认跳过（锚点名已虚构化，依赖作者私有库）
_LIVE_ANCHORS = os.environ.get("WECHAT_DIGGER_LIVE_ANCHORS") == "1"
sys.path.insert(0, str(SCRIPTS))

from fts_engine import (  # noqa: E402
    _NameBook,
    _escape_like,
    fts_db_path,
    fts_group,
    fts_search,
)


def _fts_available() -> bool:
    return fts_db_path() is not None


class TestChatType(unittest.TestCase):
    def test_group(self):
        self.assertEqual(_NameBook.chat_type("123@chatroom"), "group")

    def test_official(self):
        self.assertEqual(_NameBook.chat_type("gh_abc123"), "official")

    def test_openim(self):
        self.assertEqual(_NameBook.chat_type("25984@openim"), "openim")

    def test_wework(self):
        self.assertEqual(_NameBook.chat_type("wxid_wi_abc"), "wework")

    def test_single(self):
        self.assertEqual(_NameBook.chat_type("wxid_abc"), "single")

    def test_unknown_empty(self):
        self.assertEqual(_NameBook.chat_type(""), "single")


class TestSearchMeta(unittest.TestCase):
    """with_meta 输出契约：截断必须显式可见（R1 回归门）。"""

    @unittest.skipUnless(_fts_available() and _LIVE_ANCHORS, "live anchors: needs fts db + WECHAT_DIGGER_LIVE_ANCHORS=1")
    def test_meta_fields_present(self):
        out = fts_search("面试", since="2026-01-01", limit=10, with_meta=True)
        self.assertIn("hits", out)
        self.assertIn("total", out)
        self.assertIn("has_more", out)
        self.assertIn("limit", out)
        self.assertEqual(out["limit"], 10)

    @unittest.skipUnless(_fts_available() and _LIVE_ANCHORS, "live anchors: needs fts db + WECHAT_DIGGER_LIVE_ANCHORS=1")
    def test_truncation_is_explicit(self):
        out = fts_search("简历", since="2026-01-01", limit=50, with_meta=True)
        # 「简历」2026 年真实命中远超 50；修复前硬编码 50 静默截断
        self.assertGreater(out["total"], 50, "total 应反映全量命中而非截断数")
        self.assertEqual(len(out["hits"]), 50)
        self.assertTrue(out["has_more"])

    @unittest.skipUnless(_fts_available() and _LIVE_ANCHORS, "live anchors: needs fts db + WECHAT_DIGGER_LIVE_ANCHORS=1")
    def test_no_more_when_limit_exceeds(self):
        out = fts_search("示例单聊A", limit=100, with_meta=True)
        self.assertFalse(out["has_more"])
        self.assertEqual(out["total"], len(out["hits"]))

    @unittest.skipUnless(_fts_available() and _LIVE_ANCHORS, "live anchors: needs fts db + WECHAT_DIGGER_LIVE_ANCHORS=1")
    def test_hits_have_type_and_sender_fields(self):
        out = fts_search("面试", since="2026-01-01", limit=5, with_meta=True)
        for h in out["hits"]:
            self.assertIn("chat_type", h)
            self.assertIn("sender_resolved", h)

    @unittest.skipUnless(_fts_available() and _LIVE_ANCHORS, "live anchors: needs fts db + WECHAT_DIGGER_LIVE_ANCHORS=1")
    def test_hits_sorted_desc(self):
        out = fts_search("简历", limit=30, with_meta=True)
        ts = [h["ts"] for h in out["hits"]]
        self.assertEqual(ts, sorted(ts, reverse=True), "hits 必须全局按时间倒序")


class TestChatFilter(unittest.TestCase):
    """chat 过滤正确性（v0.6.0 门）：total 与 hits 必须同口径、过滤先于截断。

    注意 chat 过滤是模糊匹配：「记账分享2」会同时命中「记账分享2」与
    「记账分享2.0」两个不同会话——断言按前缀子集语义写。
    """

    KW = "简历"
    CHAT = "记账分享2"  # 历史会话，命中数稳定（>0 但不在全局 top2，曾验证截断 bug）

    @unittest.skipUnless(_fts_available() and _LIVE_ANCHORS, "live anchors: needs fts db + WECHAT_DIGGER_LIVE_ANCHORS=1")
    def test_total_is_chat_scoped(self):
        all_out = fts_search(self.KW, limit=5, with_meta=True)
        chat_out = fts_search(self.KW, chat=self.CHAT, limit=200, with_meta=True)
        self.assertGreater(chat_out["total"], 0, "锚点会话应有命中")
        self.assertLess(chat_out["total"], all_out["total"], "total 必须按 chat 过滤（曾是全库值）")
        self.assertFalse(chat_out["has_more"], "limit=200 ≥ 群内命中数时不应报截断")
        for h in chat_out["hits"]:
            self.assertIn(self.CHAT, h["chat_name"], "hits 必须属于过滤会话（模糊匹配含同名子串群）")

    @unittest.skipUnless(_fts_available() and _LIVE_ANCHORS, "live anchors: needs fts db + WECHAT_DIGGER_LIVE_ANCHORS=1")
    def test_search_total_matches_group_view(self):
        chat_out = fts_search(self.KW, chat=self.CHAT, limit=500, with_meta=True)
        g = fts_group(self.KW, chat=self.CHAT, top=10)
        self.assertGreaterEqual(len(g["groups"]), 1)
        for grp in g["groups"]:
            self.assertIn(self.CHAT, grp["chat_name"])
        self.assertEqual(chat_out["total"], sum(x["count"] for x in g["groups"]), "search 与 group-by 对同一过滤计数必须一致")

    @unittest.skipUnless(_fts_available() and _LIVE_ANCHORS, "live anchors: needs fts db + WECHAT_DIGGER_LIVE_ANCHORS=1")
    def test_group_filter_before_truncation(self):
        # 全局 top1（永安597 群）不匹配该过滤：先 top-N 后过滤的旧实现会错报 0 组
        g = fts_group(self.KW, chat=self.CHAT, top=1)
        self.assertGreaterEqual(len(g["groups"]), 1, "过滤必须先于 top-N 截断")
        self.assertGreater(g["groups"][0]["count"], 0)


class TestNameSource(unittest.TestCase):
    """名字真源回归（R2/R3 门）：锚点案例必须解析正确。"""

    @unittest.skipUnless(_fts_available() and _LIVE_ANCHORS, "live anchors: needs fts db + WECHAT_DIGGER_LIVE_ANCHORS=1")
    def test_chat_resolved_from_fts_name2id(self):
        out = fts_search("示例单聊A", limit=5, with_meta=True)
        self.assertGreaterEqual(len(out["hits"]), 1)
        h = out["hits"][0]
        # 真源：fts.name2id 域内单聊锚点（示例客服单聊） → 示例客服单聊（单聊）
        self.assertEqual(h["chat_name"], "示例客服单聊")
        self.assertEqual(h["chat_type"], "single")
        self.assertFalse(h["chat_name"].startswith("session:"))

    @unittest.skipUnless(_fts_available() and _LIVE_ANCHORS, "live anchors: needs fts db + WECHAT_DIGGER_LIVE_ANCHORS=1")
    def test_sender_resolved_via_fts_name2id(self):
        out = fts_search("示例单聊A", limit=5, with_meta=True)
        h = out["hits"][0]
        self.assertTrue(h["sender_resolved"], f"sender 未解析: {h['sender']}")
        self.assertEqual(h["sender"], "示例客服单聊")

    @unittest.skipUnless(_fts_available() and _LIVE_ANCHORS, "live anchors: needs fts db + WECHAT_DIGGER_LIVE_ANCHORS=1")
    def test_group_anchor_case(self):
        out = fts_search("示例群B", limit=5, with_meta=True)
        self.assertGreaterEqual(len(out["hits"]), 1)
        h = out["hits"][0]
        # 真源：fts.name2id 域内群聊锚点=示例项目群
        self.assertEqual(h["chat_type"], "group")
        self.assertIn("示例项目群", h["chat_name"])
        self.assertTrue(h["sender_resolved"], "群消息发送者应经 c5→fts.name2id 解析")

    @unittest.skipUnless(_fts_available() and _LIVE_ANCHORS, "live anchors: needs fts db + WECHAT_DIGGER_LIVE_ANCHORS=1")
    def test_archived_sender_resolvable(self):
        # 归档层（2026-02 前）发送者经 c5→fts.name2id 可解析（v0.6.0 恢复的能力）；
        # 未命中的仍显式 u:<id>，不编造名字
        out = fts_search("面试", since="2024-01-01", until="2025-12-31", limit=20, with_meta=True)
        self.assertGreater(len(out["hits"]), 0)
        resolved = sum(1 for h in out["hits"] if h["sender_resolved"])
        self.assertGreater(resolved, 0, "归档层发送者应可解析（c5 同域映射）")
        for h in out["hits"]:
            if not h["sender_resolved"]:
                self.assertTrue(
                    h["sender"].startswith("u:") or h["sender"] == "",
                    f"未解析 sender 不得显示具体名字: {h['sender']}",
                )


class TestGroupBy(unittest.TestCase):
    """聚合视图（诊断主视图）：count 加总一致、类型与名字齐备。"""

    @unittest.skipUnless(_fts_available() and _LIVE_ANCHORS, "live anchors: needs fts db + WECHAT_DIGGER_LIVE_ANCHORS=1")
    def test_group_consistency(self):
        out = fts_group("面试", since="2026-01-01", top=10000)
        # 锚点为 2026-09-06 手工 SQL 基线（74）；只设下限防数据增长炸锚
        self.assertGreaterEqual(out["total_hits"], 74, "与 2026-09-06 手工 SQL 基线一致")
        covered = sum(g["count"] for g in out["groups"])
        self.assertEqual(covered, out["total_hits"])
        self.assertFalse(out["truncated"])
        for g in out["groups"]:
            self.assertIn("chat_name", g)
            self.assertIn("chat_type", g)
            self.assertGreater(g["count"], 0)

    @unittest.skipUnless(_fts_available() and _LIVE_ANCHORS, "live anchors: needs fts db + WECHAT_DIGGER_LIVE_ANCHORS=1")
    def test_group_truncation_flag(self):
        out = fts_group("面试", since="2026-01-01", top=3)
        self.assertEqual(len(out["groups"]), 3)
        self.assertTrue(out["truncated"])

    @unittest.skipUnless(_fts_available() and _LIVE_ANCHORS, "live anchors: needs fts db + WECHAT_DIGGER_LIVE_ANCHORS=1")
    def test_group_exposes_noise(self):
        # 子串误命中（如「洁面试用」）应作为独立会话可见，而非污染人工会话判断
        out = fts_group("面试", since="2026-01-01", top=10000)
        names = {g["chat_name"] for g in out["groups"]}
        self.assertIn("示例优惠群1️⃣", names)


class TestFtsHistory(unittest.TestCase):
    """分析层 fts 取数（v0.6.1）：vault 空窗段（2026-02 前）消息可达、升序、身份可解析。"""

    CHAT = "记账分享2"  # 2023-02→2025-12，全在 vault 空窗段内

    @unittest.skipUnless(_fts_available() and _LIVE_ANCHORS, "live anchors: needs fts db + WECHAT_DIGGER_LIVE_ANCHORS=1")
    def test_history_reaches_archived_layer(self):
        from fts_engine import fts_history

        msgs = fts_history(self.CHAT, since="2023-01-01", until="2025-12-31", limit=10000)
        self.assertGreater(len(msgs), 0, "归档段会话必须有消息")
        ts = [m["ts"] for m in msgs]
        self.assertEqual(ts, sorted(ts), "历史必须按时间升序")
        self.assertLess(max(ts), 1770249600, "必须覆盖 2026-02-05 vault 建库点之前（空窗段）")
        self.assertTrue(any(m["sender_resolved"] for m in msgs), "发送者应经 c5 解析")
        for m in msgs:
            self.assertIn(self.CHAT, m["chat_name"])
            self.assertEqual(m["msg_type"], "text")

    @unittest.skipUnless(_fts_available() and _LIVE_ANCHORS, "live anchors: needs fts db + WECHAT_DIGGER_LIVE_ANCHORS=1")
    def test_search_total_matches_ground_truth_sql(self):
        """对拍门（吸收 hub test_live_parity 精华）：search total == 直接 SQL 全表 COUNT。

        这类测试本可在 v0.5.1 前抓住 total 未过滤/采样截断两类事故。
        """
        import sqlite3

        from fts_engine import (
            _FTS_TABLES,
            _NameBook,
            _as_epoch,
            _fts_where,
            _resolve_sids,
            fts_db_path,
            fts_search,
        )

        kw, since = "简历", "2023-01-01"
        path = fts_db_path()
        where, params = _fts_where(kw, _as_epoch(since), None, None)
        truth = 0
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        for t in _FTS_TABLES:
            truth += con.execute(f"SELECT COUNT(*) FROM {t}_content WHERE {where}", params).fetchone()[0]
        con.close()
        out = fts_search(kw, since=since, limit=5, with_meta=True)
        self.assertEqual(out["total"], truth, "search total 必须与直接 SQL 全表计数一致")

        # 带 chat 过滤的对拍：total 必须等于该会话子集的真值
        book = _NameBook(path.parent.parent)
        sids = _resolve_sids(book, self.CHAT)
        where2, params2 = _fts_where(kw, _as_epoch(since), None, sids)
        truth2 = 0
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        for t in _FTS_TABLES:
            truth2 += con.execute(f"SELECT COUNT(*) FROM {t}_content WHERE {where2}", params2).fetchone()[0]
        con.close()
        out2 = fts_search(kw, chat=self.CHAT, since=since, limit=5, with_meta=True)
        self.assertEqual(out2["total"], truth2, "chat 过滤后的 total 必须与 SQL 真值一致")
        self.assertLess(out2["total"], out["total"])


class TestEscapeCompat(unittest.TestCase):
    def test_escape_like(self):
        self.assertEqual(_escape_like("a%b_c"), "a\\%b\\_c")


if __name__ == "__main__":
    unittest.main()
