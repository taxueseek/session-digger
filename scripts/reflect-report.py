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

STOP = set(
    """
的了是我你在和有就不也把被这那吗呢啊吧要会能对与或及等一个这个那个什么怎么如何帮我请我们他们是否可以不用没有已经还是或者因为所以如果但是关于进行使用通过以及为了时候
the a an to of for and is in on it this that with from as by or be are was were can could would should will just please help me my i you we they how what when where why which use using used make do did does new session say hello tell model what
request interrupted user claude code skill tool agent docs https http com org json md py sh ts js true false null
""".split()
)

SKIP_TOPIC = re.compile(
    r"^(subagent|approval reviewer|longcat|trae cn summary|new session|"
    r"say hello|介绍下你的能力|users|python|github|preview|ok|test|hi|hey)\b",
    re.I,
)

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
    env = os.environ.get("SESSION_DIGGER_DATA_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".claude" / ".session-digger"


def default_db_path() -> Path:
    return _data_dir() / "index.db"


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





def _enrich_topic_phrases(sessions: list[dict]) -> None:
    from collections import defaultdict
    import math
    n = len(sessions)
    if n < 2:
        for s in sessions:
            s["topics"] = []
        return
    def tokenise(text: str) -> list[str]:
        if not text:
            return []
        tokens: list[str] = []
        chars: list[str] = []
        for ch in text:
            if "\u4e00" <= ch <= "\u9fff":
                chars.append(ch)
            else:
                if len(chars) >= 2:
                    for i in range(len(chars) - 1):
                        t = chars[i] + chars[i + 1]
                        if t not in STOP:
                            tokens.append(t)
                chars = []
        if len(chars) >= 2:
            for i in range(len(chars) - 1):
                t = chars[i] + chars[i + 1]
                if t not in STOP:
                    tokens.append(t)
        for w in __import__('re').findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", text):
            wl = w.lower()
            if wl not in STOP and len(wl) >= 3:
                tokens.append(wl)
        return tokens
    df: dict[str, int] = defaultdict(int)
    session_tokens: list[list[str]] = []
    for s in sessions:
        toks = tokenise(s.get("summary") or "")
        session_tokens.append(toks)
        seen = set(toks)
        for t in seen:
            df[t] += 1
    for s, toks in zip(sessions, session_tokens):
        if not toks:
            s["topics"] = []
            continue
        tf: dict[str, int] = defaultdict(int)
        for t in toks:
            tf[t] += 1
        scored = [(t, tf[t] * math.log(n / max(1, df[t]))) for t in set(toks)]
        scored.sort(key=lambda x: -x[1])
        s["topics"] = [t for t, w in scored[:5] if w > 0.01]


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

    cut = datetime.now() - timedelta(days=lookback_months * 30.437)
    sessions = []
    for r in con.execute(
        """
        SELECT id, agent, created, modified, message_count, user_messages,
               tool_calls, errors, total_tokens, summary, first_prompt,
               duration_seconds, project_name, tool_usage_json
        FROM sessions
        """
    ):
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
                "tokens": int(r["total_tokens"] or 0),
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
    """Mark the matching period button active (client still can switch)."""
    months = int(months)
    if months not in (1, 3, 6, 12):
        months = 1

    def repl(m):
        mval = int(m.group(1))
        label = m.group(2)
        cls = " class=\"active\"" if mval == months else ""
        return f'<button data-m="{mval}"{cls}>{label}</button>'

    return re.sub(
        r'<button data-m="(\d+)"(?: class="active")?>([^<]*)</button>',
        repl,
        html,
    )


def set_js_default_months(html: str, months: int) -> str:
    months = int(months) if int(months) in (1, 3, 6, 12) else 1
    return re.sub(
        r"let months\s*=\s*\d+",
        f"let months={months}",
        html,
        count=1,
    )


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
    payload = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "sessions": sessions,
        "trend": compute_trend(sessions),
    }
    if db_note:
        payload["index_note"] = db_note
    embed = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = load_template()
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
    if args.stats:
        print("tasks:", ", ".join(f"{k}={v}" for k, v in tasks.most_common()))

    if args.open:
        open_browser(out_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
