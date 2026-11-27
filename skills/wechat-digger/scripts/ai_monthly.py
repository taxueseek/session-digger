#!/usr/bin/env python3
"""月度 AI 使用画像：从聊天全史统计「每个月主力 AI 是什么」。

第一性原理：AI 使用的直接证据是聊天中的品牌词提及（讨论/求助/晒图），
不是自我报告。CLI 只产出结构化信号（月×工具分布、主力、活跃会话、
本人发言量），语义层的「独特分析/重复模式/教训」留给上层 LLM 解读——
CLI 硬编叙事只会变成编造。

WeChat 作为另类 agent：本人（--me 名字）的发言量与占比单独一列，
人类这半个 agent 的月度活跃度与 AI 提及曲线对照。
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fts_engine import _FTS_TABLES, _NameBook, _connect_ro, fts_db_path
from concurrent.futures import ThreadPoolExecutor

# 品牌词 → 规范名（中文别名/拼写变体合并到同一列）。一行扩一个工具。
AI_BRANDS: dict[str, str] = {
    "Claude": "Claude", "Anthropic": "Claude", "Claude Code": "Claude",
    "Grok": "Grok", "xAI": "Grok",
    "Kimi": "Kimi", "Moonshot": "Kimi", "K2": "Kimi",
    "Codex": "Codex", "OpenAI": "Codex", "ChatGPT": "Codex", "GPT-5": "Codex", "GPT-6": "Codex",
    "ZCode": "ZCode", "GLM": "ZCode", "智谱": "ZCode",
    "DeepSeek": "DeepSeek", "DSH": "DeepSeek", "R1": "DeepSeek",
    "豆包": "豆包", "Doubao": "豆包",
    "千问": "千问", "Qwen": "千问",
    "即梦": "即梦", "Jimeng": "即梦",
    "MiMo": "MiMo", "MiniMax": "MiMo",
    "Gemini": "Gemini",
    "Cursor": "Cursor",
}

_PATTERN = re.compile("|".join(re.escape(k) for k in sorted(AI_BRANDS, key=len, reverse=True)))


def _month_of(ts: Optional[int]) -> str:
    if not ts:
        return "-"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m")


def _noise_chat_keys() -> set[str]:
    """followups 的 chat ignore 状态（用户已确认的噪音群）→ 显示名集合。

    复用既有噪音治理：抢券/线报群的 AI 话术会污染月度统计
    （实测 5 月 DeepSeek 2896 条大头来自线报群刷屏）。"""
    try:
        from followups import load_state
        from paths import default_data_root
        st = load_state(default_data_root() / "followups.json")
        ignored = set()
        for entry in st.get("feedback", []):
            if isinstance(entry, dict):
                key = entry.get("target") or entry.get("key") or ""
                verdict = entry.get("verdict") or ""
                if key.startswith("chat:") and verdict in ("ignore", "low_priority"):
                    ignored.add(key.split(":", 1)[1])
            elif isinstance(entry, str) and entry.startswith("chat:"):
                ignored.add(entry.split(":", 1)[1])
        return ignored
    except Exception:
        return set()


def _scan_monthly(vault_dir: Path, months: Optional[list[str]], me_sid: Optional[int],
                  chat_filter_sids: Optional[set[int]], exclude_chats: Optional[set[str]] = None) -> dict[str, dict[str, Any]]:
    """一次全扫（OR 所有关键词）同时产出 月×规范名×sender 三维统计。

    比逐词扫描省 ~3x：单次 4 表 LIKE 联合扫描（~4s），命中行（并集 ~1.2 万）
    传输到 Python 端做正则精确归因——SQL 端 OR 不做归因，避免
    「Claude」命中「ClaudeCode」时重复计数的歧义。
    """
    con_sql = " OR ".join("c0 LIKE ? ESCAPE '\\'" for _ in AI_BRANDS)
    params = [f"%{k}%" for k in AI_BRANDS]
    book = _NameBook(vault_dir)
    fts_path = fts_db_path()

    def _scan(table: str) -> list[tuple]:
        con = _connect_ro(fts_path)
        try:
            sql = (f"SELECT c0, c4, c5, c6 FROM {table}_content WHERE {con_sql}")
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    with ThreadPoolExecutor(max_workers=len(_FTS_TABLES)) as pool:
        rows = [r for fut in [pool.submit(_scan, t) for t in _FTS_TABLES] for r in fut.result()]

    stats: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "total": 0, "mine": 0, "by_brand": Counter(), "chats": Counter(),
        "senders": set(), "senders_by_brand": defaultdict(set),
    })
    for c0, sid, c5, c6 in rows:
        text = c0 or ""
        month = _month_of(c6)
        if months and month not in months:
            continue
        if chat_filter_sids is not None and sid not in chat_filter_sids:
            continue
        matched = set(AI_BRANDS[m.group(0)] for m in _PATTERN.finditer(text))
        if not matched:
            continue  # OR 命中但正则归因空（不应发生，防御）
        st = stats[month]
        st["total"] += 1
        if me_sid is not None and c5 == me_sid:
            st["mine"] += 1
        for brand in matched:
            st["by_brand"][brand] += 1
        uname, display = book.chat(sid)
        chat_key = display or uname or f"session:{sid}"
        if exclude_chats and (uname in exclude_chats or chat_key in exclude_chats):
            continue
        st["chats"][chat_key] += 1
        st["senders"].add(c5)
        for brand in matched:
            st["senders_by_brand"][brand].add(c5)
    return stats


def build_report(vault_dir: Path, months: Optional[list[str]] = None,
                 me: str = "示例昵称", chat: Optional[str] = None,
                 top_chats: int = 3, prev_only_new: bool = True,
                 exclude_noise: bool = True) -> dict:
    """月度 AI 画像报告。prev_only_new=True 时计算「本月新兴」
    （本月出现且上月低于 20% 水平的品牌——重复模式的量化代理：
    稳定复用不亮灯，换主力的月份亮灯）。"""
    book = _NameBook(vault_dir)
    me_sid = None
    for sid, u in book._fts_user.items():
        if book.display(u) == me:
            me_sid = sid
            break
    chat_sids = None
    if chat:
        from fts_engine import _resolve_sids
        sids = _resolve_sids(book, chat)
        chat_sids = set(sids) if sids else set()

    stats = _scan_monthly(vault_dir, months, me_sid, chat_sids,
                          exclude_chats=_noise_chat_keys() if exclude_noise else None)
    out_months = []
    ordered = sorted(m for m in stats if m != "-")
    prev_dist: Counter = Counter()
    for month in ordered:
        st = stats[month]
        dist = dict(st["by_brand"].most_common())
        primary = dist and max(dist, key=dist.get)
        # 样本置信（知乎报告管线的失真治理）：小样本的主力是噪声不是结论
        confidence = "high" if st["total"] >= 50 else ("low" if st["total"] < 10 else "medium")
        # 新兴品牌：本月 >0 且上月 < 本月的 20%
        emerging = ([b for b, n in dist.items() if n > 2 and prev_dist.get(b, 0) < n * 0.2]
                    if prev_only_new and prev_dist else [])
        out_months.append({
            "month": month,
            "mention_msgs": st["total"],
            "mine_msgs": st["mine"],
            "participants": len(st["senders"]),
            "confidence": confidence,
            "primary": primary,
            "distribution": {b: {"mentions": n, "participants": len(st["senders_by_brand"].get(b, set()))}
                             for b, n in dist.items()},
            "emerging": sorted(emerging),
            "top_chats": [{"chat": c, "count": n}
                          for c, n in st["chats"].most_common(top_chats)],
        })
        prev_dist = st["by_brand"]
    return {"months": out_months, "note": "CLI 只给结构化信号（提及/主力/新兴/会话/人类侧）；"
                                            "独特分析与教训请基于 distribution+top_chats 由 LLM 解读，禁止 CLI 编造叙事"}


# ── HTML 可视化报告：纯静态 SVG，零 JS 零外部依赖（知乎报告管线同栈）──
# 隐私分界：报告含会话名，默认落私人目录（~/.local/share/wechat-digger/reports/），
# 项目/git 目录只放代码——对外分享前必须人工核对会话名脱敏。

_BRAND_COLORS = ["#c0392b", "#2b6cb0", "#b7791f", "#276749", "#6b46c1", "#818181"]


def _svg_stacked_bars(months: list[dict], width: int = 900, height: int = 260) -> str:
    """月度 AI 提及堆叠柱状图（top5 品牌 + 其他）。"""
    brands: list[str] = []
    for m in months:
        for b in m["distribution"]:
            if b not in brands:
                brands.append(b)
    totals = {b: sum(m["distribution"].get(b, {}).get("mentions", 0) for m in months) for b in brands}
    top = sorted(brands, key=lambda b: -totals[b])[:5]
    rest = [b for b in brands if b not in top]
    series = top + (["其他"] if rest else [])
    ymax = max((sum(m["distribution"].get(b, {}).get("mentions", 0) for b in series if b != "其他")
                + sum(m["distribution"].get(b, {}).get("mentions", 0) for b in rest) for m in months), default=1)
    ymax = max(ymax, 1)
    bw = (width - 60) / max(len(months), 1)
    parts = [f'<svg viewBox="0 0 {width} {height}" style="width:100%;max-width:{width}px">']
    colors = {b: _BRAND_COLORS[i % len(_BRAND_COLORS)] for i, b in enumerate(series)}
    for i, m in enumerate(months):
        x = 40 + i * bw
        y = height - 30
        for b in series:
            n = sum(m["distribution"].get(x2, {}).get("mentions", 0) for x2 in ([b] if b != "其他" else rest))
            if not n:
                continue
            h = n / ymax * (height - 50)
            y -= h
            parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw * 0.72:.1f}" height="{h:.1f}" '
                         f'fill="{colors[b]}" opacity="0.88"/>')
        parts.append(f'<text x="{x + bw * 0.36:.1f}" y="{height - 12}" font-size="10" text-anchor="middle" fill="#666">{m["month"][5:]}</text>')
    # 图例
    lx = 44
    for b in series:
        parts.append(f'<rect x="{lx}" y="4" width="10" height="10" fill="{colors[b]}"/>'
                     f'<text x="{lx + 14}" y="13" font-size="10" fill="#444">{b}</text>')
        lx += 14 + 12 * (len(b) + 4)
    parts.append(f'<line x1="40" y1="{height - 30}" x2="{width - 10}" y2="{height - 30}" stroke="#ccc"/>')
    parts.append("</svg>")
    return "".join(parts)


def _svg_lines(months: list[dict], width: int = 900, height: int = 200) -> str:
    """参与人数（讨论广度）与本人发言（人类侧）双线对照。"""
    xs = [40 + i * (width - 60) / max(len(months) - 1, 1) for i in range(len(months))]
    def _scale(vals):
        ymax = max(vals + [1])
        return [height - 30 - v / ymax * (height - 55) for v in vals]
    p1 = _scale([m["participants"] for m in months])
    p2 = _scale([m["mine_msgs"] for m in months])
    parts = [f'<svg viewBox="0 0 {width} {height}" style="width:100%;max-width:{width}px">']
    for pts, color, label in [(p1, "#2b6cb0", "参与人数"), (p2, "#c0392b", "本人发言(人类侧)")]:
        d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(zip(xs, pts)))
        parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2"/>')
        for x, y in zip(xs, pts):
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/>')
    for i, m in enumerate(months):
        parts.append(f'<text x="{xs[i]:.1f}" y="{height - 12}" font-size="10" text-anchor="middle" fill="#666">{m["month"][5:]}</text>')
    parts.append('<rect x="44" y="4" width="10" height="10" fill="#2b6cb0"/><text x="58" y="13" font-size="10" fill="#444">参与人数</text>'
                 '<rect x="140" y="4" width="10" height="10" fill="#c0392b"/><text x="154" y="13" font-size="10" fill="#444">本人发言(人类侧)</text>')
    parts.append(f'<line x1="40" y1="{height - 30}" x2="{width - 10}" y2="{height - 30}" stroke="#ccc"/>')
    parts.append("</svg>")
    return "".join(parts)


def render_html(report: dict, title: str = "月度 AI 使用画像") -> str:
    """报告 → 自包含 HTML（零 JS）。图表 + 每月明细表。"""
    months = report["months"]
    rows = []
    for m in months:
        dist = "、".join(f"{b} {v['mentions']}({v['participants']}人)" for b, v in list(m["distribution"].items())[:6])
        emg = f'<b style="color:#c0392b">{",".join(m["emerging"])}</b>' if m["emerging"] else "-"
        chats = "、".join(c["chat"] for c in m["top_chats"])
        rows.append(f'<tr><td>{m["month"]}</td><td><b>{m["primary"] or "-"}</b>'
                    f'<span style="color:#999">({m["confidence"]})</span></td>'
                    f'<td>{m["mention_msgs"]}</td><td>{m["mine_msgs"]}</td><td>{m["participants"]}</td>'
                    f'<td>{dist}</td><td>{emg}</td><td style="color:#666">{chats}</td></tr>')
    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>{title}</title><style>
body{{font-family:-apple-system,'PingFang SC',sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#222}}
h2{{border-left:4px solid #c0392b;padding-left:10px;margin-top:28px}}
table.tbl{{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}}
.tbl th,.tbl td{{border:1px solid #ddd;padding:4px 6px;text-align:left}}
.tbl th{{background:#f5f0e8}}
.note{{color:#777;font-size:12px;margin-top:18px}}
</style></head><body>
<h1>{title}</h1>
<h2>月度 AI 提及分布（堆叠）</h2>
{_svg_stacked_bars(months)}
<h2>讨论广度 vs 人类侧</h2>
{_svg_lines(months)}
<h2>每月明细</h2>
<table class="tbl"><tr><th>月份</th><th>主力(置信)</th><th>提及</th><th>本人</th><th>参与人数</th><th>分布 提及(人数)</th><th>新兴</th><th>top 会话</th></tr>
{''.join(rows)}</table>
<p class="note">CLI 只给结构化信号（提及/主力/新兴/会话/人类侧）；独特分析与教训请基于分布与会话由 LLM 解读。
报告含会话名，属私人产物——对外分享前必须人工核对脱敏。</p>
</body></html>"""
    return html


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="月度 AI 使用画像（聊天全史）")
    ap.add_argument("--year", default=None, help="只看某年，如 2026")
    ap.add_argument("--months", default=None, help="逗号分隔月列表，如 2026-01,2026-02（默认全部）")
    ap.add_argument("--me", default="示例昵称", help="本人显示名（人类 agent 侧）")
    ap.add_argument("--chat", default=None, help="限定会话（群名/备注模糊）")
    ap.add_argument("--top-chats", type=int, default=3)
    ap.add_argument("--include-noise", action="store_true",
                    help="不排除 followups 已确认的噪音群（默认排除）")
    ap.add_argument("--format", default="json", choices=["json", "text"])
    ap.add_argument("--html", nargs="?", const="", default=None,
                    help="生成自包含 HTML 可视化报告；缺省路径=私人目录 reports/")
    args = ap.parse_args(argv)

    months = None
    if args.months:
        months = set(m.strip() for m in args.months.split(","))
    elif args.year:
        months = {f"{args.year}-{m:02d}" for m in range(1, 13)}

    vault_dir = fts_db_path().parent.parent
    report = build_report(vault_dir, months=months, me=args.me, chat=args.chat,
                          top_chats=args.top_chats, exclude_noise=not args.include_noise)
    if args.html is not None:
        from paths import default_data_root
        out = Path(args.html) if args.html else (
            default_data_root() / "reports" / f"ai-monthly-{datetime.date.today().isoformat()}.html")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_html(report), encoding="utf-8")
        print(json.dumps({"html": str(out), "months": len(report["months"])}, ensure_ascii=False))
        return 0
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        for m in report["months"]:
            dist = " ".join(f"{b}:{n}" for b, n in m["distribution"].items())
            emg = f" 新兴:{','.join(m['emerging'])}" if m["emerging"] else ""
            chats = " ".join(c["chat"][:12] for c in m["top_chats"])
            print(f"{m['month']}  主力={m['primary']}({m['confidence']})  提及={m['mention_msgs']}(本人{m['mine_msgs']}, {m['participants']}人){emg}")
            print(f"    分布: {dist}")
            print(f"    会话: {chats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
