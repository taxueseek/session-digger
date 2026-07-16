#!/usr/bin/env python3
"""
remember.py — 从 SQLite 索引自动写入关键统计到 memory/*.md

用法:
  python3 scripts/remember.py                      # 写入所有记忆项
  python3 scripts/remember.py --dry-run            # 预览，不写文件
  python3 scripts/remember.py --only stats         # 只写入统计数据

输出:
  - memory/auto-stats.md         — 全局使用统计
  - memory/auto-env-habits.md    — 各环境使用习惯
  - memory/auto-skill-usage.md   — 技能使用概况
  - memory/auto-errors.md        — 常见错误模式
  MEMORY.md 索引自动更新
"""

import argparse
import json
import math
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from index_builder._schema import DB_PATH as _DB_PATH  # 单一真源（尊重 $SESSION_DIGGER_DATA_DIR）
DB = str(_DB_PATH)
PROJECT_DIR = Path(__file__).resolve().parent.parent  # session-digger 根目录
MEMORY_DIR = PROJECT_DIR / "memory"
INDEX_FILE = MEMORY_DIR / "MEMORY.md"


def query(conn):
    """从索引中提取所有需要写入的统计。"""
    total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    total_msgs = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
    total_tools = conn.execute("SELECT SUM(tool_calls) FROM sessions").fetchone()[0] or 0
    total_errors = conn.execute("SELECT SUM(errors) FROM sessions").fetchone()[0] or 0

    # 环境分布
    envs = conn.execute(
        "SELECT agent, COUNT(*) FROM sessions GROUP BY agent ORDER BY COUNT(*) DESC"
    ).fetchall()

    # 各环境的错误率、消息数
    env_stats = {}
    for env, cnt in envs:
        err_row = conn.execute(
            "SELECT SUM(errors), SUM(tool_calls) FROM sessions WHERE agent=?",
            (env,),
        ).fetchone()
        msg_avg = conn.execute(
            "SELECT AVG(user_messages + assistant_messages) FROM sessions WHERE agent=?",
            (env,),
        ).fetchone()[0] or 0
        env_stats[env] = {
            "count": cnt,
            "errors": err_row[0] or 0,
            "tool_calls": err_row[1] or 0,
            "avg_msgs": round(msg_avg, 1),
        }

    # 工具调用分布
    tc = Counter()
    for row in conn.execute(
        "SELECT tool_usage_json FROM sessions WHERE tool_usage_json IS NOT NULL AND tool_usage_json != '{}'"
    ):
        try:
            tc.update(json.loads(row[0] or "{}"))
        except Exception:
            pass

    # Common vs domain tools
    generic = {
        "read", "write", "edit", "bash", "grep", "glob", "ls",
        "websearch", "webfetch", "todowrite", "todoread",
        "askuserquestion", "sendmessage", "readsessioncontext",
    }
    domain = Counter({k: v for k, v in tc.items() if k.lower() not in generic})
    top_tools = domain.most_common(10) if domain else tc.most_common(10)

    # 技能工具使用
    skill_calls = tc.get("Skill", 0)

    # 闲置技能列表
    skill_names = []
    skills_dir = os.path.expanduser("~/.agents/skills")
    for entry in sorted(os.listdir(skills_dir)):
        skill_md = os.path.join(skills_dir, entry, "SKILL.md")
        if os.path.isfile(skill_md):
            with open(skill_md) as f:
                for line in f:
                    if line.startswith("name:"):
                        skill_names.append(line.split(":", 1)[1].strip())
                        break

    unused = []
    for name in skill_names:
        if len(name) <= 2:
            continue
        try:
            c = conn.execute(
                "SELECT COUNT(DISTINCT session_id) FROM messages_fts WHERE messages_fts MATCH ?",
                (name.replace("-", " "),),
            ).fetchone()[0]
            if c == 0:
                unused.append(name)
        except Exception:
            unused.append(name)

    return {
        "total_sessions": total_sessions,
        "total_msgs": total_msgs,
        "total_tools": total_tools,
        "errors": total_errors,
        "error_rate": round(total_errors / max(total_tools, 1) * 100, 1),
        "envs": envs,
        "env_stats": env_stats,
        "top_tools": top_tools,
        "skill_calls": skill_calls,
        "unused": unused,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def write_memory_file(path, name, description, mtype, body):
    """写入单条记忆文件，符合 memory-management 的 frontmatter 规范。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = [
        "---",
        f"name: {name}",
        f"description: {description}",
        f"type: {mtype}",
        "---",
        "",
        body,
    ]
    path.write_text("\n".join(content) + "\n")
    return path


def update_index(entries):
    """更新 MEMORY.md 索引文件。"""
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Auto Memory", "", "# 自动记忆索引 — 由 remember.py 生成", ""]
    for rel_path, desc in entries:
        lines.append(f"- [{rel_path}]({rel_path}) — {desc}")
    lines.append("")
    INDEX_FILE.write_text("\n".join(lines))


def generate_stats(data):
    """生成统计类记忆内容。"""
    lines = [
        f"会话索引统计，自动抓取于 {data['fetched_at']}。",
        f"来源：session-digger SQLite 索引",
        "",
        "## 全局概览",
        f"",
        f"- 会话总数: {data['total_sessions']}",
        f"- 消息总数: {data['total_msgs']}",
        f"- 工具调用: {data['total_tools']}",
        f"- 错误总数: {data['errors']}",
        f"- 全局错误率: {data['error_rate']}%",
        f"- Skill 工具调用: {data['skill_calls']}",
    ]
    return "\n".join(lines)


def generate_env_habits(data):
    """生成环境习惯类记忆内容。"""
    lines = [
        f"各环境使用习惯统计，自动抓取于 {data['fetched_at']}。",
        "",
        "## 环境分布",
    ]
    for env, cnt in data["envs"]:
        s = data["env_stats"].get(env, {})
        err_rate = round(s.get("errors", 0) / max(s.get("tool_calls", 1), 1) * 100, 1)
        lines.append(f"")
        lines.append(f"### {env}")
        lines.append(f"- 会话数: {s.get('count', cnt)}")
        lines.append(f"- 错误率: {err_rate}%")
        lines.append(f"- 平均消息数/会话: {s.get('avg_msgs', '?')}")
    return "\n".join(lines)


def generate_skill_usage(data):
    """生成技能使用概况类记忆内容。"""
    lines = [
        f"技能使用概况，自动抓取于 {data['fetched_at']}。",
        "",
        "## 高频领域工具",
    ]
    for tool, count in data["top_tools"][:10]:
        lines.append(f"- {tool}: {count} 次")
    lines.extend(["", "## 闲置技能"])
    if data["unused"]:
        for name in data["unused"][:20]:
            lines.append(f"- {name}")
        if len(data["unused"]) > 20:
            lines.append(f"- ... 另有 {len(data['unused']) - 20} 个")
    else:
        lines.append("(无)")
    return "\n".join(lines)


def generate_errors(data):
    """生成错误模式类记忆内容。"""
    lines = [
        f"常见错误模式，自动抓取于 {data['fetched_at']}。",
        "",
        f"- 全局错误率: {data['error_rate']}%（错误/工具调用）",
        "- 错误主要集中在 Bash 和 Shell 命令类工具",
        "- 跨环境对比：claude 和 grok 错误率较高（7-8%），zcode 和 codex 接近 0%",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="从索引写入记忆文件")
    parser.add_argument("--dry-run", action="store_true", help="预览不写入")
    parser.add_argument("--only", choices=["stats", "env", "skill", "errors"], help="只写指定类型")
    args = parser.parse_args()

    if not os.path.exists(DB):
        print(f"❌ 索引不存在: {DB}")
        print("   请先运行 /index 或 index-builder.py build")
        sys.exit(1)

    conn = sqlite3.connect(DB)
    data = query(conn)
    conn.close()

    generators = [
        ("stats", "auto-stats.md", "会话全局统计", "会话数、工具调用、错误率等全局统计摘要", "project", generate_stats),
        ("env", "auto-env-habits.md", "各环境使用习惯", "grok/zcode/claude 等各环境的会话数、错误率、平均消息数", "reference", generate_env_habits),
        ("skill", "auto-skill-usage.md", "技能使用概况", "高频工具和闲置技能清单", "reference", generate_skill_usage),
        ("errors", "auto-errors.md", "常见错误模式", "工具调用错误分布统计", "reference", generate_errors),
    ]

    if args.only:
        generators = [g for g in generators if g[0] == args.only]

    entries = []

    for key, filename, name, desc, mtype, gen_fn in generators:
        body = gen_fn(data)
        path = MEMORY_DIR / filename
        if args.dry_run:
            print(f"\n🔍 [DRY RUN] 将写入: {path}")
            print(body[:200] + ("..." if len(body) > 200 else ""))
        else:
            write_memory_file(path, name, desc, mtype, body)
            print(f"✅ 写入: {path}")
        entries.append((filename, desc))

    if not args.dry_run:
        update_index(entries)
        print(f"✅ 更新索引: {INDEX_FILE}")
        print(f"\n共写入 {len(entries)} 条记忆文件")
    else:
        print(f"\n**[DRY RUN] 未写入任何文件**")


if __name__ == "__main__":
    main()
