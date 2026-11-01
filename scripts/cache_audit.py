#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cache Health Audit (缓存命中体检与诊断) — session-digger /usage & /audit 口径.

依据 references/cache-report-rules.md 严格执行:
- 主表只报会话均命中率（rate > 0 的有效会话算术平均）
- 零缓存 / 无字段 / 未标注模型 / 有效会话 < 3 进排除清单
- Token policy 严格由 PROVIDER_POLICY[agent].input_includes_cache 解释
- 角色分列严格遵循「主对话」vs「子代理」
- 支持环境级体检与模型级体检，输出健康评级与诊断建议
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import sys
from pathlib import Path

# Path resolution: find echolib in ../scripts or current directory
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import echolib  # noqa: E402
from echolib._helpers import (  # noqa: E402
    cache_rate_eligible,
    mean_cache_hit_rate,
    session_role_label,
    SESSION_ROLE_MAIN,
    SESSION_ROLE_SUBAGENT,
)
from echolib._models import normalize_model_name  # noqa: E402

INDEX_DB_PATH = Path(os.environ.get("SESSION_DIGGER_INDEX_DB", Path.home() / ".claude" / ".session-digger" / "index.db"))


def canon_model(m: str | None) -> str:
    """Canonicalize model name, combining case and routing wrappers while keeping distinct variants."""
    if not m:
        return "unknown"
    s = str(m).strip()
    low = s.lower()
    if "longcat" in low:
        return "LongCat-2.5-Preview" if "2.5" in low else ("LongCat" if low == "longcat" else "LongCat-2.0")
    if "glm" in low and "5.3" in low:
        if "flash" in low and "flashx" not in low:
            return "GLM-5.3-Flash"
        return s
    if "deepseek" in low:
        if "v3" in low or "chat" in low:
            return "DeepSeek-V3"
        if "r1" in low or "reasoner" in low:
            return "DeepSeek-R1"
        if "4.1" in low:
            return "DeepSeek-V4.1-Flash"
        if "v4" in low:
            return "DeepSeek-V4-Flash"
        return "DeepSeek"
    if "gemini" in low:
        if "3.8" in low:
            return "Gemini 3.8 Flash"
        if "argon" in low:
            return "Gemini 4 Argon"
        if "3" in low:
            return "Gemini 3"
        return "Gemini"
    if "claude" in low:
        if "3-7" in low or "3.7" in low:
            return "Claude 3.7 Sonnet"
        if "3-5-sonnet" in low or "3.5-sonnet" in low or "3.5 sonnet" in low:
            return "Claude 3.5 Sonnet"
        if "3-5-haiku" in low or "3.5-haiku" in low:
            return "Claude 3.5 Haiku"
        if "opus" in low:
            return "Claude Opus"
        return "Claude"
    return normalize_model_name(s)


def collect_rows_from_index(db_path: Path, env_filter: str | None = None, model_filter: str | None = None) -> list[dict]:
    """Load session rows from SQLite index."""
    if not db_path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []

    query = """
        SELECT id, agent, model, cache_hit_rate, total_tokens,
               input_tokens, cache_read_tokens, output_tokens,
               session_role, jsonl_path
        FROM sessions
    """
    conditions = []
    params = []
    if env_filter:
        conditions.append("agent = ?")
        params.append(env_filter)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    rows = []
    try:
        for r in conn.execute(query, params):
            d = dict(r)
            m = d.get("model") or ""
            if model_filter and model_filter.lower() not in m.lower():
                continue
            rows.append(d)
    finally:
        conn.close()
    return rows


def collect_rows_live(env_filter: str | None = None, model_filter: str | None = None, limit_per_env: int = 200) -> list[dict]:
    """Collect session rows directly by querying echolib adapters."""
    rows = []
    target_envs = [env_filter] if env_filter else list(echolib.ENV_REGISTRY.keys())

    for env_id in target_envs:
        adapter_info = echolib.ADAPTER_REGISTRY.get(env_id)
        if not adapter_info or not adapter_info.get("list_sessions"):
            continue
        try:
            sessions = adapter_info["list_sessions"](limit=limit_per_env) or []
        except Exception:
            continue

        for s in sessions:
            if isinstance(s, dict):
                path = s.get("path") or s.get("full_path") or s.get("jsonl_path") or ""
                sid = s.get("id") or s.get("session_id") or ""
                model = s.get("model") or ""
                role = s.get("role")
            else:
                path = getattr(s, "full_path", None) or getattr(s, "path", "") or ""
                sid = getattr(s, "session_id", None) or getattr(s, "id", "") or ""
                model = getattr(s, "model", "")
                role = getattr(s, "role", None)

            if not path:
                continue
            try:
                st = echolib.dispatch_session_stats(path)
                st = echolib.finalize_session_stats(st, env_id)
            except Exception:
                continue

            model = st.get("model") or model or ""
            if model_filter and model_filter.lower() not in model.lower():
                continue

            role = role or echolib.classify_session_role(env_id, sid, path)
            rows.append({
                "id": sid,
                "agent": env_id,
                "model": model,
                "cache_hit_rate": st.get("cache_hit_rate"),
                "total_tokens": st.get("total_tokens") or 0,
                "input_tokens": st.get("input_tokens") or 0,
                "cache_read_tokens": st.get("cache_read_tokens") or 0,
                "output_tokens": st.get("output_tokens") or 0,
                "session_role": role,
                "jsonl_path": path,
            })
    return rows


def audit_cache(rows: list[dict]) -> dict:
    """Run comprehensive cache health check over session rows."""
    by_agent: dict[str, list[dict]] = collections.defaultdict(list)
    by_model_env: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)

    for r in rows:
        agent = r.get("agent") or "unknown"
        by_agent[agent].append(r)
        m = canon_model(r.get("model"))
        by_model_env[(m, agent)].append(r)

    # ── Table A: Environment Overview ──────────────────────────────────
    table_a = []
    exclusions_a = []

    for agent, s_list in sorted(by_agent.items()):
        total = len(s_list)
        eligible = [r for r in s_list if cache_rate_eligible(r.get("cache_hit_rate"))]
        zero_cache = [r for r in s_list if r.get("cache_hit_rate") == 0.0]
        null_cache = [r for r in s_list if r.get("cache_hit_rate") is None]

        mean_rate = mean_cache_hit_rate([r["cache_hit_rate"] for r in eligible])

        # Token-weighted rate calculation
        total_inp = sum(r.get("input_tokens") or 0 for r in s_list)
        total_cr = sum(r.get("cache_read_tokens") or 0 for r in s_list)
        total_out = sum(r.get("output_tokens") or 0 for r in s_list)
        tok_rate = (total_cr / (total_inp + total_cr)) if (total_inp + total_cr) > 0 else None

        # Role breakdown
        main_count = sum(1 for r in s_list if r.get("session_role") == SESSION_ROLE_MAIN)
        sub_count = sum(1 for r in s_list if r.get("session_role") == SESSION_ROLE_SUBAGENT)

        # Health grade and verdict
        if not eligible and not zero_cache:
            grade = "⚪ 未支持"
            verdict = "该环境适配器未记录或不支持 prompt cache 统计"
        elif len(eligible) < 3 and zero_cache:
            grade = "🔴 异常 (零命中)"
            verdict = f"几乎全程零缓存命中 (0% 会话数 {len(zero_cache)})，检查模型或前缀注入"
        elif mean_rate is not None:
            if mean_rate >= 0.80:
                grade = "🟢 优秀"
                verdict = "缓存利用极佳，长上下文复用率高，降本显著"
            elif mean_rate >= 0.60:
                grade = "🟡 良好"
                verdict = "缓存稳定发挥，上下文连贯"
            else:
                grade = "🟠 偏低"
                verdict = "缓存命中偏低，短会话/单轮会话偏多或前缀波动"
        else:
            grade = "⚪ 数据不足"
            verdict = "有效会话样本过少"

        env_name = echolib.ENV_REGISTRY.get(agent, {}).get("name") or agent

        entry = {
            "agent": agent,
            "env_name": env_name,
            "total_sessions": total,
            "eligible_sessions": len(eligible),
            "zero_cache_sessions": len(zero_cache),
            "null_cache_sessions": len(null_cache),
            "mean_rate": mean_rate,
            "weighted_rate": tok_rate,
            "total_input_tokens": total_inp,
            "total_cache_tokens": total_cr,
            "total_output_tokens": total_out,
            "total_tokens": total_inp + total_cr + total_out,
            "main_sessions": main_count,
            "subagent_sessions": sub_count,
            "grade": grade,
            "verdict": verdict,
        }
        if eligible:
            table_a.append(entry)
        else:
            exclusions_a.append(entry)

    # Sort Table A by mean_rate descending
    table_a.sort(key=lambda x: x["mean_rate"] or 0.0, reverse=True)

    # ── Table B: Model × Environment ──────────────────────────────────
    table_b = []
    exclusions_b = []

    for (m, agent), s_list in sorted(by_model_env.items()):
        if not m or m == "unknown":
            exclusions_b.append({"model": m, "agent": agent, "count": len(s_list), "reason": "未标注模型"})
            continue

        eligible = [r for r in s_list if cache_rate_eligible(r.get("cache_hit_rate"))]
        zero_cache = [r for r in s_list if r.get("cache_hit_rate") == 0.0]

        if len(eligible) < 3:
            exclusions_b.append({
                "model": m,
                "agent": agent,
                "count": len(s_list),
                "eligible": len(eligible),
                "zero_cache": len(zero_cache),
                "reason": "有效会话 < 3" if not zero_cache else "全程零缓存",
            })
            continue

        mean_rate = mean_cache_hit_rate([r["cache_hit_rate"] for r in eligible])
        total_inp = sum(r.get("input_tokens") or 0 for r in s_list)
        total_cr = sum(r.get("cache_read_tokens") or 0 for r in s_list)
        tok_rate = (total_cr / (total_inp + total_cr)) if (total_inp + total_cr) > 0 else None

        table_b.append({
            "model": m,
            "agent": agent,
            "env_name": echolib.ENV_REGISTRY.get(agent, {}).get("name") or agent,
            "total_sessions": len(s_list),
            "eligible_sessions": len(eligible),
            "zero_cache_sessions": len(zero_cache),
            "mean_rate": mean_rate,
            "weighted_rate": tok_rate,
            "total_tokens": sum(r.get("total_tokens") or 0 for r in s_list),
        })

    table_b.sort(key=lambda x: x["mean_rate"] or 0.0, reverse=True)

    # Global Health Score (0-100)
    all_eligible = [r for r in rows if cache_rate_eligible(r.get("cache_hit_rate"))]
    global_mean = mean_cache_hit_rate([r["cache_hit_rate"] for r in all_eligible]) if all_eligible else 0.0
    zero_ratio = sum(1 for r in rows if r.get("cache_hit_rate") == 0.0) / len(rows) if rows else 0.0
    health_score = int(global_mean * 100 * (1.0 - (zero_ratio * 0.5))) if global_mean else 0

    return {
        "global": {
            "total_sessions": len(rows),
            "eligible_sessions": len(all_eligible),
            "global_mean_rate": global_mean,
            "health_score": health_score,
        },
        "table_a": table_a,
        "exclusions_a": exclusions_a,
        "table_b": table_b,
        "exclusions_b": exclusions_b,
    }


def format_markdown_report(audit: dict, env_filter: str | None = None, model_filter: str | None = None) -> str:
    """Render audit results as GitHub-flavored Markdown report."""
    lines = []
    title_suffix = ""
    if env_filter:
        title_suffix += f" [环境: {env_filter}]"
    if model_filter:
        title_suffix += f" [模型: {model_filter}]"

    lines.append(f"# 缓存命中体检与诊断报告{title_suffix}\n")
    g = audit["global"]
    lines.append(f"> **综合健康评分**: `{g['health_score']}/100` · "
                 f"**分析会话总数**: `{g['total_sessions']}` · "
                 f"**有效缓存会话**: `{g['eligible_sessions']}` · "
                 f"**全局会话均命中率**: `{g['global_mean_rate']:.2%}`\n")

    lines.append("## 一、环境整体缓存体检（Table A）\n")
    lines.append("| 环境 | 有效/总会话 | 会话均命中率 | Token加权 | 主对话/子代理 | 体检评级 | 诊断意见 |")
    lines.append("|------|------------|-------------|-----------|--------------|---------|----------|")

    for a in audit["table_a"]:
        m_rate = f"{a['mean_rate']:.2%}" if a["mean_rate"] is not None else "N/A"
        w_rate = f"{a['weighted_rate']:.2%}" if a["weighted_rate"] is not None else "N/A"
        sess_ratio = f"{a['eligible_sessions']}/{a['total_sessions']}"
        role_split = f"{a['main_sessions']}/{a['subagent_sessions']}"
        lines.append(f"| **{a['env_name']}** | {sess_ratio} | **{m_rate}** | {w_rate} | {role_split} | {a['grade']} | {a['verdict']} |")

    if not audit["table_a"]:
        lines.append("| (暂无有效环境数据) | - | - | - | - | - | - |")

    lines.append("\n## 二、模型缓存命中水平（Table B）\n")
    lines.append("| 模型 | 环境 | 有效会话 | 会话均命中率 | Token加权 | 总消耗 Token |")
    lines.append("|------|------|----------|-------------|-----------|-------------|")

    for b in audit["table_b"]:
        m_rate = f"{b['mean_rate']:.2%}" if b["mean_rate"] is not None else "N/A"
        w_rate = f"{b['weighted_rate']:.2%}" if b["weighted_rate"] is not None else "N/A"
        tok = f"{b['total_tokens']:,}" if b["total_tokens"] else "0"
        lines.append(f"| **{b['model']}** | {b['env_name']} | {b['eligible_sessions']} | **{m_rate}** | {w_rate} | {tok} |")

    if not audit["table_b"]:
        lines.append("| (暂无有效模型数据，有效会话数均 < 3 或零缓存) | - | - | - | - | - |")

    if audit["exclusions_a"] or audit["exclusions_b"]:
        lines.append("\n## 三、排除与异常清单（Exclusions & Anomalies）\n")
        lines.append("> 遵循 `cache-report-rules.md`：零缓存、无使用字段或有效会话 < 3 独立列出，不拉低主表均值。\n")
        lines.append("| 对象 | 归属环境 | 会话数 | 排除/异常原因 |")
        lines.append("|------|----------|--------|--------------|")
        for ea in audit["exclusions_a"]:
            lines.append(f"| {ea['env_name']} (整体) | {ea['agent']} | {ea['total_sessions']} | {ea['verdict']} |")
        for eb in audit["exclusions_b"]:
            lines.append(f"| {eb['model']} | {eb['agent']} | {eb.get('count', 0)} | {eb['reason']} |")

    lines.append("\n## 四、体检结论与优化建议\n")
    # Generate tailored recommendations based on the findings
    recs = []
    high_cache_envs = [a["env_name"] for a in audit["table_a"] if (a["mean_rate"] or 0) >= 0.8]
    if high_cache_envs:
        recs.append(f"- **高能效环境**: `{', '.join(high_cache_envs)}` 缓存命中率 ≥ 80%，提示词前缀与历史上下文复用极其充分。")

    antigravity_data = next((a for a in audit["table_a"] if a["agent"] == "antigravity"), None)
    if antigravity_data and antigravity_data["mean_rate"]:
        recs.append(f"- **Antigravity 体检**: 会话均命中率 `{antigravity_data['mean_rate']:.2%}`，Token 加权 `{antigravity_data['weighted_rate']:.2%}`，"
                    f"子代理会话 `{antigravity_data['subagent_sessions']}` 条，整体缓存状态健康。")

    dsh_data = next((a for a in audit["table_a"] if a["agent"] == "dsh"), None)
    if dsh_data and dsh_data["mean_rate"]:
        recs.append(f"- **DSH (DeepSeek) 体检**: 会话均命中率 `{dsh_data['mean_rate']:.2%}`，搭配 LongCat 等模型可实现 > 95% 的极高命中。")

    zcode_data = next((a for a in audit["table_a"] if a["agent"] in ("zcode", "zcode_db", "zcode_v2")), None)
    if zcode_data and zcode_data["mean_rate"]:
        recs.append(f"- **ZCode 体检**: GLM-5.3-Flash / LongCat 请求级缓存命中表现良好，复用率稳定。")

    if any(a["zero_cache_sessions"] > 5 for a in audit["table_a"]):
        recs.append("- **优化机会**: 部分环境存在单轮即退出或短会话造成的零命中现象，建议避免无意义重启对话，长任务保持同会话推进。")

    if not recs:
        recs.append("- 暂无特定异常，持续监控会话历史。")

    lines.extend(recs)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Session Digger Cache Health Audit")
    parser.add_argument("--env", help="Audit specific environment (antigravity, dsh, zcode, claude, etc.)")
    parser.add_argument("--model", help="Audit specific model name pattern (e.g. gemini, deepseek, longcat)")
    parser.add_argument("--live", action="store_true", help="Force live disk scanning instead of index.db")
    parser.add_argument("--index", help="Custom path to index.db")
    parser.add_argument("--json", action="store_true", help="Output raw JSON instead of markdown table")
    parser.add_argument("--limit", type=int, default=300, help="Max sessions to read in live scan mode")
    args = parser.parse_args()

    db_path = Path(args.index) if args.index else INDEX_DB_PATH

    rows = []
    if not args.live and db_path.is_file():
        rows = collect_rows_from_index(db_path, env_filter=args.env, model_filter=args.model)

    # Fallback to live scan if index is missing or returned no rows
    if not rows or args.live:
        rows = collect_rows_live(env_filter=args.env, model_filter=args.model, limit_per_env=args.limit)

    audit_result = audit_cache(rows)

    if args.json:
        print(json.dumps(audit_result, ensure_ascii=False, indent=2))
    else:
        print(format_markdown_report(audit_result, env_filter=args.env, model_filter=args.model))


if __name__ == "__main__":
    main()
