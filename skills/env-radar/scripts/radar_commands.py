#!/usr/bin/env python3
"""radar_commands.py — env-radar 查询命令实现。

每个命令从 SQLite 数据库读取数据，组装 sections 后交给
radar_report.OutputFormatter 渲染输出。
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from typing import Any

from radar_report import OutputFormatter


# ── 数据库连接 ────────────────────────────────────────────────────────────────

def connect_db(db_path: str) -> sqlite3.Connection:
    """连接数据库，检查文件存在性和基本 schema。"""
    if not os.path.exists(db_path):
        print(f"错误: 数据库文件不存在: {db_path}", file=sys.stderr)
        sys.exit(1)

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # 启用 WAL 模式提升并发读取性能
        conn.execute("PRAGMA journal_mode=WAL")
        # 检查必需的表是否存在
        _validate_schema(conn)
        return conn
    except sqlite3.DatabaseError as e:
        print(f"错误: 数据库损坏或格式不兼容: {e}", file=sys.stderr)
        sys.exit(1)


def _validate_schema(conn: sqlite3.Connection) -> None:
    """验证数据库包含所有必需的表。"""
    required_tables = {
        "project_meta", "files", "modules", "dependencies",
        "conventions", "evolution", "tech_debt", "commit_stats"
    }
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    existing = {row["name"] for row in cursor.fetchall()}
    missing = required_tables - existing
    if missing:
        print(
            f"错误: 数据库缺少必需的表: {', '.join(missing)}\n"
            f"请确认这是由 radar-build.py 生成的数据库。",
            file=sys.stderr,
        )
        sys.exit(1)


# ── 辅助函数 ──────────────────────────────────────────────────────────────────

def _parse_json_list(raw: str | None) -> list[str]:
    """解析 JSON 数组字符串；兼容旧格式（逗号分隔的纯文本）。"""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    except (json.JSONDecodeError, TypeError):
        pass
    return [item.strip() for item in raw.split(",") if item.strip()]


# ── 命令实现 ──────────────────────────────────────────────────────────────────

def cmd_overview(conn: sqlite3.Connection, fmt: OutputFormatter, args: argparse.Namespace) -> str:
    """项目总览命令。"""
    meta = conn.execute("SELECT * FROM project_meta LIMIT 1").fetchone()
    if not meta:
        return fmt.render_report(
            [{"title": "错误", "text": "project_meta 表中无数据"}],
            "env-radar 项目总览",
        )

    # 统计主要语言
    lang_rows = conn.execute(
        "SELECT extension, COUNT(*) as cnt FROM files GROUP BY extension ORDER BY cnt DESC LIMIT 5"
    ).fetchall()
    main_languages = ", ".join(
        f"{row['extension'] or '(无扩展名)'} ({row['cnt']})" for row in lang_rows
    )

    # 依赖统计
    dep_count = conn.execute("SELECT COUNT(*) as c FROM dependencies").fetchone()["c"]
    outdated_count = conn.execute(
        "SELECT COUNT(*) as c FROM dependencies WHERE is_outdated=1"
    ).fetchone()["c"]

    # 模块数
    module_count = conn.execute("SELECT COUNT(*) as c FROM modules").fetchone()["c"]

    # 扫描时间格式化
    scanned_at = ""
    if meta["scanned_at"]:
        scanned_at = datetime.fromtimestamp(meta["scanned_at"]).strftime("%Y-%m-%d %H:%M:%S")

    sections = [
        {
            "title": "基本信息",
            "table": {
                "headers": ["属性", "值"],
                "rows": [
                    ["项目名", meta["name"] or "-"],
                    ["根路径", meta["root_path"] or "-"],
                    ["扫描时间", scanned_at or "-"],
                    ["Git 分支", meta["git_branch"] or "-"],
                    ["Git Remote", meta["git_remote"] or "-"],
                ],
            },
        },
        {
            "title": "规模统计",
            "table": {
                "headers": ["指标", "数值"],
                "rows": [
                    ["文件总数", meta["total_files"] or 0],
                    ["代码总行数", meta["total_lines"] or 0],
                    ["模块数", module_count],
                    ["依赖数", dep_count],
                    ["过期依赖", outdated_count],
                    ["主要语言", main_languages or "-"],
                ],
            },
        },
    ]
    return fmt.render_report(sections, f"env-radar 项目总览 — {meta['name'] or '未命名'}")


def cmd_structure(conn: sqlite3.Connection, fmt: OutputFormatter, args: argparse.Namespace) -> str:
    """结构报告命令。"""
    depth = getattr(args, "depth", 3)
    struct_format = getattr(args, "struct_format", "tree")

    # 获取目录树
    rows = conn.execute(
        "SELECT rel_path, depth, parent_dir, is_entry FROM files ORDER BY rel_path"
    ).fetchall()

    # 按深度过滤
    filtered = [r for r in rows if r["depth"] <= depth]

    # 入口文件
    entries = [r["rel_path"] for r in rows if r["is_entry"]]

    # 模块边界
    modules = conn.execute(
        "SELECT name, path, file_count, main_language FROM modules ORDER BY name"
    ).fetchall()

    # 文件类型分布
    ext_stats = conn.execute(
        "SELECT extension, COUNT(*) as cnt, SUM(lines) as total_lines "
        "FROM files GROUP BY extension ORDER BY cnt DESC"
    ).fetchall()

    sections = []

    # 目录树或列表
    if struct_format == "tree":
        tree_text = _build_tree(filtered)
        sections.append({"title": f"目录树 (深度 ≤ {depth})", "tree": tree_text})
    else:
        file_list = [r["rel_path"] for r in filtered]
        sections.append({"title": f"文件列表 (深度 ≤ {depth})", "list": file_list})

    # 入口文件
    if entries:
        sections.append({"title": "入口文件", "list": entries})
    else:
        sections.append({"title": "入口文件", "text": "未检测到入口文件"})

    # 模块边界
    if modules:
        sections.append({
            "title": "模块边界",
            "table": {
                "headers": ["模块名", "路径", "文件数", "主语言"],
                "rows": [[m["name"], m["path"], m["file_count"], m["main_language"] or "-"]
                         for m in modules],
            },
        })

    # 文件类型分布
    sections.append({
        "title": "文件类型分布",
        "table": {
            "headers": ["扩展名", "文件数", "总行数"],
            "rows": [[r["extension"] or "(无)", r["cnt"], r["total_lines"] or 0]
                     for r in ext_stats],
        },
    })

    return fmt.render_report(sections, "env-radar 结构报告")


def _build_tree(rows: list[sqlite3.Row]) -> str:
    """将文件列表转为树形文本。"""
    # 使用字典构建树结构
    tree: dict = {}
    for row in rows:
        parts = row["rel_path"].split("/")
        node = tree
        for part in parts:
            node = node.setdefault(part, {})

    # 递归渲染
    lines: list[str] = []

    def _render(node: dict, prefix: str = "", is_last: bool = True) -> None:
        items = list(node.items())
        for i, (name, children) in enumerate(items):
            last = i == len(items) - 1
            connector = "└── " if last else "├── "
            lines.append(f"{prefix}{connector}{name}")
            if children:
                branch = "    " if last else "│   "
                _render(children, prefix + branch, last)

    _render(tree)
    return "\n".join(lines)


def cmd_conventions(conn: sqlite3.Connection, fmt: OutputFormatter, args: argparse.Namespace) -> str:
    """约定报告命令。"""
    category = getattr(args, "category", "all")

    # 构建查询条件
    conditions = []
    params: list[Any] = []
    if category != "all":
        conditions.append("category = ?")
        params.append(category)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    convs = conn.execute(
        f"SELECT category, name, value, count, sample_files FROM conventions {where} "
        f"ORDER BY category, count DESC",
        params,
    ).fetchall()

    if not convs:
        return fmt.render_report(
            [{"title": "约定统计", "text": "未检测到约定数据"}],
            "env-radar 约定报告",
        )

    # 按类别分组
    grouped: dict[str, list[sqlite3.Row]] = {}
    for c in convs:
        grouped.setdefault(c["category"], []).append(c)

    sections = []
    for cat, items in grouped.items():
        sections.append({
            "title": f"类别: {cat}",
            "table": {
                "headers": ["名称", "值", "出现次数", "示例文件"],
                "rows": [
                    [i["name"], i["value"] or "-", i["count"],
                     ", ".join(_parse_json_list(i["sample_files"])[:3]) or "-"]
                    for i in items
                ],
            },
        })

    # 注释密度
    comment_stats = conn.execute(
        "SELECT name, value, count FROM conventions WHERE category='comments'"
    ).fetchall()
    if comment_stats:
        sections.append({
            "title": "注释密度",
            "table": {
                "headers": ["指标", "值", "计数"],
                "rows": [[s["name"], s["value"], s["count"]] for s in comment_stats],
            },
        })

    return fmt.render_report(sections, "env-radar 约定报告")


def cmd_health(conn: sqlite3.Connection, fmt: OutputFormatter, args: argparse.Namespace) -> str:
    """健康报告命令。"""
    sections = []

    # 过期依赖
    outdated = conn.execute(
        "SELECT name, version, latest_version, dep_type FROM dependencies "
        "WHERE is_outdated=1 ORDER BY name"
    ).fetchall()
    if outdated:
        sections.append({
            "title": "过期依赖",
            "table": {
                "headers": ["依赖名", "当前版本", "最新版本", "类型"],
                "rows": [[d["name"], d["version"], d["latest_version"] or "未知", d["dep_type"] or "-"]
                         for d in outdated],
            },
        })
    else:
        sections.append({"title": "过期依赖", "text": "所有依赖均为最新版本"})

    # 依赖统计
    dep_stats = conn.execute(
        "SELECT dep_type, COUNT(*) as cnt, "
        "SUM(CASE WHEN is_outdated=1 THEN 1 ELSE 0 END) as outdated "
        "FROM dependencies GROUP BY dep_type"
    ).fetchall()
    sections.append({
        "title": "依赖统计",
        "table": {
            "headers": ["类型", "总数", "过期数"],
            "rows": [[d["dep_type"] or "未知", d["cnt"], d["outdated"]] for d in dep_stats],
        },
    })

    # 技术债
    debt_count = conn.execute("SELECT COUNT(*) as c FROM tech_debt").fetchone()["c"]
    debt_by_tag = conn.execute(
        "SELECT tag, COUNT(*) as cnt FROM tech_debt GROUP BY tag ORDER BY cnt DESC"
    ).fetchall()
    if debt_by_tag:
        sections.append({
            "title": f"技术债统计 (总计 {debt_count})",
            "table": {
                "headers": ["标签", "数量"],
                "rows": [[d["tag"], d["cnt"]] for d in debt_by_tag],
            },
        })

    # 安全风险标记（从 tech_debt 中筛选安全相关标签）
    security_tags = ("security", "vulnerability", "CVE", "injection", "xss")
    security_debt = conn.execute(
        "SELECT tag, content FROM tech_debt WHERE "
        + " OR ".join("tag LIKE ?" for _ in security_tags),
        [f"%{t}%" for t in security_tags],
    ).fetchall()
    if security_debt:
        sections.append({
            "title": "安全风险标记",
            "table": {
                "headers": ["标签", "内容"],
                "rows": [[d["tag"], (d["content"] or "")[:80]] for d in security_debt],
            },
        })

    # 缺失配置检测
    missing_configs = _detect_missing_configs(conn)
    if missing_configs:
        sections.append({"title": "缺失配置", "list": missing_configs})

    return fmt.render_report(sections, "env-radar 健康报告")


def _detect_missing_configs(conn: sqlite3.Connection) -> list[str]:
    """检测常见缺失配置文件。"""
    common_configs = [
        ".gitignore", ".env.example", "README.md", "LICENSE",
        "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
        ".editorconfig", "tsconfig.json", "Makefile",
    ]
    existing = {r["rel_path"].split("/")[-1] for r in conn.execute(
        "SELECT rel_path FROM files"
    ).fetchall()}
    return [f for f in common_configs if f not in existing]


def cmd_evolution(conn: sqlite3.Connection, fmt: OutputFormatter, args: argparse.Namespace) -> str:
    """演化报告命令。"""
    top = getattr(args, "top", 20)
    period = getattr(args, "period", "week")

    sections = []

    # 热点文件 Top N
    hot_files = conn.execute(
        "SELECT f.rel_path, e.commit_count, e.last_modified, e.author_count "
        "FROM evolution e JOIN files f ON e.file_id = f.id "
        "ORDER BY e.commit_count DESC LIMIT ?",
        (top,),
    ).fetchall()
    if hot_files:
        sections.append({
            "title": f"热点文件 Top {top}",
            "table": {
                "headers": ["文件路径", "提交次数", "最近修改", "作者数"],
                "rows": [[h["rel_path"], h["commit_count"], h["last_modified"] or "-", h["author_count"]]
                         for h in hot_files],
            },
        })

    # 提交频率趋势
    commit_trend = conn.execute(
        "SELECT period_start, commit_count, files_changed "
        "FROM commit_stats WHERE period = ? ORDER BY period_start",
        (period,),
    ).fetchall()
    if commit_trend:
        sections.append({
            "title": f"提交频率趋势 (按 {period})",
            "table": {
                "headers": ["时间段", "提交数", "变更文件数"],
                "rows": [[c["period_start"], c["commit_count"], c["files_changed"]]
                         for c in commit_trend],
            },
        })

    # 作者分布
    authors_data = conn.execute(
        "SELECT authors FROM commit_stats WHERE authors IS NOT NULL"
    ).fetchall()
    author_counts: dict[str, int] = {}
    for row in authors_data:
        for author in _parse_json_list(row["authors"]):
            if author:
                author_counts[author] = author_counts.get(author, 0) + 1
    if author_counts:
        sorted_authors = sorted(author_counts.items(), key=lambda x: -x[1])[:15]
        sections.append({
            "title": "作者分布",
            "table": {
                "headers": ["作者", "提交次数"],
                "rows": [[a, c] for a, c in sorted_authors],
            },
        })

    # 技术债统计
    debt_stats = conn.execute(
        "SELECT tag, COUNT(*) as cnt FROM tech_debt GROUP BY tag ORDER BY cnt DESC"
    ).fetchall()
    if debt_stats:
        sections.append({
            "title": "技术债统计",
            "table": {
                "headers": ["标签", "数量"],
                "rows": [[d["tag"], d["cnt"]] for d in debt_stats],
            },
        })

    return fmt.render_report(sections, "env-radar 演化报告")


def cmd_search(conn: sqlite3.Connection, fmt: OutputFormatter, args: argparse.Namespace) -> str:
    """全文搜索命令。"""
    query = getattr(args, "query", "")
    search_type = getattr(args, "search_type", "file")
    limit = getattr(args, "limit", 50)
    offset = getattr(args, "offset", 0)

    if not query:
        return fmt.render_report(
            [{"title": "错误", "text": "请提供搜索关键词"}], "env-radar 搜索"
        )

    sections = []
    pattern = f"%{query}%"

    if search_type in ("file", "all"):
        files = conn.execute(
            "SELECT rel_path, extension, lines, module_name FROM files "
            "WHERE rel_path LIKE ? OR module_name LIKE ? "
            "ORDER BY rel_path LIMIT ? OFFSET ?",
            (pattern, pattern, limit, offset),
        ).fetchall()
        if files:
            sections.append({
                "title": f"文件匹配 ({len(files)} 个)",
                "table": {
                    "headers": ["路径", "扩展名", "行数", "模块"],
                    "rows": [[f["rel_path"], f["extension"] or "-", f["lines"] or 0, f["module_name"] or "-"]
                             for f in files],
                },
            })

    if search_type in ("module", "all"):
        modules = conn.execute(
            "SELECT name, path, file_count, main_language FROM modules "
            "WHERE name LIKE ? OR path LIKE ? LIMIT ? OFFSET ?",
            (pattern, pattern, limit, offset),
        ).fetchall()
        if modules:
            sections.append({
                "title": f"模块匹配 ({len(modules)} 个)",
                "table": {
                    "headers": ["模块名", "路径", "文件数", "主语言"],
                    "rows": [[m["name"], m["path"], m["file_count"], m["main_language"] or "-"]
                             for m in modules],
                },
            })

    if search_type in ("dep", "all"):
        deps = conn.execute(
            "SELECT name, version, dep_type, is_outdated FROM dependencies "
            "WHERE name LIKE ? LIMIT ? OFFSET ?",
            (pattern, limit, offset),
        ).fetchall()
        if deps:
            sections.append({
                "title": f"依赖匹配 ({len(deps)} 个)",
                "table": {
                    "headers": ["依赖名", "版本", "类型", "是否过期"],
                    "rows": [[d["name"], d["version"], d["dep_type"] or "-", "是" if d["is_outdated"] else "否"]
                             for d in deps],
                },
            })

    if search_type in ("debt", "all"):
        debts = conn.execute(
            "SELECT t.tag, t.content, f.rel_path FROM tech_debt t "
            "JOIN files f ON t.file_id = f.id "
            "WHERE t.tag LIKE ? OR t.content LIKE ? LIMIT ? OFFSET ?",
            (pattern, pattern, limit, offset),
        ).fetchall()
        if debts:
            sections.append({
                "title": f"技术债匹配 ({len(debts)} 个)",
                "table": {
                    "headers": ["标签", "内容", "文件"],
                    "rows": [[d["tag"], (d["content"] or "")[:60], d["rel_path"]] for d in debts],
                },
            })

    if not sections:
        sections.append({"title": "搜索结果", "text": f"未找到与 '{query}' 匹配的内容"})

    return fmt.render_report(sections, f"env-radar 搜索 — {query}")


def cmd_diff(conn: sqlite3.Connection, fmt: OutputFormatter, args: argparse.Namespace) -> str:
    """版本对比命令。"""
    other_db_path = getattr(args, "other_db", None)
    if not other_db_path:
        return fmt.render_report(
            [{"title": "错误", "text": "请提供对比数据库路径"}], "env-radar 版本对比"
        )

    other_conn = connect_db(other_db_path)
    try:
        return _compute_diff(conn, other_conn, fmt)
    finally:
        other_conn.close()


def _compute_diff(
    conn1: sqlite3.Connection,
    conn2: sqlite3.Connection,
    fmt: OutputFormatter,
) -> str:
    """计算两个数据库之间的差异。"""
    sections = []

    # 文件变化
    files1 = {r["rel_path"]: r for r in conn1.execute("SELECT rel_path, lines, size_bytes FROM files").fetchall()}
    files2 = {r["rel_path"]: r for r in conn2.execute("SELECT rel_path, lines, size_bytes FROM files").fetchall()}

    added = set(files2.keys()) - set(files1.keys())
    removed = set(files1.keys()) - set(files2.keys())
    modified = []
    for path in set(files1.keys()) & set(files2.keys()):
        f1, f2 = files1[path], files2[path]
        if f1["lines"] != f2["lines"] or f1["size_bytes"] != f2["size_bytes"]:
            modified.append({
                "path": path,
                "old_lines": f1["lines"],
                "new_lines": f2["lines"],
                "old_size": f1["size_bytes"],
                "new_size": f2["size_bytes"],
            })

    if added:
        sections.append({"title": f"新增文件 ({len(added)})", "list": sorted(added)})
    if removed:
        sections.append({"title": f"删除文件 ({len(removed)})", "list": sorted(removed)})
    if modified:
        sections.append({
            "title": f"修改文件 ({len(modified)})",
            "table": {
                "headers": ["文件路径", "旧行数", "新行数", "旧大小", "新大小"],
                "rows": [[m["path"], m["old_lines"], m["new_lines"], m["old_size"], m["new_size"]]
                         for m in modified[:50]],
            },
        })

    # 依赖变化
    deps1 = {r["name"]: r for r in conn1.execute("SELECT name, version FROM dependencies").fetchall()}
    deps2 = {r["name"]: r for r in conn2.execute("SELECT name, version FROM dependencies").fetchall()}
    new_deps = set(deps2.keys()) - set(deps1.keys())
    removed_deps = set(deps1.keys()) - set(deps2.keys())
    upgraded = []
    for name in set(deps1.keys()) & set(deps2.keys()):
        if deps1[name]["version"] != deps2[name]["version"]:
            upgraded.append({
                "name": name,
                "old_ver": deps1[name]["version"],
                "new_ver": deps2[name]["version"],
            })
    if new_deps:
        sections.append({"title": f"新增依赖 ({len(new_deps)})", "list": sorted(new_deps)})
    if removed_deps:
        sections.append({"title": f"移除依赖 ({len(removed_deps)})", "list": sorted(removed_deps)})
    if upgraded:
        sections.append({
            "title": f"版本变更 ({len(upgraded)})",
            "table": {
                "headers": ["依赖名", "旧版本", "新版本"],
                "rows": [[u["name"], u["old_ver"], u["new_ver"]] for u in upgraded],
            },
        })

    # 约定漂移
    convs1 = {(r["category"], r["name"]): r for r in conn1.execute(
        "SELECT category, name, value, count FROM conventions"
    ).fetchall()}
    convs2 = {(r["category"], r["name"]): r for r in conn2.execute(
        "SELECT category, name, value, count FROM conventions"
    ).fetchall()}
    drift = []
    for key in set(convs1.keys()) & set(convs2.keys()):
        c1, c2 = convs1[key], convs2[key]
        if c1["value"] != c2["value"] or abs(c1["count"] - c2["count"]) > max(c1["count"] * 0.1, 5):
            drift.append({
                "category": key[0],
                "name": key[1],
                "old_value": c1["value"],
                "new_value": c2["value"],
                "old_count": c1["count"],
                "new_count": c2["count"],
            })
    if drift:
        sections.append({
            "title": f"约定漂移 ({len(drift)})",
            "table": {
                "headers": ["类别", "名称", "旧值", "新值", "旧计数", "新计数"],
                "rows": [[d["category"], d["name"], d["old_value"], d["new_value"], d["old_count"], d["new_count"]]
                         for d in drift],
            },
        })

    if not sections:
        sections.append({"title": "对比结果", "text": "两次扫描之间未检测到变化"})

    return fmt.render_report(sections, "env-radar 版本对比")


def cmd_export(conn: sqlite3.Connection, fmt: OutputFormatter, args: argparse.Namespace) -> str:
    """导出完整报告命令。"""
    sections = []

    # 总览
    meta = conn.execute("SELECT * FROM project_meta LIMIT 1").fetchone()
    if meta:
        sections.append({
            "title": "项目总览",
            "table": {
                "headers": ["属性", "值"],
                "rows": [
                    ["项目名", meta["name"] or "-"],
                    ["根路径", meta["root_path"] or "-"],
                    ["文件总数", meta["total_files"] or 0],
                    ["代码总行数", meta["total_lines"] or 0],
                    ["扫描时间", datetime.fromtimestamp(meta["scanned_at"]).strftime("%Y-%m-%d %H:%M:%S") if meta["scanned_at"] else "-"],
                ],
            },
        })

    # 模块
    modules = conn.execute("SELECT name, path, file_count FROM modules ORDER BY name").fetchall()
    if modules:
        sections.append({
            "title": "模块",
            "table": {
                "headers": ["模块名", "路径", "文件数"],
                "rows": [[m["name"], m["path"], m["file_count"]] for m in modules],
            },
        })

    # 依赖
    deps = conn.execute(
        "SELECT name, version, is_outdated, dep_type FROM dependencies ORDER BY name"
    ).fetchall()
    if deps:
        sections.append({
            "title": "依赖",
            "table": {
                "headers": ["依赖名", "版本", "过期", "类型"],
                "rows": [[d["name"], d["version"], "是" if d["is_outdated"] else "否", d["dep_type"] or "-"]
                         for d in deps],
            },
        })

    # 约定
    convs = conn.execute(
        "SELECT category, name, value, count FROM conventions ORDER BY category, name"
    ).fetchall()
    if convs:
        sections.append({
            "title": "约定",
            "table": {
                "headers": ["类别", "名称", "值", "计数"],
                "rows": [[c["category"], c["name"], c["value"] or "-", c["count"]] for c in convs],
            },
        })

    # 技术债
    debts = conn.execute(
        "SELECT t.tag, t.content, f.rel_path FROM tech_debt t "
        "JOIN files f ON t.file_id = f.id ORDER BY t.tag"
    ).fetchall()
    if debts:
        sections.append({
            "title": "技术债",
            "table": {
                "headers": ["标签", "内容", "文件"],
                "rows": [[d["tag"], (d["content"] or "")[:80], d["rel_path"]] for d in debts],
            },
        })

    return fmt.render_report(sections, "env-radar 完整报告")
