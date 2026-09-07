#!/usr/bin/env python3
"""radar_convention.py — 约定雷达：命名规范、代码风格、架构模式、错误处理、注释密度。"""
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from radar_common import CODE_EXTENSIONS, detect_naming_style, log, safe_read_text


class ConventionScanner:
    """约定雷达：命名规范、代码风格、架构模式、错误处理、注释密度"""

    # import/require 正则
    IMPORT_PATTERNS = {
        'python': re.compile(r'^\s*(?:from|import)\s+([\w.]+)', re.MULTILINE),
        'javascript': re.compile(r'''(?:import|require)\s*\(?['"]([\w@./]+)'''),
        'typescript': re.compile(r'''(?:import|require)\s*\(?['"]([\w@./]+)'''),
        'go': re.compile(r'import\s+\(?["`]([\w./]+)'),
        'rust': re.compile(r'use\s+([\w:]+)'),
        'ruby': re.compile(r'require\s+[\'"]([\w./]+)'),
        'php': re.compile(r'(?:use|require|include)\s+[\'"]([\w./]+)'),
    }

    # 架构模式痕迹
    ARCH_PATTERNS = {
        'mvc': re.compile(r'(?:controllers?|models?|views?|routes?)\b', re.I),
        'layered': re.compile(r'(?:service|repository|dao|dto|middleware)\b', re.I),
        'plugin': re.compile(r'(?:plugin|extension|addon|hook)\b', re.I),
        'microservice': re.compile(r'(?:gateway|service-discovery|consul|etcd)\b', re.I),
        'event_driven': re.compile(r'(?:event-emitter|pubsub|message-bus|kafka|rabbitmq)\b', re.I),
        'cqrs': re.compile(r'(?:command-bus|query-bus|event-sourcing)\b', re.I),
    }

    # 错误处理模式
    ERROR_PATTERNS = {
        'try_catch': re.compile(r'\b(try\s*\{|catch\s*\(|except\s+\w)', re.I),
        'result_type': re.compile(r'\b(Result|Either|Option|Maybe)\b'),
        'error_boundary': re.compile(r'(error-boundary|componentDidCatch|getDerivedStateFromError)', re.I),
        'panic': re.compile(r'\bpanic!\b'),
        'raise': re.compile(r'\braise\s+\w+'),
    }

    def __init__(self, root: Path, files: list[dict]):
        self.root = root
        self.files = files
        self.stats: dict = {}

    def scan(self) -> list[dict]:
        """执行约定扫描，返回 conventions 记录列表"""
        log("分析代码约定...")

        conventions = []
        naming_styles = Counter()
        indent_styles = Counter()
        quote_styles = Counter()
        arch_traces = Counter()
        error_patterns = Counter()
        total_comment_lines = 0
        total_code_lines = 0
        import_modules: Counter = Counter()
        sample_files: dict[str, list[str]] = defaultdict(list)

        # 只扫描代码文件，最多取 500 个样本
        code_files = [
            f for f in self.files
            if f['extension'] in CODE_EXTENSIONS
        ]
        if not code_files:
            log("无代码文件，跳过约定分析")
            return []

        sample_size = min(len(code_files), 500)
        # 按文件大小排序，取中间段作为样本（避免过大过小，也避免
        # 原实现从最小文件起步导致采样整体偏小）
        start = (len(code_files) - sample_size) // 2
        sample_files_list = sorted(code_files, key=lambda x: x['size_bytes'])[start:start + sample_size]

        for file_info in sample_files_list:
            fp = Path(file_info['path'])
            ext = file_info['extension']
            lang = CODE_EXTENSIONS.get(ext)
            if not lang:
                continue

            text = safe_read_text(fp, max_bytes=200_000)
            if not text:
                continue

            lines = text.splitlines()
            file_comment_lines = 0

            # 1. 命名规范（从文件名和标识符）
            # 提取标识符
            identifiers = re.findall(r'\b([a-z_][a-z0-9_]{2,}|[A-Z][a-zA-Z0-9]{2,})\b', text)
            for ident in identifiers[:50]:  # 每文件最多 50 个
                style = detect_naming_style(ident)
                if style:
                    naming_styles[style] += 1

            # 2. 缩进风格
            tab_lines = sum(1 for l in lines if l.startswith('\t'))
            space_lines = sum(1 for l in lines if l.startswith('  '))
            if tab_lines > space_lines:
                indent_styles['tab'] += 1
            elif space_lines > 0:
                indent_styles['space'] += 1

            # 3. 引号风格
            single_quotes = len(re.findall(r"'[^']*'", text))
            double_quotes = len(re.findall(r'"[^"]*"', text))
            if single_quotes > double_quotes:
                quote_styles['single'] += 1
            elif double_quotes > 0:
                quote_styles['double'] += 1

            # 4. 注释密度
            if lang == 'python':
                file_comment_lines = sum(1 for l in lines if l.strip().startswith('#'))
                # 多行字符串/文档字符串
                file_comment_lines += text.count('"""') // 2 * 2
            elif lang in ('javascript', 'typescript', 'java', 'c', 'cpp', 'go', 'rust'):
                file_comment_lines = len(re.findall(r'//', text))
                file_comment_lines += text.count('/*')  # 块注释

            total_comment_lines += file_comment_lines
            total_code_lines += len(lines)

            # 5. 架构模式
            for arch_name, pattern in self.ARCH_PATTERNS.items():
                if pattern.search(text):
                    arch_traces[arch_name] += 1

            # 6. 错误处理
            for err_name, pattern in self.ERROR_PATTERNS.items():
                matches = len(pattern.findall(text))
                if matches > 0:
                    error_patterns[err_name] += matches

            # 7. import 统计
            if lang in self.IMPORT_PATTERNS:
                pattern = self.IMPORT_PATTERNS[lang]
                imports = pattern.findall(text)
                for imp in imports:
                    # 取顶层模块
                    top_module = imp.split('/')[0].split('.')[0]
                    if top_module and not top_module.startswith('.'):
                        import_modules[top_module] += 1

            # 收集样本文件
            if len(sample_files.get('naming', [])) < 5:
                sample_files['naming'].append(file_info['rel_path'])

        # 汇总 conventions 记录
        # 命名规范
        for style, count in naming_styles.most_common():
            conventions.append({
                'category': 'naming',
                'name': style,
                'value': str(count),
                'count': count,
                'sample_files': json.dumps(sample_files.get('naming', [])[:3], ensure_ascii=False),
            })

        # 代码风格
        for style, count in indent_styles.most_common(1):
            conventions.append({
                'category': 'style',
                'name': 'indentation',
                'value': style,
                'count': count,
                'sample_files': '[]',
            })
        for style, count in quote_styles.most_common(1):
            conventions.append({
                'category': 'style',
                'name': 'quotes',
                'value': style,
                'count': count,
                'sample_files': '[]',
            })

        # 架构模式
        for arch, count in arch_traces.most_common():
            conventions.append({
                'category': 'architecture',
                'name': arch,
                'value': str(count),
                'count': count,
                'sample_files': '[]',
            })

        # 错误处理
        for err, count in error_patterns.most_common():
            conventions.append({
                'category': 'error_handling',
                'name': err,
                'value': str(count),
                'count': count,
                'sample_files': '[]',
            })

        # 注释密度
        comment_ratio = round(total_comment_lines / max(total_code_lines, 1), 4)
        conventions.append({
            'category': 'comments',
            'name': 'comment_density',
            'value': str(comment_ratio),
            'count': total_comment_lines,
            'sample_files': json.dumps([f'{total_comment_lines}/{total_code_lines}'], ensure_ascii=False),
        })

        # import 统计（作为依赖关系的顶层统计）
        for mod, count in import_modules.most_common(20):
            conventions.append({
                'category': 'imports',
                'name': mod,
                'value': str(count),
                'count': count,
                'sample_files': '[]',
            })

        log(f"约定分析完成：{len(conventions)} 条记录")
        return conventions
