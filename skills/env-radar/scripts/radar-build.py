#!/usr/bin/env python3
"""radar-build.py — 项目环境雷达扫描器（入口）。

扫描指定项目目录，生成包含结构/约定/健康/演化四个维度的 SQLite 索引数据库。
主流程在此编排，四个维度的采集分别由 radar_structure / radar_convention /
radar_health / radar_evolution 实现。

Usage:
  python3 radar-build.py <project_path> [--output <db_path>]
"""
import argparse
import sys
import time
from pathlib import Path

from radar_common import (
    CODE_EXTENSIONS,
    DEFAULT_EXCLUDES,
    count_lines,
    init_db,
    log,
    run_git,
)
from radar_convention import ConventionScanner
from radar_evolution import EvolutionScanner
from radar_health import HealthScanner
from radar_structure import StructureScanner


def build_radar(project_path: str, output_path: str | None = None) -> str:
    """主构建函数，返回数据库路径"""
    root = Path(project_path).resolve()

    if not root.exists():
        raise FileNotFoundError(f"项目路径不存在: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"不是目录: {root}")

    # 确定输出路径
    if output_path:
        db_path = Path(output_path).resolve()
    else:
        db_path = root / '.env-radar' / 'radar.db'

    log(f"项目路径: {root}")
    log(f"数据库路径: {db_path}")

    # 幂等重建：增量扫描尚未实现，每次构建都清空旧数据后全量写入，
    # 保证重复扫描不产生重复行（原实现直接追加导致数据翻倍膨胀）。
    conn = init_db(db_path)
    conn.executescript("""
        DELETE FROM project_meta;
        DELETE FROM files;
        DELETE FROM modules;
        DELETE FROM dependencies;
        DELETE FROM conventions;
        DELETE FROM evolution;
        DELETE FROM tech_debt;
        DELETE FROM commit_stats;
    """)

    # 获取 git 信息
    git_branch = run_git(['rev-parse', '--abbrev-ref', 'HEAD'], root)
    git_remote = run_git(['remote', 'get-url', 'origin'], root)

    # 1. 结构扫描
    struct_scanner = StructureScanner(root, DEFAULT_EXCLUDES)
    files, modules = struct_scanner.scan()

    # 填充行数（只对代码文件）
    log("统计文件行数...")
    for f in files:
        if f['extension'] in CODE_EXTENSIONS:
            f['lines'] = count_lines(Path(f['path']))

    total_lines = sum(f['lines'] for f in files)

    # 2. 约定扫描
    conv_scanner = ConventionScanner(root, files)
    conventions = conv_scanner.scan()

    # 3. 健康扫描
    health_scanner = HealthScanner(root, files)
    deps, health_conventions = health_scanner.scan()
    conventions.extend(health_conventions)

    # 写入数据库（先写入文件以获取 id，供演化扫描使用）
    log("写入数据库...")

    # 项目元信息
    conn.execute("""
        INSERT INTO project_meta (root_path, name, scanned_at, total_files, total_lines, git_branch, git_remote)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (str(root), root.name, time.time(), len(files), total_lines, git_branch, git_remote))

    # 文件（用 lastrowid 直接取 id，避免逐条回查）
    file_id_map: dict[str, int] = {}
    for f in files:
        cur = conn.execute("""
            INSERT INTO files (path, rel_path, extension, size_bytes, lines, is_entry, module_name, depth, parent_dir)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (f['path'], f['rel_path'], f['extension'], f['size_bytes'],
              f['lines'], f['is_entry'], f['module_name'], f['depth'], f['parent_dir']))
        file_id_map[f['path']] = cur.lastrowid

    # 更新 files 的 id 字段
    for f in files:
        f['id'] = file_id_map.get(f['path'], 0)

    # 4. 演化扫描（需要文件 id 作为外键）
    evo_scanner = EvolutionScanner(root, files)
    evolution, tech_debt, commit_stats = evo_scanner.scan()

    # 模块
    for m in modules:
        conn.execute("""
            INSERT INTO modules (name, path, file_count, main_language, is_leaf)
            VALUES (?, ?, ?, ?, ?)
        """, (m['name'], m['path'], m['file_count'], m['main_language'], m['is_leaf']))

    # 依赖
    for d in deps:
        conn.execute("""
            INSERT INTO dependencies (name, version, latest_version, is_outdated, dep_type, package_manager, file_source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (d['name'], d['version'], d['latest_version'], d['is_outdated'],
              d['dep_type'], d['package_manager'], d['file_source']))

    # 约定
    for c in conventions:
        conn.execute("""
            INSERT INTO conventions (category, name, value, count, sample_files)
            VALUES (?, ?, ?, ?, ?)
        """, (c['category'], c['name'], c['value'], c['count'], c['sample_files']))

    # 演化
    for e in evolution:
        conn.execute("""
            INSERT INTO evolution (file_id, commit_count, last_modified, first_seen, author_count)
            VALUES (?, ?, ?, ?, ?)
        """, (e['file_id'], e['commit_count'], e['last_modified'], e['first_seen'], e['author_count']))

    # 技术债
    for td in tech_debt:
        conn.execute("""
            INSERT INTO tech_debt (file_id, line_number, tag, content)
            VALUES (?, ?, ?, ?)
        """, (td['file_id'], td['line_number'], td['tag'], td['content']))

    # 提交统计
    for cs in commit_stats:
        conn.execute("""
            INSERT INTO commit_stats (period, period_start, commit_count, files_changed, authors)
            VALUES (?, ?, ?, ?, ?)
        """, (cs['period'], cs['period_start'], cs['commit_count'], cs['files_changed'], cs['authors']))

    conn.commit()
    conn.close()

    log(f"扫描完成：{len(files)} 文件，{len(modules)} 模块，{len(deps)} 依赖，{len(tech_debt)} 技术债")
    return str(db_path)


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='env-radar: 项目环境雷达扫描器',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('project_path', help='要扫描的项目根目录')
    parser.add_argument('--output', '-o', default=None,
                        help='输出数据库路径（默认: <project_path>/.env-radar/radar.db）')

    args = parser.parse_args()

    try:
        start = time.time()
        db_path = build_radar(args.project_path, args.output)
        elapsed = time.time() - start
        log(f"耗时 {elapsed:.1f}s")
        # 输出数据库路径到 stdout
        print(db_path)
    except FileNotFoundError as e:
        log(f"错误: {e}")
        sys.exit(1)
    except NotADirectoryError as e:
        log(f"错误: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        log("已取消")
        sys.exit(130)
    except Exception as e:
        log(f"未知错误: {e}")
        raise


if __name__ == '__main__':
    main()
