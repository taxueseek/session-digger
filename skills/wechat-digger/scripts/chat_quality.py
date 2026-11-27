#!/usr/bin/env python3
"""群聊质量分层：识别线报/羊毛/广告类低增量信息群（followups 排除与 roundup 素材筛选共用）。

多信号加权评分，单点特征不定性（避免误报）：
- 正向（指向噪音）：群名特征词、发言集中度（广播群）、模板复重、口令/链接密度、
  广告 emoji 密度、跨群同文（同一内容短窗内多群出现=群发嫌疑）
- 反向（指向真实对话）：问答密度、活跃人数多且无支配者

level: noise(线报/广告群, ≥60) / low(低信息, 40-59) / normal(<40)。
分类是现算证据 + reasons 可解释；确认排除走 followups --feedback chat:<id> ignore。
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Optional

NAME_NOISE_WORDS = (
    "线报", "羊毛", "薅", "优惠", "福利", "神价", "神车", "bug价", "捡漏", "漏价",
    "内部价", "白菜价", "话费", "充值", "信用卡", "招商", "兼职", "互赞", "互助",
    "红包", "返利", "砍价", "拼团", "优惠券", "淘宝", "京东", "拼多多", "天猫",
    "线报群", "车", "🈲", "💰",
)
# 「车」单独太易误报（拼车/开车），仅在与其他词共存时计分
WEAK_SINGLE_WORDS = {"车"}

LINK_PATTERNS = (
    re.compile(r"https?://"),
    re.compile(r"复制[这這]条"),
    re.compile(r"[¥￥]\s*\w+"),  # 淘口令
    re.compile(r"(京东|淘宝|天猫|拼多多).{0,12}(下单|购买|链接|优惠)"),
)
AD_EMOJI = set("🔻‼️🧧💰🉑🈲🔥🉐✅👉📢🎁")
QUESTION_MARKS = ("？", "?")
DUP_TEXT_LEN = 20
DUP_MIN_CHATS = 3

# Link density at or above which a chat counts as "near-pure forwarding".
# At this level the question-mark signal inverts and the link signal becomes
# conclusive rather than merely indicative.
LINK_DOMINANT = 0.8

# Active-sender ceiling for the "broadcast + near-pure forwarding" rule.
# Measured: 线报 groups broadcast to many readers but are driven by very few
# senders (实测 2-6), while genuine communities show double digits.
BROADCAST_MAX_USERS = 6

# Levels excluded when scanning for business opportunities (followups).
# Stricter than roundup: `low`/低信息群 measured as pure 线报/羊毛 groups whose
# only "deal" signals are e-commerce forwards. roundup deliberately keeps
# `low` because it needs material breadth for article composition.
FOLLOWUP_EXCLUDED_LEVELS = ("noise", "low")


def _norm_dup(text: str) -> str:
    """跨群同文归一：去空白/数字/标点后取前缀（优惠码数字不同不影响同判）。"""
    stripped = re.sub(r"[\s\d，。！!？?、：:；;（）()【】\[\]#*~—\-·…\"'“”]+", "", text)
    return stripped[:DUP_TEXT_LEN]


def classify_chats(
    msgs_by_chat: dict[str, list[dict]],
    min_messages: int = 10,
) -> dict[str, dict[str, Any]]:
    """窗口内各会话质量分层。

    msgs_by_chat: chat_id → 升序消息列表（fts_recent 输出形态）。
    返回 {chat_id: {level, score, label, reasons, stats, name, chatType}}。
    """
    # 跨群同文：归一文本 → 出现会话集合
    dup_chats: dict[str, set[str]] = defaultdict(set)
    for cid, msgs in msgs_by_chat.items():
        for m in msgs:
            key = _norm_dup(m.get("text") or "")
            if len(key) >= 12:
                dup_chats[key].add(cid)

    out: dict[str, dict[str, Any]] = {}
    for cid, msgs in msgs_by_chat.items():
        name = (msgs[0].get("chat_name") or cid) if msgs else cid
        chat_type = (msgs[0].get("chat_type") or "unknown") if msgs else "unknown"
        texts = [(m.get("text") or "").strip() for m in msgs]
        texts_nonempty = [t for t in texts if t]
        n = len(texts_nonempty)
        stats = {
            "messages": len(msgs),
            "activeUsers": len({(m.get("sender") or "") for m in msgs}),
            "span": [msgs[0].get("ts"), msgs[-1].get("ts")] if msgs else [],
        }
        reasons: list[str] = []
        score = 0
        if n < min_messages:
            out[cid] = {
                "level": "normal", "score": 0, "label": "样本不足",
                "reasons": [f"窗口内仅 {n} 条文本，不参与判定"],
                "stats": stats, "name": name, "chatType": chat_type,
            }
            continue

        # 1) 群名特征（组合判定：「车」单独不计）
        low = name.lower()
        hits = [w for w in NAME_NOISE_WORDS if w != "车" and w in low]
        if len(hits) >= 1 and any(w in low for w in WEAK_SINGLE_WORDS):
            hits.append("车")
        if hits:
            score += min(40, 25 + 5 * (len(hits) - 1))
            reasons.append(f"群名特征词：{'、'.join(sorted(set(hits))[:5])}")

        # 2) 发言集中度（广播群）
        sender_counts = Counter(m.get("sender") or "?" for m in msgs)
        top_share = sender_counts.most_common(1)[0][1] / max(len(msgs), 1)
        if top_share >= 0.7:
            score += 20
            reasons.append(f"发言高度集中：第一名占 {top_share:.0%}（广播群特征）")
        elif top_share >= 0.5:
            score += 10
            reasons.append(f"发言偏集中：第一名占 {top_share:.0%}")

        # 3) 模板复重：同一发送者反复发相似开头
        by_sender: dict[str, Counter] = defaultdict(Counter)
        for m in msgs:
            t = (m.get("text") or "").strip()
            if len(t) >= 12:
                by_sender[m.get("sender") or "?"][t[:12]] += 1
        template_msgs = sum(c for cnt in by_sender.values() for c in cnt.values() if c >= 3)
        template_ratio = template_msgs / max(n, 1)
        if template_ratio >= 0.5:
            score += 20
            reasons.append(f"模板化刷屏：{template_ratio:.0%} 消息为同一人重复相似内容")

        # 4) 口令/链接密度（分级）
        #
        # Graded rather than binary. A single >=0.4 threshold scored a chat
        # with 41% links the same as one with 98%, which let a near-pure
        # 线报 group (实测 97.8% 链接) sit below the noise cut-off.
        link_hits = sum(1 for t in texts_nonempty if any(p.search(t) for p in LINK_PATTERNS))
        link_ratio = link_hits / max(n, 1)
        if link_ratio >= LINK_DOMINANT:
            score += 25
            reasons.append(f"口令/链接密度 {link_ratio:.0%}（近乎纯转发）")
        elif link_ratio >= 0.4:
            score += 15
            reasons.append(f"口令/链接密度 {link_ratio:.0%}")

        # 5) 广告 emoji 密度
        emoji_msgs = sum(1 for t in texts_nonempty if any(ch in AD_EMOJI for ch in t))
        emoji_ratio = emoji_msgs / max(n, 1)
        if emoji_ratio >= 0.3:
            score += 10
            reasons.append(f"广告 emoji 密度 {emoji_ratio:.0%}")

        # 6) 跨群同文（群发嫌疑）
        dup_msgs = sum(
            1 for m in msgs
            if len(dup_chats.get(_norm_dup(m.get("text") or ""), set())) >= DUP_MIN_CHATS
        )
        dup_ratio = dup_msgs / max(n, 1)
        if dup_ratio >= 0.5:
            score += 20
            reasons.append(f"跨群同发：{dup_ratio:.0%} 消息同时出现在 ≥{DUP_MIN_CHATS} 个会话")

        # 7) 反向信号：真实问答
        #
        # QUESTION_MARKS are only evidence of genuine conversation when the
        # chat is not link-dominated. In 线报/羊毛 groups the standard
        # call-and-response patter ("还有吗？" "怎么领？" "要的扣1？") produces
        # a question density as high as any real discussion, so applying this
        # penalty unconditionally suppressed the strongest noise signal.
        # Measured on a 11992-message 京东优惠群: 97.8% links, 100% top-share,
        # only 4 active senders, yet scored `normal` purely because of this.
        q_ratio = sum(1 for t in texts_nonempty if any(q in t for q in QUESTION_MARKS)) / max(n, 1)
        link_dominated = link_ratio >= LINK_DOMINANT
        if q_ratio >= 0.15:
            if link_dominated:
                reasons.append(
                    f"问答密度 {q_ratio:.0%} 但链接 {link_ratio:.0%}，不计真实对话"
                )
            else:
                score -= 15
                reasons.append(f"问答密度 {q_ratio:.0%}（真实对话特征）")
        # 广播群 + 近乎纯转发 = 定型线报群，实测需此强信号才够 noise 阈值
        if link_ratio >= LINK_DOMINANT and stats["activeUsers"] <= BROADCAST_MAX_USERS:
            score += 20
            reasons.append(
                f"广播群+高转发（{stats['activeUsers']} 人发 {link_ratio:.0%} 转发）"
            )
        if stats["activeUsers"] >= 8 and top_share < 0.4 and not link_dominated:
            score -= 10
            reasons.append(f"活跃 {stats['activeUsers']} 人且无支配者")

        score = max(0, min(100, score))
        level = "noise" if score >= 60 else ("low" if score >= 40 else "normal")
        label = {"noise": "线报/广告群", "low": "低信息群", "normal": "正常"}[level]
        out[cid] = {
            "level": level, "score": score, "label": label,
            "reasons": reasons or ["无显著特征"],
            "stats": stats, "name": name, "chatType": chat_type,
        }
    return out


def split_by_level(
    msgs_by_chat: dict[str, list[dict]],
    classified: Optional[dict[str, dict]] = None,
    confirmed_noise: Optional[set[str]] = None,
) -> tuple[dict[str, list[dict]], dict[str, list[dict]], dict[str, dict]]:
    """按质量拆分消息：保留正常/低信息，剔除噪音（含用户已确认的）。

    返回 (keep_msgs_by_chat, noise_msgs_by_chat, classification)。
    """
    classified = classified or classify_chats(msgs_by_chat)
    confirmed_noise = confirmed_noise or set()
    keep, noise = {}, {}
    for cid, msgs in msgs_by_chat.items():
        info = classified.get(cid) or {}
        if info.get("level") == "noise" or cid in confirmed_noise:
            noise[cid] = msgs
        else:
            keep[cid] = msgs
    return keep, noise, classified
