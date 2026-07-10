#!/usr/bin/env python3
"""
herdr-stats-pane.py — Herdr stats pane for session-digger.

Runs as a Herdr overlay pane. Outputs a self-contained dashboard
(snapshot of session-digger statistics). Designed to be lazily
refreshed by Herdr (re-run on pane open or on demand).

No write side effects — pure read-only CLI.

Environment:
  HERDR_SOCKET_PATH    — Herdr IPC (unused, reserved)
  HERDR_BIN_PATH       — Herdr binary path (unused, reserved)
  HERDR_ENV            — "1" when running under Herdr
  HERDR_PLUGIN_ID      — plugin id (taxueseek.session-digger)
  HERDR_PLUGIN_ROOT    — absolute path to this plugin's directory

Usage:
  python3 herdr-stats-pane.py
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path.home() / ".claude" / ".session-digger" / "index.db"
PLUGIN_ROOT = Path(os.environ.get("HERDR_PLUGIN_ROOT", Path(__file__).resolve().parent.parent))
SCRIPT_DIR = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

_COLORS = {
    "hdr": "\033[1;36m",   # cyan bold
    "dim": "\033[2;37m",   # gray dim
    "val": "\033[1;33m",   # yellow bold
    "ok":  "\033[1;32m",   # green bold
    "warn": "\033[1;35m",  # magenta
    "rst": "\033[0m",
}

def c(name, text):
    """Colorize if terminal supports it."""
    if not sys.stdout.isatty():
        return text
    return f"{_COLORS.get(name, '')}{text}{_COLORS['rst']}"

def section(title):
    print(f"\n{c('hdr', '━' * 40)}")
    print(c('hdr', f"  {title}"))
    print(c('hdr', '━' * 40))

def stat(label, value, color="val"):
    print(f"  {c('dim', label.ljust(22))} {c(color, str(value))}")

def fallback_message():
    """Index not built yet — guide user to build it."""
    print(c("warn", "\n  ⚠  会话索引尚未构建。"))
    print(c("dim", "  请在终端运行以下命令：\n"))
    print(c("dim", "    herdr plugin action taxueseek.session-digger:sd-reindex"))
    print(c("dim", "  # 或直接"))
    print(c("dim", "    python3 scripts/index-builder.py --agent cross\n"))

def render():
    print(c("hdr", "⛏  Session Digger — 会话知识面板"))
    print(c("dim", f"  索引: {DB_PATH}"))
    print(c("dim", f"  插件: {os.environ.get('HERDR_PLUGIN_ID', 'standalone')}"))

    if not DB_PATH.exists():
        fallback_message()
        return

    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
    except Exception as e:
        print(c("warn", f"\n  ⚠  无法打开索引数据库: {e}"))
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
        bar = "█" * min(row["cnt"], 30)
        stat(row["agent"], f"{row['cnt']:>4}  {bar}")

    section("最近 5 个会话")
    recent = conn.execute(
        "SELECT id, agent, created, message_count FROM sessions "
        "ORDER BY created DESC LIMIT 5"
    ).fetchall()
    if recent:
        for r in recent:
            stat(
                r["id"][:30],
                f"{r['agent']:>8} · {r['message_count']:>3} msgs · {r['created'][:10] if r['created'] else '?'}"
            )
    else:
        print(c("dim", "  （无会话记录）"))

    section("高频工具 TOP 5")
    # tool_usage_json is a JSON dict stored per session; aggregate in Python
    tool_agg = {}
    for row in conn.execute("SELECT tool_usage_json FROM sessions WHERE tool_usage_json IS NOT NULL").fetchall():
        try:
            usage = json.loads(row["tool_usage_json"])
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
    print(c("dim", "  趋势:   herdr plugin action taxueseek.session-digger:sd-trend"))
    print(c("dim", "  Skill:  herdr plugin action taxueseek.session-digger:sd-skill-gap"))
    print(c("dim", "  重建:   herdr plugin action taxueseek.session-digger:sd-reindex"))
    print()

if __name__ == "__main__":
    render()
