#!/usr/bin/env python3
"""
Lab-style multi-section report (吸收 welink/ChatLab 报告精华).

不引入 GUI/MCP；输出可分享的 Markdown + 机器 JSON。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional


def _date_range(messages: list[dict]) -> str:
    ts = [m.get("ts") for m in messages if m.get("ts")]
    if not ts:
        return datetime.now().strftime("%Y-%m-%d")
    a = datetime.fromtimestamp(min(ts)).strftime("%Y-%m-%d")
    b = datetime.fromtimestamp(max(ts)).strftime("%Y-%m-%d")
    return a if a == b else f"{a} ~ {b}"


def render_lab_report(
    analysis: dict,
    chat_name: str = "会话",
    messages: Optional[list] = None,
    flavor: str = "lab",
) -> str:
    """
    flavor:
      lab  — 数据实验室总览（welink 精华）
      dyad — 关系/双人对谈（垂直 skill 精华）
    """
    messages = messages or []
    stats = analysis.get("stats") or {}
    ranking = analysis.get("ranking") or []
    keywords = analysis.get("keywords") or []
    activity = analysis.get("activity") or {}
    sentiment = analysis.get("sentiment") or {}
    topics = analysis.get("topics") or []
    profiles = analysis.get("profiles") or []
    decisions = analysis.get("decisions") or []
    reciprocity = analysis.get("reciprocity") or {}
    graph = analysis.get("graph") or {}

    title = f"{chat_name} · 数据实验室报告" if flavor == "lab" else f"{chat_name} · 关系互动报告"
    lines = [
        f"# {title}",
        f"时间范围：{_date_range(messages)}",
        f"生成：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 1. 概览",
        f"- 消息 **{stats.get('totalMessages', 0)}** 条",
        f"- 活跃成员 **{stats.get('activeUsers', 0)}** 人",
        f"- 语义分段 **{stats.get('segmentCount', 0)}** 段",
    ]
    if activity.get("peakHour") is not None:
        lines.append(f"- 活跃高峰：周{activity.get('peakWeekday')} · {activity.get('peakHour')} 时")
    if sentiment.get("overall"):
        o = sentiment["overall"]
        lines.append(
            f"- 情绪：积极 {o.get('positive', 0):.0%} / 中性 {o.get('neutral', 0):.0%} / 消极 {o.get('negative', 0):.0%}"
        )
    lines.append("")

    # ranking
    if ranking:
        lines.append("## 2. 发言排行")
        for r in ranking[:12]:
            pct = f"{r.get('share', 0):.0%}"
            lines.append(
                f"{r.get('rank')}. **{r.get('nickname')}** — {r.get('messageCount')} 条（{pct}），"
                f"均长 {r.get('avgLength')}"
            )
        lines.append("")

    # keywords
    if keywords:
        lines.append("## 3. 高频词（词云原料）")
        top = " · ".join(f"{k['term']}({k['count']})" for k in keywords[:20])
        lines.append(top)
        lines.append("")

    # activity
    if activity.get("byHour"):
        lines.append("## 4. 时段热力（按小时）")
        # compact bar using counts
        hours = activity["byHour"]
        mx = max((h["count"] for h in hours), default=1) or 1
        for h in hours:
            if h["count"] <= 0:
                continue
            bar = "█" * max(1, int(10 * h["count"] / mx))
            lines.append(f"- {h['hour']:02d}:00 {bar} {h['count']}")
        lines.append("")

    # topics
    if topics:
        lines.append("## 5. 话题")
        for i, t in enumerate(topics[:8], 1):
            lines.append(f"{i}. {t.get('title')}（{t.get('messageCount')} 条，{t.get('weight', 0):.0%}）")
        lines.append("")

    # dyad / reciprocity
    if flavor == "dyad" or reciprocity.get("pair"):
        lines.append("## 6. 双人互动")
        if reciprocity.get("pair"):
            lines.append(f"- 对偶：{' ↔ '.join(reciprocity['pair'])}")
            lines.append(f"- 发言比：{reciprocity.get('messageRatio')}（{reciprocity.get('balanceLabel')}）")
            m = reciprocity.get("metrics") or {}
            for key in ("a", "b"):
                side = m.get(key) or {}
                lag = side.get("avgReplyLagSec")
                lag_s = f"{lag:.0f}s" if isinstance(lag, (int, float)) else "—"
                lines.append(
                    f"- {side.get('nickname')}: {side.get('count')} 条，对方回复后平均回应间隔 {lag_s}"
                )
            lines.append(f"- 对话轮次约：{(m.get('turnCount') or 0)}")
        else:
            lines.append(f"- {reciprocity.get('note') or '无对偶数据'}")
        lines.append("")

    # profiles
    if profiles and flavor == "lab":
        lines.append("## 7. 成员画像")
        for p in profiles[:10]:
            lines.append(f"- **{p.get('nickname')}**（{p.get('roleLabel')}）：{p.get('digestSummary')}")
        lines.append("")

    # decisions
    if decisions:
        lines.append("## 8. 决策/结论片段")
        for d in decisions[:8]:
            lines.append(f"- [{d.get('sender')}] {d.get('text', '')[:120]} （{d.get('confidence')}）")
        lines.append("")

    # graph core
    if graph.get("nodes") and flavor == "lab":
        core = [n for n in graph["nodes"] if n.get("role") == "核心人物"][:5]
        if core:
            lines.append("## 9. 关系网络核心")
            lines.append("核心人物：" + "、".join(n["nickname"] for n in core))
            for e in (graph.get("edges") or [])[:6]:
                lines.append(f"- {e['source']} → {e['target']}（{e['weight']}，{','.join(e.get('types') or [])}）")
            lines.append("")

    lines.append("---")
    lines.append("本报告由 wechat-digger 本地分析生成（规则+统计，无外部上传）。")
    return "\n".join(lines)
