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


def classify(text: str, tools: int = 0) -> str:
    """Rough task bucket for the report bars (not ground truth)."""
    t = text or ""
    pl = t.lower()
    if any(k in t for k in ("写", "文章", "文案", "标题", "公众号", "小红书", "润色", "改稿")):
        return "写东西"
    if any(k in t for k in ("查", "搜索", "资料", "研究", "了解")) or "search" in pl:
        return "查资料"
    if any(k in t for k in ("读", "阅读", "划线", "笔记", "这本书")):
        return "阅读探索"
    if any(k in t for k in ("代码", "bug", "修复", "实现", "函数", "重构", "报错")) or tools >= 3:
        return "写代码"
    if tools == 0:
        return "闲聊/纯对话"
    return "综合协作"


def topic_phrases(summary: str) -> list:
    if not summary:
        return []
    s = summary.strip()
    if len(s) < 4 or SKIP_TOPIC.match(s):
        return []
    if len(s) <= 40 and not s.startswith("{"):
        s2 = re.sub(r"\s+", " ", s).strip("「」\"'“”")
        if s2 and not SKIP_TOPIC.match(s2):
            return [s2[:40]]
    out = []
    for w in re.findall(r"[\u4e00-\u9fff]{2,12}|[A-Za-z][A-Za-z0-9_-]{2,20}", s):
        if w.lower() in STOP or w in STOP:
            continue
        out.append(w)
        if len(out) >= 3:
            break
    return out


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
        "duration_seconds", "project_name",
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
               duration_seconds, project_name
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
                "errors": int(r["errors"] or 0),
                "tokens": int(r["total_tokens"] or 0),
                "project": (r["project_name"] or "")[:60],
                "summary": summary[:160],
                "task": classify(summary, tools),
                "topics": topic_phrases(summary),
            }
        )
    con.close()
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


def build_html(sessions: list[dict], months: int, db_note: str = "") -> str:
    payload = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "sessions": sessions,
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
