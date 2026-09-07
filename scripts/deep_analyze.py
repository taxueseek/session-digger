#!/usr/bin/env python3
"""deep-analyze — pack index data for a model to analyze on its own.

Produces a bounded Markdown data pack: global aggregates + the strongest
sessions in scope (stats, summary, user-message samples, tool-error profile)
+ which sessions already carry saved analysis conclusions. The consuming
model (command: deep-analyze) analyzes the pack and saves conclusions back
via sd-recall.py save-summary — no JSONL parsing anywhere in the loop.

Usage:
  deep_analyze.py [--days N] [--agent NAME] [--keyword KW] [--top N]
                  [--sort quality|recent] [--min-messages N] [--budget BYTES]
                  [--out FILE]   # default stdout
"""
import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from index_builder._builder import build_index  # noqa: E402
from index_builder._reader import (  # noqa: E402
    evidence_from_index,
    global_aggregates,
    last_build_age_hours,
    recent_sessions,
    summary_cache_info,
)
from index_builder._schema import DB_PATH  # noqa: E402


def ensure_fresh_index(max_age_hours=6.0):
    """Rebuild incrementally when the index is stale. Returns a status line.

    max_age_hours=None skips the check entirely (caller opted out).
    max_age_hours=0 forces a rebuild; a never-built index always rebuilds.
    """
    if max_age_hours is None:
        return None
    age = last_build_age_hours()
    if age is not None and age < max_age_hours:
        return None
    reason = ("索引从未建立" if age is None
              else f"索引已 {age:.1f} 小时未更新")
    result = build_index(rebuild=False, agent_filter="cross")
    return (f"{reason}，已自动增量重建："
            f"新索引 {result['indexed']}，跳过 {result['skipped']}，"
            f"耗时 {result['elapsed']}s")


def _clip(text, n):
    if not text:
        return ""
    return str(text).replace("\n", " ")[:n]


def _error_profile(tool_errors_json, width=60):
    try:
        agg = json.loads(tool_errors_json or "{}")
    except (ValueError, TypeError):
        return ""
    if not agg:
        return ""
    top = sorted(agg.items(), key=lambda kv: -kv[1])[:4]
    return "、".join(f"{name}×{n}" for name, n in top)[:width]


def build_pack(days, agent, keyword, top, sort, min_messages, budget,
               max_age_hours=6.0):
    refresh_note = ensure_fresh_index(max_age_hours)
    if not DB_PATH.exists():
        return None, "索引不存在且增量重建失败。检查 index-builder.py build 的输出。"
    aggs = global_aggregates(days=days, agent=agent)
    aggs_all = global_aggregates(days=None, agent=agent)
    sessions = recent_sessions(days=days, agent=agent, keyword=keyword,
                               limit=top, min_messages=min_messages, sort=sort)
    if not sessions:
        return None, "范围内没有符合条件的会话（放宽 --days / --min-messages 或检查索引）。"

    cache = summary_cache_info([s["id"] for s in sessions])
    lines = []
    scope = f"近{days}天" if days else "全部"
    if agent:
        scope += f" · 环境 {agent}"
    if keyword:
        scope += f" · 关键词「{keyword}」"
    lines.append(f"# 分析数据包（{scope}，{len(sessions)} 个重点会话）")
    if refresh_note:
        lines.append(f"> {refresh_note}")

    if aggs_all:
        t = aggs_all["totals"]
        err_pct = (100 * t["tool_errors"] / t["tool_calls"]) if t["tool_calls"] else 0
        lines.append("\n## 全局概况")
        lines.append(f"- 全库：会话 {t['sessions']}，消息 {t['messages']}，工具调用 "
                     f"{t['tool_calls']}（工具错误率 {err_pct:.0f}%），总 token {t['total_tokens']:,}")
        if aggs and days:
            r = aggs["totals"]
            lines.append(f"- 本次时间窗（{scope}）：会话 {r['sessions']}，"
                         f"消息 {r['messages']}，工具调用 {r['tool_calls']}")
        envs = "；".join(f"{e['agent']} {e['sessions']} 会话" for e in aggs_all["envs"])
        lines.append(f"- 环境分布（全库）：{envs}")
        if aggs and aggs["daily"]:
            daily = "，".join(f"{d['day'][5:]}: {d['sessions']}" for d in aggs["daily"][:10])
            lines.append(f"- 按日会话数（新→旧）：{daily}")

    lines.append("\n## 重点会话（按信息量排序）")
    per_budget = max(600, (budget - 1500) // len(sessions))
    for i, s in enumerate(sessions, 1):
        block = []
        title = _clip(s["summary"] or s["first_prompt"], 120) or "(无摘要)"
        block.append(f"### {i}. {s['id']}")
        meta = [f"agent={s['agent']}", f"时间={(s['created'] or '?')[:16]}"]
        if s.get("model"):
            meta.append(f"model={s['model']}")
        if s.get("project_path") and "://" not in str(s["project_path"]):
            meta.append(f"项目={Path(str(s['project_path'])).name}")
        block.append(f"- {' | '.join(meta)}")
        block.append(f"- 主题：{title}")
        block.append(f"- 规模：用户消息 {s['user_messages']}，助手消息 "
                     f"{s['assistant_messages']}，工具调用 {s['tool_calls']}，"
                     f"工具错误 {s['errors']}")
        block.append(f"- 会话文件: {s['jsonl_path']}")
        ep = _error_profile(s.get("tool_errors_json"))
        if ep:
            block.append(f"- 工具错误画像：{ep}")
        cached = cache.get(s["id"])
        if cached:
            block.append(f"- 已有分析结论：{cached['count']} 条，最新 "
                         f"{cached['latest_at'][:16]}（意图 {cached['latest_intent']}）")
            block.append(f"  - 最新要点：{_clip(cached['latest_analysis'], 110)}")
            src_mtime = cached.get("latest_source_mtime")
            idx_mtime = s.get("jsonl_mtime")
            grew = (isinstance(src_mtime, (int, float)) and isinstance(idx_mtime, (int, float))
                    and idx_mtime > src_mtime)
            if grew:
                block.append("  - ⚠ 会话在此分析之后又有新内容，建议重新分析")
            block.append("  - → 先向用户展示以上结论并询问是否重新分析，按用户决定执行，"
                         "不要自行跳过")
        # user message samples from FTS (uncjk-restored); drop non-human lines
        ev = evidence_from_index(s["id"], limit_msgs=12)
        samples = [
            m for m in ev["user_messages"]
            if not m["text"].startswith((
                "<system-reminder>", "<notification>", "Image read from",
                "This session is being continued", "<task-notification>"))
        ][:6]
        if samples:
            block.append("- 用户消息样本（时间序，已滤系统注入）：")
            remain = per_budget - sum(len(b) for b in block) - 40
            per_msg = max(60, remain // max(1, len(samples)))
            for m in samples:
                ts = (m.get("timestamp") or "?")[:10]
                block.append(f"  - [{ts}] {_clip(m['text'], per_msg)}")
        chunk = "\n".join(block)
        if sum(len(l) for l in lines) + len(chunk) > budget:
            lines.append(f"\n（预算已达上限，其余 {len(sessions) - i + 1} 个会话省略）")
            break
        lines.append(chunk)

    lines.append("\n## 回存接口（分析完成后由模型调用）")
    lines.append("- 逐会话回存：`python3 $SD_ROOT/scripts/sd-recall.py save-summary <会话jsonl路径> "
                 "\"<分析结论文本>\" --query <意图标签> --agent <agent> --tier periodic`")
    lines.append("- tier 语义：permanent=认知规律 / periodic=阶段性结论(默认) / once=一次性上下文")
    return "\n".join(lines), None


def main():
    ap = argparse.ArgumentParser(description="pack index data for model analysis")
    ap.add_argument("--days", type=int, default=7, help="时间窗（天），0=全部")
    ap.add_argument("--agent", default=None, help="限定环境（zcode/dimcode/...）")
    ap.add_argument("--keyword", default=None, help="FTS 关键词筛选")
    ap.add_argument("--top", type=int, default=10, help="重点会话数")
    ap.add_argument("--sort", default="quality", choices=["quality", "recent"])
    ap.add_argument("--min-messages", type=int, default=4, help="最小消息数（滤琐碎）")
    ap.add_argument("--budget", type=int, default=12000, help="数据包字节预算")
    ap.add_argument("--max-age-hours", type=float, default=6.0,
                    help="索引最大可接受年龄（小时），超龄自动增量重建；-1 跳过检查")
    ap.add_argument("--out", default=None, help="写出文件（默认 stdout）")
    args = ap.parse_args()

    max_age = None if args.max_age_hours < 0 else args.max_age_hours
    pack, err = build_pack(args.days, args.agent, args.keyword, args.top,
                           args.sort, args.min_messages, args.budget,
                           max_age_hours=max_age)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)
    if args.out:
        Path(args.out).write_text(pack, encoding="utf-8")
        print(f"数据包已写入 {args.out}（{len(pack)} 字节）")
    else:
        print(pack)


if __name__ == "__main__":
    main()
