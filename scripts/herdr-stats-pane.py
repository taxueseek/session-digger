#!/usr/bin/env python3
"""
herdr-stats-pane.py — Herdr stats overlay for session-digger.

Read-only dashboard. Paths shown redacted (no username).
Data dir: SESSION_DIGGER_DATA_DIR or ~/.claude/.session-digger/index.db

Env (Herdr): HERDR_PLUGIN_ROOT, HERDR_PLUGIN_ID, HERDR_ENV
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

PLUGIN_ROOT = Path(
    os.environ.get("HERDR_PLUGIN_ROOT")
    or os.environ.get("SESSION_DIGGER_ROOT")
    or Path(__file__).resolve().parent.parent
)
SCRIPT_DIR = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


def _data_dir() -> Path:
    env = os.environ.get("SESSION_DIGGER_DATA_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".claude" / ".session-digger"


def _db_path() -> Path:
    return _data_dir() / "index.db"


def _redact_display_path(p: Path) -> str:
    """Show index location without leaking username."""
    s = str(p)
    home = str(Path.home())
    name = Path.home().name
    if s.startswith(home):
        s = "~" + s[len(home) :]
    if name:
        s = s.replace(name, "<user>")
    s = re.sub(r"/Users/[^/]+", "/Users/<user>", s)
    s = re.sub(r"/home/[^/]+", "/home/<user>", s)
    return s


_COLORS = {
    "hdr": "\033[1;36m",
    "dim": "\033[2;37m",
    "val": "\033[1;33m",
    "ok": "\033[1;32m",
    "warn": "\033[1;35m",
    "rst": "\033[0m",
}


def c(name, text):
    if not sys.stdout.isatty():
        return text
    return f"{_COLORS.get(name, '')}{text}{_COLORS['rst']}"


def section(title):
    print(f"\n{c('hdr', '=' * 40)}")
    print(c("hdr", f"  {title}"))
    print(c("hdr", "=" * 40))


def stat(label, value, color="val"):
    print(f"  {c('dim', label.ljust(22))} {c(color, str(value))}")


def fallback_message():
    print(c("warn", "\n  会话索引尚未构建。"))
    print(c("dim", "  运行:\n"))
    print(c("dim", "    herdr plugin action taxueseek.session-digger:sd-reindex"))
    print(c("dim", "  或"))
    print(c("dim", f"    python3 {PLUGIN_ROOT / 'scripts' / 'index-builder.py'} build --agent cross\n"))


def render():
    db = _db_path()
    print(c("hdr", "Session Digger — 会话知识面板"))
    print(c("dim", f"  索引: {_redact_display_path(db)}"))
    print(c("dim", f"  插件: {os.environ.get('HERDR_PLUGIN_ID', 'standalone')}"))
    print(c("dim", f"  版本: herdr-surface (plugin root resolved)"))

    if not db.exists():
        fallback_message()
        return

    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
    except Exception as e:
        print(c("warn", f"\n  无法打开索引数据库: {type(e).__name__}"))
        return

    section("总览")
    n_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    n_msgs = conn.execute("SELECT SUM(message_count) FROM sessions").fetchone()[0] or 0
    n_tools = conn.execute("SELECT SUM(tool_calls) FROM sessions").fetchone()[0] or 0
    n_errors = conn.execute("SELECT SUM(errors) FROM sessions").fetchone()[0] or 0

    stat("总会话数", n_sessions)
    stat("总消息数", f"{n_msgs:,}")
    stat("工具调用", f"{n_tools:,}")
    stat("工具错误", n_errors, "warn" if n_errors > 0 else "ok")
    if n_tools > 0:
        err_rate = n_errors / n_tools * 100
        stat("错误率", f"{err_rate:.1f}%", "warn" if err_rate > 10 else "ok")

    section("按环境分布")
    rows = conn.execute(
        "SELECT agent, COUNT(*) as cnt FROM sessions GROUP BY agent ORDER BY cnt DESC"
    ).fetchall()
    for row in rows:
        bar = "#" * min(int(row["cnt"]), 30)
        stat(row["agent"] or "?", f"{row['cnt']:>4}  {bar}")

    section("最近 5 个会话")
    recent = conn.execute(
        "SELECT id, agent, created, message_count FROM sessions "
        "ORDER BY created DESC LIMIT 5"
    ).fetchall()
    if recent:
        for r in recent:
            sid = (r["id"] or "")[:28]
            created = (r["created"] or "?")[:10]
            stat(sid, f"{(r['agent'] or '?'):>8} · {r['message_count'] or 0:>3} msgs · {created}")
    else:
        print(c("dim", "  （无会话记录）"))

    section("高频工具 TOP 5")
    tool_agg = {}
    for row in conn.execute(
        "SELECT tool_usage_json FROM sessions WHERE tool_usage_json IS NOT NULL"
    ).fetchall():
        try:
            usage = json.loads(row["tool_usage_json"] or "{}")
            for k, v in usage.items():
                tool_agg[k] = tool_agg.get(k, 0) + (v if isinstance(v, int) else 0)
        except (json.JSONDecodeError, TypeError):
            continue
    top = sorted(tool_agg.items(), key=lambda x: -x[1])[:5]
    if top:
        for tool_name, count in top:
            stat(tool_name, count)
    else:
        print(c("dim", "  （暂无工具记录）"))

    conn.close()

    section("快捷操作")
    print(c("dim", "  搜索:   herdr plugin action taxueseek.session-digger:sd-search"))
    print(c("dim", "  模糊:   herdr plugin action taxueseek.session-digger:sd-fuzzy-search"))
    print(c("dim", "  趋势:   herdr plugin action taxueseek.session-digger:sd-trend"))
    print(c("dim", "  差距:   herdr plugin action taxueseek.session-digger:sd-skill-gap"))
    print(c("dim", "  自检:   herdr plugin action taxueseek.session-digger:sd-skill-health"))
    print(c("dim", "  重建:   herdr plugin action taxueseek.session-digger:sd-reindex"))
    print()


if __name__ == "__main__":
    render()
