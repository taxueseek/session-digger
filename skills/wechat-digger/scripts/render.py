#!/usr/bin/env python3
"""Render analysis JSON to human-readable digests (normal / roast)."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def _date_range(messages: list[dict]) -> str:
    ts = [m.get("ts") for m in messages if m.get("ts")]
    if not ts:
        return datetime.now().strftime("%Y-%m-%d")
    a = datetime.fromtimestamp(min(ts)).strftime("%Y-%m-%d")
    b = datetime.fromtimestamp(max(ts)).strftime("%Y-%m-%d")
    return a if a == b else f"{a} ~ {b}"


def render_overview(analysis: dict, chat_name: str = "会话", messages: Optional[list] = None) -> str:
    stats = analysis.get("stats") or {}
    topics = analysis.get("topics") or []
    top = topics[0]["title"] if topics else "无显著话题"
    return (
        f"共 {stats.get('totalMessages', 0)} 条消息，"
        f"{stats.get('activeUsers', 0)} 人参与。"
        f"主要话题：{top}。"
    )


def render_summary(
    analysis: dict,
    chat_name: str = "会话",
    messages: Optional[list] = None,
    version: str = "normal",
) -> str:
    messages = messages or []
    date_range = _date_range(messages)
    stats = analysis.get("stats") or {}
    topics = analysis.get("topics") or []
    profiles = analysis.get("profiles") or []
    sentiment = analysis.get("sentiment") or {}
    decisions = analysis.get("decisions") or []
    graph = analysis.get("graph") or {}

    title_suffix = "群聊精华" if version == "normal" else "群聊精华 · 毒舌版"
    lines = [f"{chat_name} {title_suffix} · {date_range}", ""]

    # stats
    lines.append(f"📊 消息统计: 共 {stats.get('totalMessages', 0)} 条消息，{stats.get('activeUsers', 0)} 人参与")
    if profiles:
        top10 = sorted(profiles, key=lambda p: -p.get("messageCount", 0))[:10]
        board = " / ".join(f"{p['nickname']}({p['messageCount']})" for p in top10)
        lines.append(f"发言排行: {board}")
    lines.append("")

    # opening
    lines.append(render_overview(analysis, chat_name, messages))
    if sentiment.get("overall"):
        o = sentiment["overall"]
        lines.append(
            f"情绪倾向：积极 {o.get('positive', 0):.0%} · 中性 {o.get('neutral', 0):.0%} · 消极 {o.get('negative', 0):.0%}"
        )
    lines.append("")

    # profiles
    if profiles:
        lines.append("👥 群友画像")
        for p in profiles[:12]:
            if version == "roast":
                lines.append(
                    f"• {p.get('nickname')}（{p.get('roleLabel')}）：发言 {p.get('messageCount')} 条——"
                    f"{p.get('digestSummary', '')}。金句库存：{('；'.join(p.get('quotes') or [])[:80] or '暂无')}"
                )
            else:
                lines.append(f"• {p.get('nickname')}（{p.get('roleLabel')}）：{p.get('digestSummary', '')}")
        lines.append("")

    # topics body
    if topics:
        lines.append("💬 话题分类")
        emoji = ["🛠", "📦", "📰", "💬", "😄", "📚"]
        for i, t in enumerate(topics[:6]):
            em = emoji[i % len(emoji)]
            lines.append(f"{em} {t.get('title')}（{t.get('messageCount')}条，占比{t.get('weight', 0):.0%}）")
            for km in (t.get("keyMessages") or [])[:2]:
                if km:
                    lines.append(f"  「{km[:100]}」")
        lines.append("")

    # decisions
    if decisions and version == "normal":
        lines.append("✅ 关键决策 / 结论")
        for d in decisions[:8]:
            who = d.get("sender") or "?"
            lines.append(f"• [{who}] {d.get('text', '')[:120]} （置信度:{d.get('confidence', 'low')}）")
        lines.append("")

    # relation core
    if graph.get("nodes") and version == "normal":
        core = [n for n in graph["nodes"] if n.get("role") == "核心人物"][:5]
        if core:
            lines.append("🕸 核心互动")
            lines.append("核心人物：" + "、".join(n["nickname"] for n in core))
            for e in (graph.get("edges") or [])[:5]:
                lines.append(f"• {e['source']} → {e['target']}（权重 {e['weight']}，{','.join(e.get('types') or [])}）")
            lines.append("")

    if version == "roast":
        lines.append("本简报由一个没有感情的 AI 自动生成，如有冒犯，概不负责")
    else:
        lines.append("本简报由 AI 自动生成（wechat-digger）")

    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--chat-name", default="会话")
    p.add_argument("--version", choices=["normal", "roast"], default="normal")
    p.add_argument("--messages", default=None, help="optional messages json for date range")
    p.add_argument("--output", default="-")
    args = p.parse_args()
    analysis = json.loads(Path(args.input).read_text(encoding="utf-8"))
    msgs = []
    if args.messages:
        raw = json.loads(Path(args.messages).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "data" in raw:
            raw = raw["data"]
        msgs = raw if isinstance(raw, list) else raw.get("messages", [])
    text = render_summary(analysis, chat_name=args.chat_name, messages=msgs, version=args.version)
    if args.output == "-":
        print(text)
    else:
        Path(args.output).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
