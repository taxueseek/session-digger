#!/usr/bin/env python3
"""跨会话信息整合成文（roundup）。

三种模式共用 取数→去重→分段→渲染 管线，数据全走 fts 文本层：
- match：同类群聊汇总（按群名/联系人名匹配，点名模式——噪音群不排除）
- keyword：跨群主题提取（按内容关键词，默认排除噪音群）
- mine：我的发言梳理——每条我方消息带对话上下文（前后各 2 条）

去重规则（吸收 hub 跨群链接去重精华）：归一同文（去空白/数字/标点）与
同 URL 多群出现只保留首条，附全部出现会话与条数。
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Optional

from analyze import STOPWORDS, _tokenize, segment_messages
from chat_quality import _norm_dup

URL_RE = re.compile(r"https?://\S+")
DAY_FMT = "%Y-%m-%d"


def _day(ts: Optional[int]) -> str:
    return datetime.fromtimestamp(ts).strftime(DAY_FMT) if ts else "未知日期"


def dedup_messages(msgs: list[dict]) -> tuple[list[dict], int, dict[str, dict]]:
    """跨群同文/同 URL 去重（URL 与归一文本双键，任一命中即合并）。

    返回 (保留, 去掉条数, {归一键: 出现信息})。
    """
    url_map: dict[str, dict] = {}
    text_map: dict[str, dict] = {}
    kept: list[dict] = []
    removed = 0

    def _keys(m: dict) -> list[str]:
        keys: list[str] = []
        text = (m.get("text") or "").strip()
        urls = URL_RE.findall(text)
        if urls:
            keys.append("url:" + urls[0])
        norm = _norm_dup(text)
        if len(norm) >= 12:
            keys.append("txt:" + norm)
        return keys

    for m in sorted(msgs, key=lambda x: x.get("ts") or 0):
        if not (m.get("text") or "").strip():
            continue
        keys = _keys(m)
        if not keys:
            kept.append(m)
            continue
        seen = None
        for k in keys:
            store = url_map if k.startswith("url:") else text_map
            if store.get(k):
                seen = store[k]
                break
        if seen:
            seen["chats"].add(m.get("chat_name") or m.get("chat_id") or "?")
            seen["count"] += 1
            seen["lastTs"] = max(seen["lastTs"], m.get("ts") or 0)
            removed += 1
            continue
        entry = {
            "first": m,
            "chats": {m.get("chat_name") or m.get("chat_id") or "?"},
            "count": 1,
            "lastTs": m.get("ts") or 0,
        }
        for k in keys:
            (url_map if k.startswith("url:") else text_map)[k] = entry
        kept.append(m)
    seen_info = {
        k: {"count": e["count"], "chats": sorted(e["chats"])}
        for k, e in {**url_map, **text_map}.items() if e["count"] > 1
    }
    return kept, removed, seen_info


def _seg_title(seg: dict) -> str:
    texts = " ".join((m.get("text") or "")[:40] for m in seg.get("messages", [])[:4])
    tokens = [t for t in _tokenize(texts) if len(t) >= 2 and t not in STOPWORDS]
    top = [t for t, _ in Counter(tokens).most_common(3)]
    return "/".join(top) if top else (seg.get("topicHint") or "")[:24]


def _fmt_msg(m: dict, with_chat: bool = True) -> str:
    t = datetime.fromtimestamp(m["ts"]).strftime("%H:%M") if m.get("ts") else "??:??"
    who = m.get("sender") or "?"
    chat = f"〔{m['chat_name']}〕" if with_chat and m.get("chat_name") else ""
    return f"- {t} {chat}{who}：{(m.get('text') or '').strip()[:160]}"


def render_group_article(
    title: str,
    msgs: list[dict],
    window: dict,
    excluded_noise: Optional[dict[str, dict]] = None,
) -> str:
    """match/keyword 模式：按天分段、段内主题聚类、跨群同文标注。"""
    kept, removed, seen_info = dedup_messages(msgs)
    lines = [f"# {title}", ""]
    chats = sorted({m.get("chat_name") or m.get("chat_id") for m in msgs if m.get("text")})
    lines.append(
        f"> 窗口：近 {window.get('days', '?')} 天｜来源 {len(chats)} 会话｜"
        f"消息 {len(msgs)} 条（跨群同文去重 {removed} 条）｜生成 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    lines.append("")
    if not kept:
        lines.append("（窗口内无匹配内容）")
        return "\n".join(lines)

    by_day: dict[str, list[dict]] = defaultdict(list)
    for m in kept:
        by_day[_day(m.get("ts"))].append(m)

    for day in sorted(by_day):
        day_msgs = by_day[day]
        segs = segment_messages(day_msgs)
        lines.append(f"## {day}")
        for seg in segs[:12]:
            seg_msgs = sorted(seg.get("messages", []), key=lambda x: x.get("ts") or 0)
            seg_chats = sorted({m.get("chat_name") or m.get("chat_id") for m in seg_msgs})
            lines.append(
                f"### {_seg_title(seg)}（{len(seg_msgs)} 条 · "
                f"{'、'.join(seg_chats[:4])}{'…' if len(seg_chats) > 4 else ''}）"
            )
            picks = sorted(seg_msgs, key=lambda m: -len((m.get("text") or "")))[:6]
            for m in sorted(picks, key=lambda x: x.get("ts") or 0):
                lines.append(_fmt_msg(m))
            lines.append("")

    multi = {k: v for k, v in seen_info.items() if v["count"] > 1}
    if multi:
        lines.append("## 附：跨群同发内容（N 群同发只记一条）")
        for _k, v in sorted(multi.items(), key=lambda kv: -kv[1]["count"])[:10]:
            lines.append(f"- {v['count']} 处出现：{'、'.join(v['chats'][:6])}")
        lines.append("")

    if excluded_noise:
        lines.append("## 附：已按噪音群跳过（未计入上文，可用 --include-noise 纳入）")
        for name, info in sorted(excluded_noise.items())[:15]:
            lines.append(f"- {name}（{info.get('label')}，{info.get('score')} 分：{'；'.join(info.get('reasons', [])[:2])}）")
        lines.append("")
    return "\n".join(lines)


def render_mine_article(msgs_by_chat: dict[str, list[dict]], self_hint: str, window: dict) -> str:
    """mine 模式：我的每条发言带前后上下文，按会话归纳。"""
    lines = [f"# 我的发言梳理（近 {window.get('days', '?')} 天）", ""]
    total = 0
    ordered = sorted(
        (ms for ms in msgs_by_chat.values() if ms),
        key=lambda ms: ms[0].get("ts") or 0,
    )
    for msgs in ordered:
        name = msgs[0].get("chat_name") or (msgs[0].get("chat_id") or "?")
        mine_idx = [
            i for i, m in enumerate(msgs)
            if self_hint and self_hint.lower() in str(m.get("sender") or "").lower()
        ]
        if not mine_idx:
            continue
        total += len(mine_idx)
        lines.append(f"## {name}（我 {len(mine_idx)} 条）")
        shown: set[int] = set()
        for i in mine_idx:
            if i in shown:
                continue
            lo, hi = max(0, i - 2), min(len(msgs), i + 3)
            lines.append(f"### {_day(msgs[i].get('ts'))}")
            for j in range(lo, hi):
                m = msgs[j]
                marker = "👉" if j == i else "　"
                lines.append(f"{marker} {_fmt_msg(m, with_chat=False)}")
                shown.add(j)
            lines.append("")
    if not total:
        lines.append("（窗口内未找到本人发言——检查 --self 是否与聊天中的显示名一致）")
    return "\n".join(lines)
