#!/usr/bin/env python3
"""radar_structure.py — 结构雷达：目录树、文件类型、入口文件、模块边界。"""
import json
import os
from collections import Counter
from pathlib import Path

from radar_common import (
    CODE_EXTENSIONS,
    DEFAULT_EXCLUDES,
    ENTRY_PATTERNS,
    MODULE_MARKERS,
    log,
    safe_read_text,
)


class StructureScanner:
    """结构雷达：目录树、文件类型、入口文件、模块边界"""

    def __init__(self, root: Path, excludes: set[str]):
        self.root = root
        self.excludes = excludes
        self.files: list[dict] = []
        self.modules: list[dict] = []
        self.extension_counter: Counter = Counter()
        self.entry_files: set[Path] = set()

    def _should_exclude(self, path: Path) -> bool:
        """判断是否应排除该路径"""
        parts = path.relative_to(self.root).parts
        for part in parts:
            if part in self.excludes:
                return True
            # 通配符匹配
            for exc in self.excludes:
                if '*' in exc and exc.replace('*', '') in part:
                    return True
        return False

    def _detect_entries(self) -> None:
        """识别入口文件"""
        # 1. 按文件名模式
        for file_info in self.files:
            fp = Path(file_info['path'])
            ext = file_info['extension']
            lang = CODE_EXTENSIONS.get(ext)
            if lang and lang in ENTRY_PATTERNS:
                if fp.name in ENTRY_PATTERNS[lang]:
                    self.entry_files.add(fp)

        # 2. package.json 的 main 字段
        for file_info in self.files:
            if Path(file_info['path']).name == 'package.json':
                text = safe_read_text(Path(file_info['path']))
                if text:
                    try:
                        pkg = json.loads(text)
                        main = pkg.get('main')
                        if main:
                            main_path = (self.root / main).resolve()
                            if not main_path.exists():
                                # 尝试加 .js/.ts 后缀
                                for suffix in ['.js', '.ts', '.mjs']:
                                    candidate = main_path.with_suffix(suffix)
                                    if candidate.exists():
                                        main_path = candidate
                                        break
                            self.entry_files.add(main_path)
                    except (json.JSONDecodeError, ValueError):
                        pass

        # 3. pyproject.toml 的 scripts
        for file_info in self.files:
            if Path(file_info['path']).name == 'pyproject.toml':
                text = safe_read_text(Path(file_info['path']))
                if text:
                    # 简单解析 [project.scripts] 或 [tool.poetry.scripts]
                    in_scripts = False
                    for line in text.splitlines():
                        if '[project.scripts]' in line or '[tool.poetry.scripts]' in line:
                            in_scripts = True
                            continue
                        if in_scripts:
                            if line.strip().startswith('['):
                                break
                            if '=' in line:
                                script_path = line.split('=')[-1].strip().strip('"').strip("'")
                                # 通常是 module:function 格式
                                if ':' in script_path:
                                    module = script_path.split(':')[0]
                                    # 转成文件路径
                                    candidate = self.root / module.replace('.', '/') / '__init__.py'
                                    if candidate.exists():
                                        self.entry_files.add(candidate)
                                    candidate2 = self.root / (module.replace('.', '/') + '.py')
                                    if candidate2.exists():
                                        self.entry_files.add(candidate2)

    def _detect_modules(self) -> None:
        """识别模块边界"""
        module_dirs: dict[str, dict] = {}

        for file_info in self.files:
            fp = Path(file_info['path'])
            if fp.name in MODULE_MARKERS:
                module_dir = fp.parent
                rel = module_dir.relative_to(self.root)
                key = str(rel)
                if key not in module_dirs:
                    module_dirs[key] = {
                        'name': module_dir.name or self.root.name,
                        'path': str(rel),
                        'files': Counter(),
                        'is_leaf': True,
                    }
                # 统计该目录下的语言
                ext = file_info['extension']
                if ext in CODE_EXTENSIONS:
                    module_dirs[key]['files'][CODE_EXTENSIONS[ext]] += 1

        # 判断是否为叶子模块（不被其他模块包含）
        module_paths = sorted(module_dirs.keys(), key=lambda x: x.count('/'))
        for i, path_i in enumerate(module_paths):
            for j, path_j in enumerate(module_paths):
                if i != j and path_j.startswith(path_i + '/'):
                    module_dirs[path_i]['is_leaf'] = False
                    break

        for key, info in module_dirs.items():
            main_lang = info['files'].most_common(1)[0][0] if info['files'] else 'unknown'
            self.modules.append({
                'name': info['name'],
                'path': info['path'],
                'file_count': sum(info['files'].values()),
                'main_language': main_lang,
                'is_leaf': info['is_leaf'],
            })

    def scan(self) -> tuple[list[dict], list[dict]]:
        """执行扫描，返回 (files, modules)"""
        log("扫描目录结构...")

        for dirpath, dirnames, filenames in os.walk(self.root):
            dir_p = Path(dirpath)

            # 过滤隐藏目录（原地修改 dirnames 以阻止 os.walk 进入）
            dirnames[:] = [d for d in dirnames if not d.startswith('.')]
            # 再按排除规则过滤
            dirnames[:] = [
                d for d in dirnames
                if not self._should_exclude(dir_p / d)
            ]

            for fname in filenames:
                fp = dir_p / fname
                if self._should_exclude(fp):
                    continue

                rel = fp.relative_to(self.root)
                ext = fp.suffix.lower()

                try:
                    size = fp.stat().st_size
                except (OSError, PermissionError):
                    continue

                self.extension_counter[ext] += 1

                self.files.append({
                    'path': str(fp),
                    'rel_path': str(rel),
                    'extension': ext,
                    'size_bytes': size,
                    'lines': 0,  # 稍后填充
                    'is_entry': False,
                    'module_name': None,
                    'depth': len(rel.parts) - 1,
                    'parent_dir': str(rel.parent),
                })

        log(f"发现 {len(self.files)} 个文件，{len(self.extension_counter)} 种扩展名")

        # 识别入口和模块
        self._detect_entries()
        self._detect_modules()

        # 标记入口文件
        for f in self.files:
            if Path(f['path']) in self.entry_files:
                f['is_entry'] = True

        # 标记模块归属
        module_path_set = {m['path'] for m in self.modules}
        for f in self.files:
            rel = f['rel_path']
            # 找到最近的模块
            best_match = ''
            for mp in module_path_set:
                if rel.startswith(mp + '/') or rel.startswith(mp):
                    if len(mp) > len(best_match):
                        best_match = mp
            if best_match:
                f['module_name'] = best_match.split('/')[0] if '/' in best_match else best_match

        return self.files, self.modules
