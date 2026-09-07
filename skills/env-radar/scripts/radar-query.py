#!/usr/bin/env python3
"""
radar-query.py — env-radar 查询接口（入口）。

读取 radar-build.py 生成的 SQLite 数据库，输出分析报告。

用法:
    python3 radar-query.py <db_path> <command> [options]

命令:
    overview    项目总览
    structure   结构报告
    conventions 约定报告
    health      健康报告
    evolution   演化报告
    search      全文搜索
    diff        版本对比
    export      导出完整报告
"""
import argparse
import sqlite3
import sys

from radar_commands import (
    connect_db,
    cmd_conventions,
    cmd_diff,
    cmd_evolution,
    cmd_export,
    cmd_health,
    cmd_overview,
    cmd_search,
    cmd_structure,
)
from radar_report import OutputFormatter


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="radar-query",
        description="env-radar 查询接口 — 读取 SQLite 数据库输出分析报告",
    )
    parser.add_argument("db", help="数据库文件路径")
    parser.add_argument("command", choices=[
        "overview", "structure", "conventions", "health",
        "evolution", "search", "diff", "export",
    ], help="查询命令")
    parser.add_argument("--format", choices=["markdown", "json", "html"],
                        default="markdown", help="输出格式 (默认 markdown)")
    parser.add_argument("--output", "-o", help="输出文件路径 (默认 stdout)")
    parser.add_argument("--limit", type=int, default=50, help="结果数量限制 (默认 50)")
    parser.add_argument("--offset", type=int, default=0, help="结果偏移量 (默认 0)")

    # structure 子参数
    parser.add_argument("--depth", type=int, default=3, help="目录树深度 (默认 3)")
    parser.add_argument("--struct-format", dest="struct_format", choices=["tree", "list"],
                        default="tree", help="结构输出格式 (默认 tree)")

    # conventions 子参数
    parser.add_argument("--category", choices=["naming", "style", "architecture", "all"],
                        default="all", help="约定类别过滤")

    # evolution 子参数
    parser.add_argument("--top", type=int, default=20, help="热点文件数量 (默认 20)")
    parser.add_argument("--period", choices=["week", "month"], default="week",
                        help="统计周期 (默认 week)")

    # search 子参数
    parser.add_argument("--query", dest="query", help="搜索关键词")
    parser.add_argument("--type", dest="search_type",
                        choices=["file", "module", "dep", "debt", "all"],
                        default="file", help="搜索类型 (默认 file)")

    # diff 子参数
    parser.add_argument("--other-db", dest="other_db", help="对比数据库路径")

    return parser


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # 连接数据库
    conn = connect_db(args.db)

    # 构建格式化器
    fmt = OutputFormatter(fmt=args.format)

    # 路由到对应命令
    command_map = {
        "overview": cmd_overview,
        "structure": cmd_structure,
        "conventions": cmd_conventions,
        "health": cmd_health,
        "evolution": cmd_evolution,
        "search": cmd_search,
        "diff": cmd_diff,
        "export": cmd_export,
    }

    handler = command_map[args.command]
    try:
        result = handler(conn, fmt, args)
        fmt.output(result, args.output)
    except sqlite3.Error as e:
        print(f"数据库查询错误: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
