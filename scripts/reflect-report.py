#!/usr/bin/env python3
"""
reflect-report.py — Local multi-environment AI usage recap HTML.

Reads the session-digger SQLite index (default ~/.claude/.session-digger/index.db)
and writes a single-file HTML report: overview + per-env pages, calendar / hour
heatmaps, task mix, summary topics, three reflective questions, quiet-hours
settings (browser-local).

Zero pip dependencies. UI shell: reflect-report.template.html (same directory).

Examples:
  python3 scripts/reflect-report.py --help
  python3 scripts/reflect-report.py --months 1 --open
  python3 scripts/reflect-report.py --db ~/.claude/.session-digger/index.db -o /tmp/reflect.html
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import webbrowser
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATE_NAME = "reflect-report.template.html"
DEFAULT_LOOKBACK_MONTHS = 12
MAX_MINUTES = 8 * 60  # cap single-session span estimate

# Boilerplate / noise summaries — never surface as "topics"
_TOPIC_BAN = re.compile(
    r"(?:"
    r"^subagent\b"
    r"|^approval\s*reviewer\b"
    r"|^new\s*session\b"
    r"|^trae\s*cn\s*summary\b"
    r"|^say\s*hello\b"
    r"|^handle\s+user\s+instruction\b"
    r"|^reply\s+with\s+ok\b"
    r"|^\[system\]"
    r"|^\[request interrupted"
    r"|request interrupted by user"
    r"|^you are (?:part of a multi-agent|a dns|an? )\b"
    r"|^you are longcat\b"
    r"|^you are mimo\b"
    r"|^agent-relay\s+接手\b"
    r"|你正在通过\s*agent-relay"
    r"|介绍.{0,6}你的能力"
    r"|你能有多聪明"
    r"|人能有多聪明"
    r"|你是一个.{0,24}(?:测试|评估|用例|专家)"
    r"|你是\s*(?:longcat|mimo|deepseek|step)[^\n]{0,40}模型"
    r"|请用一句话说明你当前运行的模型"
    r"|尽一切可能找出当前你正在使用的模型"
    r"|告诉我你当前运行在哪个模型"
    r"|诊断(?:任务)?[：:].{0,20}模型"
    r"|回复一句话确认你已启动"
    r"|测试调用[：:].{0,30}模型"
    r"|你是什么模型"
    r"|你是哪个模型"
    r"|直接回答模型名称"
    r"|说出你的模型名称"
    r"|一句话确认你已启动"
    r"|运行模型[？?]"
    r"|只回答模型名"
    r"|say\s*[\"']hello[\"']"
    r"|hello[\"']?\s+in exactly"
    r"|speedtest-cli"
    r"|快速回答[：:].{0,12}当前日期"
    r"|^回复\s*ok\s*$"
    r"|^回复ok$"
    r"|^ok$"
    r"|^test$"
    r"|^hi$"
    r"|^hey$"
    r"|^users$"
    r")",
    re.I,
)

# Per-session cap for Token charts (raw kept on session for detail).
# Extreme Dim/agent loops otherwise dominate cross-env share bars.
TOKEN_DISPLAY_CAP = 2_000_000

# 单一真源：模型名归一（含 GPT 别名 / longcat-preview 兜底）
from echolib._models import normalize_model_name, MODEL_ALIASES


def display_tokens(tok: int) -> int:
    """Winsorize per-session tokens for cross-env comparison charts."""
    t = int(tok or 0)
    if t <= 0:
        return 0
    return t if t <= TOKEN_DISPLAY_CAP else TOKEN_DISPLAY_CAP
# Paths / code dumps / pure noise in labels
_TOPIC_NOISE = re.compile(
    r"(?:"
    r"https?://"
    r"|[\\/]Users[\\/]"
    r"|[\\/]home[\\/]"
    r"|\$HOME"
    r"|curl\s+"
    r"|```"
    r"|Authorization:"
    r"|api[_-]?key"
    r"|sk-[A-Za-z0-9]"
    r")",
    re.I,
)
_TOPIC_MAX_LEN = 42


def _filter_label(label: str) -> bool:
    """Return True iff `label` is a usable human-readable topic string.

    Pure function — centralises that a label is rejected when it matches
    either the boilerplate ban list or the noise pattern. Keeps the two
    compiled regex objects as implementation details so callers do not
    re-check them inline (previously inlined at 6 call sites).
    """
    if not label:
        return False
    if _TOPIC_BAN.search(label):
        return False
    if _TOPIC_NOISE.search(label[:80]):
        return False
    return True


# ── Task classification schema (tool-usage + keyword hybrid) ──────────────

_TOOL_SCORE_MAX = 6  # tool-distribution points cap per category

_TOOL_CAT = {
    "写代码": {"Write", "Edit", "Bash", "Grep", "Glob", "GrepTool"},
    "调试/修复": {"Bash", "Grep", "Read", "DiagnosingBugs", "Bash"},
    "查资料/研究": {"WebSearch", "WebFetch", "Browse", "WebCrawl", "Fetch"},
    "阅读/笔记": {"Read", "NotebookEdit"},
    "写文章/文案": {"Write", "Edit"},
    "数据分析": {"Bash", "Read", "Write", "WebFetch"},
    "设计/规划": {"Plan", "Think", "Write"},
    "投资分析": {"WebSearch", "WebFetch", "Read", "Bash", "Eodhd", "Eastmoney"},
}

# Ambiguous tools used across many categories — weighted lower
_AMBIGUOUS_TOOLS = {"Bash", "Read", "Write", "Agent"}

_CAT_KEYWORDS = {
    "调试/修复": {"bug", "报错", "异常", "错误", "崩溃", "修", "修复", "排查", "定位"},
    "写代码": {"实现", "函数", "重构", "算法", "类", "接口", "优化", "代码", "模块"},
    "写文章/文案": {"写", "文章", "文案", "标题", "公众号", "小红书", "润色", "改稿", "封面", "配图", "海报"},
    "查资料/研究": {"查", "搜索", "资料", "研究", "了解", "调研", "对比", "研究"},
    "阅读/笔记": {"读", "阅读", "划线", "笔记", "这本书", "读过"},
    "数据分析": {"数据", "统计", "报表", "图表", "趋势", "指标", "回测"},
    "设计/规划": {"方案", "设计", "规划", "架构", "结构", "模式", "设计"},
    "投资分析": {"投资", "基金", "股票", "估值", "回报", "收益率", "PE", "PB", "资产", "财务"},
}

_CAT_ORDER = [
    "写代码", "调试/修复", "写文章/文案", "查资料/研究",
    "阅读/笔记", "数据分析", "设计/规划", "投资分析",
    "综合协作", "闲聊/纯对话",
]


def classify_hybrid(text: str, total_tools: int, tool_usage: dict[str, int]) -> str:
    """Two-signal task classification: tool distribution + summary keywords.

    *Tool distribution*: what fraction of tool calls fall into each category's
    tool set.  Categories whose tools are dominated by ambiguous tools get a
    soft penalty.

    *Keyword boost*: if the summary contains a category's signal words and the
    tool signal is plausible, bump score by 0.5.

    Fallback: total_tools == 0 → 闲聊/纯对话;  otherwise → 综合协作.
    """
    if not isinstance(tool_usage, dict) or not tool_usage:
        # fall back to legacy keywords-only when no tool_usage available
        t = text or ""
        if total_tools == 0:
            return "闲聊/纯对话"
        pl = t.lower()
        if any(k in t for k in ("写", "文章", "文案", "标题", "公众号", "小红书", "润色", "改稿")):
            return "写文章/文案"
        if any(k in t for k in ("查", "搜索", "资料", "研究", "了解")) or "search" in pl:
            return "查资料/研究"
        if any(k in t for k in ("读", "阅读", "划线", "笔记", "这本书")):
            return "阅读/笔记"
        if any(k in t for k in ("代码", "bug", "修复", "实现", "函数", "重构", "报错")) or total_tools >= 3:
            return "写代码"
        if any(k in t for k in ("数据", "统计", "报表", "图表", "趋势")):
            return "数据分析"
        if any(k in t for k in ("方案", "设计", "规划", "架构")):
            return "设计/规划"
        if any(k in t for k in ("投资", "基金", "股票", "估值")):
            return "投资分析"
        return "综合协作"

    total = sum(tool_usage.values())
    if total == 0:
        return "闲聊/纯对话"

    # ── tool-distribution score ──
    scores: dict[str, float] = {}
    for cat, tools in _TOOL_CAT.items():
        cat_tool_sum = sum(tool_usage.get(t, 0) for t in tools if t in tool_usage)
        if cat_tool_sum == 0:
            continue
        # Penalise categories whose tools are mostly ambiguous
        ambig_share = sum(tool_usage.get(t, 0) for t in tools & _AMBIGUOUS_TOOLS) / cat_tool_sum
        weight = 1.0 if ambig_share < 0.6 else 0.5
        pt = (cat_tool_sum / total) * _TOOL_SCORE_MAX * weight
        if pt > 0.1:
            scores[cat] = pt

    # ── keyword boost ──
    t = text or ""
    for cat, kwset in _CAT_KEYWORDS.items():
        boost = sum(1 for k in kwset if k in t)
        if boost:
            scores[cat] = scores.get(cat, 0) + 0.5 * min(boost, 3)

    if not scores:
        return "综合协作"

    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    return best


def _data_dir() -> Path:
    # 单一真源：复用 index_builder._schema.DB_DIR
    from index_builder._schema import DB_DIR
    return DB_DIR


def default_db_path() -> Path:
    from index_builder._schema import DB_PATH
    return DB_PATH


def default_out_path() -> Path:
    reports = _data_dir() / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    return reports / f"reflect-{stamp}.html"


def parse_dt(s):
    if not s:
        return None
    s = str(s).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except Exception:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(str(s)[:19], fmt)
            except Exception:
                pass
        return None


def family(agent: str) -> str:
    a = (agent or "").lower()
    if a in ("kimi_code", "kimi"):
        return "kimi"
    if a in ("dimcode", "dim"):
        return "dim"
    if a in ("universal", "qoder"):
        return "other"
    return a or "other"





def _clean_topic_text(text: str) -> str:
    s = re.sub(r"\s+", " ", (text or "").strip())
    s = s.strip("「」『』\"'“”‘’ \t")
    # drop leading slash-command only if nothing else (keep "/taxue 赚不到钱")
    return s


def _is_readable_topic(label: str) -> bool:
    if not label or len(label) < 2:
        return False
    if len(label) > _TOPIC_MAX_LEN + 2:  # allow ellipsis
        return False
    if not _filter_label(label):
        return False
    # pure punctuation / digits
    if re.fullmatch(r"[\d\W_]+", label, flags=re.UNICODE):
        return False
    cjk = re.findall(r"[\u4e00-\u9fff]", label)
    latin = re.findall(r"[A-Za-z]{3,}", label)
    # pure Chinese words (报销、备案、基金) — allow 2+ chars
    if re.fullmatch(r"[\u4e00-\u9fff]{2,12}", label):
        return True
    # reject short non-word debris (old TF-IDF scraps mixed with punctuation)
    if len(label) < 4 and not latin:
        return False
    # single English token that is too generic
    if len(latin) == 1 and len(cjk) == 0 and latin[0].lower() in {
        "subagent", "reviewer", "approval", "summary", "session", "request",
        "interrupted", "turns", "model", "test", "hello", "preview",
    }:
        return False
    return True


def _headline_from_summary(summary: str) -> str | None:
    """Turn a session summary into one human-readable topic label.

    Prefer whole short summaries; for long text take the first clause.
    Never emit Chinese character-bigrams (旧版 TF-IDF 碎片的来源).
    """
    s = _clean_topic_text(summary)
    if len(s) < 4:
        return None
    if not _filter_label(s):
        # still try first Chinese/English sentence without the URL/path head
        s2 = re.sub(r"https?://\S+", "", s)
        s2 = re.sub(r"(?:/Users|/home)[^\s，。]{0,80}", "", s2)
        s2 = _clean_topic_text(s2)
        if len(s2) < 4 or not _filter_label(s2):
            return None
        s = s2

    # strip markdown heading / list prefix
    s = re.sub(r"^#{1,6}\s*", "", s)
    s = re.sub(r"^[\-\*\d]+[\.\)]\s*", "", s)

    if len(s) <= _TOPIC_MAX_LEN:
        label = s
    else:
        head = s[: _TOPIC_MAX_LEN + 12]
        cut = None
        for sep in ("。", "？", "！", "；", "\n", "? ", "! ", "; ", "，", ", "):
            i = head.find(sep)
            if 6 <= i <= _TOPIC_MAX_LEN:
                cut = i
                break
        if cut is None:
            # break on space for English; else hard cut
            if " " in head[:_TOPIC_MAX_LEN]:
                cut = head[:_TOPIC_MAX_LEN].rfind(" ")
            else:
                cut = _TOPIC_MAX_LEN
        label = head[:cut].strip(" ，,;；") + "…"

    label = _clean_topic_text(label)
    if not _is_readable_topic(label):
        return None
    return label


def _enrich_topic_phrases(sessions: list[dict]) -> None:
    """Attach human-readable topic labels (0–1 per session) from summary."""
    for s in sessions:
        label = _headline_from_summary(s.get("summary") or "")
        s["topics"] = [label] if label else []


def estimate_minutes(duration_seconds, created: datetime, modified: datetime | None) -> float:
    if duration_seconds is not None and float(duration_seconds) > 0:
        return min(float(duration_seconds) / 60.0, MAX_MINUTES)
    end = modified or created
    if end >= created:
        span = (end - created).total_seconds() / 60.0
        if span <= 0:
            return 0.0
        return min(span, MAX_MINUTES)
    return 0.0


def load_sessions(db_path: Path, lookback_months: int) -> list[dict]:
    if not db_path.is_file():
        raise FileNotFoundError(
            f"Index not found: {db_path}\n"
            "Build it first: python3 scripts/index-builder.py build"
        )
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
    need = {
        "id", "agent", "created", "modified", "message_count", "user_messages",
        "tool_calls", "errors", "total_tokens", "summary", "first_prompt",
        "duration_seconds", "project_name", "tool_usage_json",
    }
    missing = need - cols
    if missing:
        con.close()
        raise RuntimeError(f"index.db sessions table missing columns: {sorted(missing)}")
    has_model = "model" in cols

    cut = datetime.now() - timedelta(days=lookback_months * 30.437)
    sessions = []
    select_cols = (
        "id, agent, created, modified, message_count, user_messages, "
        "tool_calls, errors, total_tokens, summary, first_prompt, "
        "duration_seconds, project_name, tool_usage_json"
        + (", model" if has_model else "")
    )
    for r in con.execute(f"SELECT {select_cols} FROM sessions"):
        dt = parse_dt(r["created"]) or parse_dt(r["modified"])
        if not dt:
            continue
        if dt < cut:
            continue
        end = parse_dt(r["modified"]) or dt
        summary = (r["summary"] or "").strip() or (r["first_prompt"] or "").strip()[:120]
        tools = int(r["tool_calls"] or 0)
        # parse tool_usage_json into dict
        tu_raw = r["tool_usage_json"]
        tool_usage: dict[str, int] = {}
        if tu_raw:
            try:
                tu_parsed = json.loads(tu_raw)
                if isinstance(tu_parsed, dict):
                    tool_usage = {str(k): int(v) for k, v in tu_parsed.items() if v}
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        model = ""
        if has_model:
            model = normalize_model_name(r["model"] or "")
        raw_tok = int(r["total_tokens"] or 0)
        sessions.append(
            {
                "id": r["id"],
                "agent": r["agent"] or "",
                "family": family(r["agent"]),
                "created": dt.isoformat(timespec="seconds"),
                "date": dt.strftime("%Y-%m-%d"),
                "hour": dt.hour,
                "weekday": dt.weekday(),  # Mon=0, matches JS labels
                "minutes": round(estimate_minutes(r["duration_seconds"], dt, end), 1),
                "messages": int(r["message_count"] or 0),
                "user_messages": int(r["user_messages"] or 0),
                "tools": tools,
                "tool_usage": tool_usage,
                "errors": int(r["errors"] or 0),
                "tokens": raw_tok,
                "tokens_display": display_tokens(raw_tok),
                "model": model,
                "project": (r["project_name"] or "")[:60],
                "summary": summary[:160],
            }
        )
    con.close()

    # ── Post-pass: classify + topic_phrases (need full corpus for TF-IDF) ──
    _enrich_topic_phrases(sessions)
    for s in sessions:
        s["task"] = classify_hybrid(
            s["summary"], s["tools"], s.get("tool_usage", {}),
        )
    return sessions


def load_template() -> str:
    path = SCRIPT_DIR / TEMPLATE_NAME
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing UI template: {path}\n"
            "Expected reflect-report.template.html next to reflect-report.py"
        )
    text = path.read_text(encoding="utf-8")
    if "__REFLECT_DATA__" not in text:
        raise RuntimeError(f"Template missing __REFLECT_DATA__ placeholder: {path}")
    return text


def set_default_months(html: str, months: int) -> str:
    """No-op for static buttons (period UI is JS-rendered). Kept for API stability."""
    return html


def set_js_default_months(html: str, months: int) -> str:
    """Inject default period into client state. Template uses `let mo = N`."""
    months = int(months) if int(months) in (1, 3, 6, 12) else 1
    html2, n = re.subn(
        r"let\s+mo\s*=\s*\d+",
        f"let mo = {months}",
        html,
        count=1,
    )
    if n:
        return html2
    # older templates
    html2, n = re.subn(
        r"let\s+months\s*=\s*\d+",
        f"let months = {months}",
        html,
        count=1,
    )
    return html2 if n else html


def compute_usage_insights(sessions: list[dict]) -> dict:
    """Token + model preference aggregates for report UI.

    Only counts sessions where the field is present (no zero-fill fiction).
    Token charts use per-session winsorize (TOKEN_DISPLAY_CAP) so one env
    with multi-10M agent loops cannot own the whole bar chart.
    Monthly series supports period-over-period preference charts.
    """
    by_model: Counter = Counter()
    model_tokens: Counter = Counter()
    family_tokens: Counter = Counter()
    family_tokens_raw: Counter = Counter()
    family_model: dict[str, Counter] = {}
    monthly_tokens: dict[str, int] = {}
    monthly_sessions_with_tok: dict[str, int] = {}
    monthly_models: dict[str, Counter] = {}

    tok_sessions = 0
    total_tokens = 0
    total_tokens_raw = 0
    capped_sessions = 0
    model_sessions = 0

    for s in sessions:
        fam = s.get("family") or "other"
        m = normalize_model_name(s.get("model") or "")
        tok_raw = int(s.get("tokens") or 0)
        tok = int(s.get("tokens_display") or display_tokens(tok_raw))
        month = (s.get("date") or "")[:7]
        if m:
            model_sessions += 1
            by_model[m] += 1
            family_model.setdefault(fam, Counter())[m] += 1
            if month:
                monthly_models.setdefault(month, Counter())[m] += 1
            if tok_raw > 0:
                model_tokens[m] += tok
        if tok_raw > 0:
            tok_sessions += 1
            total_tokens_raw += tok_raw
            total_tokens += tok
            if tok_raw > TOKEN_DISPLAY_CAP:
                capped_sessions += 1
            family_tokens[fam] += tok
            family_tokens_raw[fam] += tok_raw
            if month:
                monthly_tokens[month] = monthly_tokens.get(month, 0) + tok
                monthly_sessions_with_tok[month] = monthly_sessions_with_tok.get(month, 0) + 1

    top_models = [
        {"model": name, "sessions": n, "tokens": int(model_tokens.get(name, 0))}
        for name, n in by_model.most_common(12)
    ]
    months_sorted = sorted(set(list(monthly_tokens.keys()) + list(monthly_models.keys())))
    series = []
    for mo in months_sorted:
        mc = monthly_models.get(mo, Counter())
        top = mc.most_common(1)[0] if mc else ("", 0)
        series.append(
            {
                "month": mo,
                "tokens": int(monthly_tokens.get(mo, 0)),
                "token_sessions": int(monthly_sessions_with_tok.get(mo, 0)),
                "top_model": top[0],
                "top_model_sessions": int(top[1]),
                "models": dict(mc.most_common(8)),
            }
        )

    # half-period deltas for tokens when enough data (use display tokens)
    with_tok = [s for s in sessions if (s.get("tokens") or 0) > 0]
    with_tok.sort(key=lambda x: x.get("created") or "")
    token_shift = {"has_data": False}
    if len(with_tok) >= 6:
        mid = len(with_tok) // 2
        f, s2 = with_tok[:mid], with_tok[mid:]
        ft = sum(int(x.get("tokens_display") or display_tokens(x.get("tokens") or 0)) for x in f)
        st = sum(int(x.get("tokens_display") or display_tokens(x.get("tokens") or 0)) for x in s2)
        token_shift = {
            "has_data": True,
            "first_n": len(f),
            "second_n": len(s2),
            "first_tokens": ft,
            "second_tokens": st,
            "delta_ratio": round((st - ft) / ft, 3) if ft else None,
        }

    model_shift = {"has_data": False}
    with_m = [s for s in sessions if normalize_model_name(s.get("model") or "")]
    with_m.sort(key=lambda x: x.get("created") or "")
    if len(with_m) >= 6:
        mid = len(with_m) // 2
        f, s2 = with_m[:mid], with_m[mid:]
        fc = Counter(normalize_model_name(x.get("model") or "") for x in f)
        sc = Counter(normalize_model_name(x.get("model") or "") for x in s2)
        model_shift = {
            "has_data": True,
            "first_top": fc.most_common(3),
            "second_top": sc.most_common(3),
        }

    return {
        "token_sessions": tok_sessions,
        "total_tokens": total_tokens,
        "total_tokens_raw": total_tokens_raw,
        "token_cap": TOKEN_DISPLAY_CAP,
        "token_capped_sessions": capped_sessions,
        "model_sessions": model_sessions,
        "top_models": top_models,
        "family_tokens": dict(family_tokens),
        "family_tokens_raw": dict(family_tokens_raw),
        "family_models": {k: dict(v.most_common(5)) for k, v in family_model.items()},
        "monthly": series,
        "token_shift": token_shift,
        "model_shift": model_shift,
    }


def compute_env_profiles(sessions: list[dict]) -> dict:
    """Per-family data-fidelity profile: what the index actually holds.

    Used by the report UI to hide empty metrics and pick primary callouts.
    Rates are 0–1 fractions of sessions with non-empty / positive values.
    """
    by: dict[str, list] = {}
    for s in sessions:
        by.setdefault(s.get("family") or "other", []).append(s)

    def rate(ss, pred) -> float:
        if not ss:
            return 0.0
        return round(sum(1 for s in ss if pred(s)) / len(ss), 3)

    profiles: dict[str, dict] = {}
    for fam, ss in by.items():
        n = len(ss)
        r_sum = rate(ss, lambda s: bool((s.get("summary") or "").strip() and len((s.get("summary") or "").strip()) >= 4))
        r_top = rate(ss, lambda s: bool(s.get("topics")))
        r_tok = rate(ss, lambda s: (s.get("tokens") or 0) > 0)
        r_tool = rate(ss, lambda s: (s.get("tools") or 0) > 0)
        r_min = rate(ss, lambda s: (s.get("minutes") or 0) > 0)
        r_msg = rate(ss, lambda s: (s.get("messages") or 0) > 0)
        r_model = rate(ss, lambda s: bool((s.get("model") or "").strip()))
        # primary callout for cards: strongest signal this env can offer
        # prefer time → tools → tokens → sessions
        if r_min >= 0.25 and sum(s.get("minutes") or 0 for s in ss) > n * 2:
            primary = "minutes"
        elif r_tool >= 0.25:
            primary = "tools"
        elif r_tok >= 0.25:
            primary = "tokens"
        else:
            primary = "sessions"
        # what analysis layers make sense
        layers = ["sessions", "calendar", "hour"]
        if r_min >= 0.15:
            layers.append("duration")
        if r_tool >= 0.15:
            layers.append("tools")
        if r_tok >= 0.15:
            layers.append("tokens")
        if r_model >= 0.15:
            layers.append("models")
        if r_sum >= 0.1 or r_top >= 0.1:
            layers.append("topics")
        if r_tool >= 0.1 or r_sum >= 0.1:
            layers.append("tasks")
        top_m = Counter((s.get("model") or "").strip() for s in ss if (s.get("model") or "").strip())
        profiles[fam] = {
            "n": n,
            "summary": r_sum,
            "topics": r_top,
            "tokens": r_tok,
            "tools": r_tool,
            "minutes": r_min,
            "messages": r_msg,
            "models": r_model,
            "primary": primary,
            "layers": layers,
            "total_minutes": round(sum(s.get("minutes") or 0 for s in ss), 1),
            "total_tokens": int(sum(s.get("tokens") or 0 for s in ss)),
            "total_tools": int(sum(s.get("tools") or 0 for s in ss)),
            "total_messages": int(sum(s.get("messages") or 0 for s in ss)),
            "top_model": top_m.most_common(1)[0][0] if top_m else "",
        }
    # cross-env overview profile
    all_ss = sessions
    if all_ss:
        profiles["_all"] = {
            "n": len(all_ss),
            "summary": round(sum(1 for s in all_ss if (s.get("summary") or "").strip()) / len(all_ss), 3),
            "topics": round(sum(1 for s in all_ss if s.get("topics")) / len(all_ss), 3),
            "tokens": round(sum(1 for s in all_ss if (s.get("tokens") or 0) > 0) / len(all_ss), 3),
            "tools": round(sum(1 for s in all_ss if (s.get("tools") or 0) > 0) / len(all_ss), 3),
            "minutes": round(sum(1 for s in all_ss if (s.get("minutes") or 0) > 0) / len(all_ss), 3),
            "messages": round(sum(1 for s in all_ss if (s.get("messages") or 0) > 0) / len(all_ss), 3),
            "primary": "minutes",
            "layers": ["sessions", "calendar", "hour", "duration", "tools", "tokens", "topics", "tasks"],
            "total_minutes": round(sum(s.get("minutes") or 0 for s in all_ss), 1),
            "total_tokens": int(sum(s.get("tokens") or 0 for s in all_ss)),
            "total_tools": int(sum(s.get("tools") or 0 for s in all_ss)),
            "total_messages": int(sum(s.get("messages") or 0 for s in all_ss)),
        }
    return profiles


def compute_trend(sessions: list[dict]) -> dict:
    """Split the session list into two halves (by time) and compute deltas.

    Returns a dict with first/second period metrics and deltas for:
    - session count
    - total minutes
    - error rate
    - avg tools per session
    - avg messages per session
    """
    if len(sessions) < 4:
        return {"has_data": False}
    sorted_s = sorted(sessions, key=lambda x: x.get("created", ""))
    mid = len(sorted_s) // 2
    first, second = sorted_s[:mid], sorted_s[mid:]

    def agg(ss):
        n = len(ss)
        total_min = sum(s.get("minutes", 0) for s in ss)
        err_sum = sum(s.get("errors", 0) for s in ss)
        tool_sum = sum(s.get("tools", 0) for s in ss)
        msg_sum = sum(s.get("messages", 0) for s in ss)
        return {
            "n": n,
            "total_min": round(total_min, 1),
            "error_rate": round(err_sum / max(1, n), 3),
            "avg_tools": round(tool_sum / max(1, n), 1),
            "avg_msgs": round(msg_sum / max(1, n), 1),
        }

    f = agg(first)
    s = agg(second)

    def delta(a, b):
        if a == 0:
            return None
        return round((b - a) / a, 3)

    return {
        "has_data": True,
        "first_label": sorted_s[0].get("date", ""),
        "second_label": sorted_s[-1].get("date", ""),
        "first": f,
        "second": s,
        "deltas": {
            "n": f["n"] - s["n"],  # absolute diff
            "total_min_delta": delta(f["total_min"], s["total_min"]),
            "error_rate_delta": delta(f["error_rate"], s["error_rate"]),
            "avg_tools_delta": delta(f["avg_tools"], s["avg_tools"]),
            "avg_msgs_delta": delta(f["avg_msgs"], s["avg_msgs"]),
        },
    }


def build_html(sessions: list[dict], months: int, db_note: str = "") -> str:
    profiles = compute_env_profiles(sessions)
    insights = compute_usage_insights(sessions)
    payload = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "sessions": sessions,
        "trend": compute_trend(sessions),
        "profiles": profiles,
        "insights": insights,
        # static legend: what each index field means (UI footnotes)
        "field_guide": {
            "sessions": "会话条数（索引能列出的对话）",
            "minutes": "用时估算（会话跨度，非盯屏秒表）",
            "messages": "消息条数",
            "tools": "工具调用次数（编码型环境更准）",
            "tokens": "Token 消耗（有记录才统计，不把 0 当成真零）",
            "models": "会话主模型（从 transcript 抽取，无则跳过）",
            "topics": "可读摘要标题（依赖 summary / first_prompt）",
            "tasks": "任务类型启发式（工具分布 + 摘要关键词）",
        },
    }
    if db_note:
        payload["index_note"] = db_note
    embed = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = load_template()
    # Inject design-system tokens from source-of-truth CSS file.
    tokens_css = (SCRIPT_DIR / "design-tokens.css").read_text(encoding="utf-8")
    html = html.replace("__DESIGN_TOKENS_CSS__", tokens_css, 1)
    html = html.replace("__REFLECT_DATA__", embed, 1)
    html = set_default_months(html, months)
    html = set_js_default_months(html, months)
    return html


def redact_path(p: Path) -> str:
    s = str(p)
    home = str(Path.home())
    if s.startswith(home):
        s = "~" + s[len(home) :]
    name = Path.home().name
    if name:
        s = s.replace(name, "<user>")
    s = re.sub(r"/Users/[^/]+", "/Users/<user>", s)
    s = re.sub(r"/home/[^/]+", "/home/<user>", s)
    return s


def open_browser(path: Path) -> None:
    url = path.resolve().as_uri()
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path.resolve())], check=False)
        else:
            webbrowser.open(url)
    except Exception as e:
        print(f"Could not open browser: {e}", file=sys.stderr)
        print(f"Open manually: {url}", file=sys.stderr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="reflect-report.py",
        description="Generate a local multi-environment AI usage recap HTML from the session-digger index.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to index.db (default: $SESSION_DIGGER_DATA_DIR/index.db or ~/.claude/.session-digger/index.db)",
    )
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="Output HTML path (default: ~/.claude/.session-digger/reports/reflect-YYYY-MM-DD.html)",
    )
    parser.add_argument(
        "--months",
        type=int,
        default=1,
        choices=(1, 3, 6, 12),
        help="Default period selected in the report UI (default: 1)",
    )
    parser.add_argument(
        "--lookback",
        type=int,
        default=DEFAULT_LOOKBACK_MONTHS,
        help=f"Include sessions from the last N months in the data payload (default: {DEFAULT_LOOKBACK_MONTHS})",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the generated HTML in the default browser",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Also write the session payload JSON to this path",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print family/task counts to stdout",
    )
    args = parser.parse_args(argv)

    db_path = (args.db or default_db_path()).expanduser()
    out_path = (args.out or default_out_path()).expanduser()

    try:
        sessions = load_sessions(db_path, lookback_months=max(1, args.lookback))
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

    note = f"source={redact_path(db_path)} n={len(sessions)} lookback_months={args.lookback}"
    html = build_html(sessions, months=args.months, db_note=note)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    if args.json:
        args.json.expanduser().parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated": datetime.now().isoformat(timespec="seconds"),
            "sessions": sessions,
            "index_note": note,
        }
        args.json.expanduser().write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(f"wrote {redact_path(out_path)} ({len(sessions)} sessions, {out_path.stat().st_size // 1024} KB)")
    fam = Counter(s["family"] for s in sessions)
    tasks = Counter(s["task"] for s in sessions)
    print("families:", ", ".join(f"{k}={v}" for k, v in fam.most_common()))
    profiles = compute_env_profiles(sessions)
    if args.stats:
        print("tasks:", ", ".join(f"{k}={v}" for k, v in tasks.most_common()))
        print("env coverage (summary/tools/tokens/minutes):")
        for fam_k, p in sorted(
            ((k, v) for k, v in profiles.items() if k != "_all"),
            key=lambda x: -x[1].get("n", 0),
        ):
            print(
                f"  {fam_k}: n={p['n']} sum={p['summary']:.0%} "
                f"tools={p['tools']:.0%} tok={p['tokens']:.0%} "
                f"min={p['minutes']:.0%} primary={p['primary']} layers={','.join(p['layers'])}"
            )

    if args.open:
        open_browser(out_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
