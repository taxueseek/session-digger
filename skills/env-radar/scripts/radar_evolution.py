#!/usr/bin/env python3
"""radar_evolution.py — 演化雷达：热点文件、提交频率、作者分布、技术债。"""
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from radar_common import CODE_EXTENSIONS, TECH_DEBT_TAGS, log, run_git, safe_read_text


class EvolutionScanner:
    """演化雷达：热点文件、提交频率、作者分布、技术债"""

    def __init__(self, root: Path, files: list[dict]):
        self.root = root
        self.files = files
        self.is_git = (root / '.git').exists()

    def scan(self) -> tuple[list[dict], list[dict], list[dict]]:
        """执行演化扫描，返回 (evolution_records, tech_debt_records, commit_stats)"""
        log("分析演化数据...")

        if not self.is_git:
            log("非 git 仓库，跳过演化分析")
            return [], [], []

        evolution = []
        tech_debt = []
        commit_stats = []

        # 1. 热点文件（git log 中修改频率最高的 20 个文件）
        hotspots = self._get_hotspot_files()
        file_rel_to_id = {f['rel_path']: f['id'] for f in self.files}

        for rel_path, count, last_mod, first_seen, authors in hotspots:
            file_id = file_rel_to_id.get(rel_path)
            if file_id:
                evolution.append({
                    'file_id': file_id,
                    'commit_count': count,
                    'last_modified': last_mod,
                    'first_seen': first_seen,
                    'author_count': len(authors),
                })

        # 2. 提交频率统计
        commit_stats = self._get_commit_frequency()

        # 3. 技术债标记
        tech_debt = self._scan_tech_debt(file_rel_to_id)

        log(f"演化分析完成：{len(evolution)} 个热点文件，{len(tech_debt)} 个技术债标记")
        return evolution, tech_debt, commit_stats

    def _get_hotspot_files(self) -> list[tuple]:
        """获取热点文件列表（全历史 git log）"""
        # git log --name-only --pretty=format:HASH|AUTHOR|DATE
        output = run_git(
            ['log', '--name-only', '--pretty=format:%H|%an|%aI'],
            self.root,
            timeout=20,
        )
        if not output:
            return []

        file_commits: dict[str, dict] = defaultdict(lambda: {
            'count': 0, 'last_modified': '', 'first_seen': '', 'authors': set()
        })

        current_hash = ''
        current_author = ''
        current_date = ''

        for line in output.splitlines():
            if not line:
                continue
            if '|' in line and not line.startswith(' '):
                parts = line.split('|', 2)
                if len(parts) == 3 and len(parts[0]) == 40:
                    current_hash = parts[0]
                    current_author = parts[1]
                    current_date = parts[2]
                    continue
            # 文件路径
            if current_hash and line.strip():
                fp = line.strip()
                info = file_commits[fp]
                info['count'] += 1
                info['authors'].add(current_author)
                if not info['last_modified'] or current_date > info['last_modified']:
                    info['last_modified'] = current_date
                if not info['first_seen'] or current_date < info['first_seen']:
                    info['first_seen'] = current_date

        # 排序取前 20
        sorted_files = sorted(file_commits.items(), key=lambda x: x[1]['count'], reverse=True)[:20]
        return [
            (path, info['count'], info['last_modified'], info['first_seen'], info['authors'])
            for path, info in sorted_files
        ]

    def _get_commit_frequency(self) -> list[dict]:
        """获取提交频率统计（最近 90 天，按周聚合）"""
        since = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')
        output = run_git(
            ['log', f'--since={since}', '--name-only', '--pretty=format:%aI|%an'],
            self.root,
            timeout=15,
        )
        if not output:
            return []

        week_commits: dict[str, dict] = defaultdict(lambda: {'count': 0, 'authors': set(), 'files': set()})
        current_week = ''

        for line in output.splitlines():
            if not line:
                continue
            if '|' in line and not line.startswith(' '):
                # 提交头行：日期|作者
                parts = line.split('|', 1)
                if len(parts) != 2:
                    continue
                date_str, author = parts
                try:
                    dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                except ValueError:
                    continue
                current_week = dt.strftime('%Y-W%W')
                week_commits[current_week]['count'] += 1
                week_commits[current_week]['authors'].add(author)
            elif current_week and line.strip():
                # 变更文件行，归入当前提交所在周
                week_commits[current_week]['files'].add(line.strip())

        stats = []
        # 按周聚合
        for week, info in sorted(week_commits.items()):
            stats.append({
                'period': 'week',
                'period_start': week,
                'commit_count': info['count'],
                'files_changed': len(info['files']),
                'authors': json.dumps(list(info['authors']), ensure_ascii=False),
            })

        return stats

    def _scan_tech_debt(self, file_rel_to_id: dict) -> list[dict]:
        """扫描技术债标记"""
        debt = []

        # 技术债正则
        debt_pattern = re.compile(
            r'(?:#|//|/\*|\*)\s*(' + '|'.join(TECH_DEBT_TAGS) + r')\s*[:\s]\s*(.*)',
            re.IGNORECASE
        )

        # 只扫描代码文件，最多 1000 个
        code_files = [
            f for f in self.files
            if f['extension'] in CODE_EXTENSIONS
        ][:1000]

        for file_info in code_files:
            fp = Path(file_info['path'])
            text = safe_read_text(fp, max_bytes=200_000)
            if not text:
                continue

            for i, line in enumerate(text.splitlines(), 1):
                match = debt_pattern.search(line)
                if match:
                    debt.append({
                        'file_id': file_info['id'],
                        'line_number': i,
                        'tag': match.group(1).upper(),
                        'content': match.group(2).strip()[:200],  # 截断过长内容
                    })

        return debt
