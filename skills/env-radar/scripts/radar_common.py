#!/usr/bin/env python3
"""radar_common.py — env-radar 公共模块：常量、工具函数、数据库初始化。"""
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量定义
# ---------------------------------------------------------------------------

# 默认排除的目录
DEFAULT_EXCLUDES = {
    'node_modules', '.git', '__pycache__', 'venv', '.venv', 'env',
    '.env', 'dist', 'build', '.next', '.nuxt', '.cache', 'target',
    'vendor', 'coverage', '.tox', '.mypy_cache', '.pytest_cache',
    'site-packages', '.eggs', '*.egg-info', '.terraform', '.serverless',
}

# 入口文件模式
ENTRY_PATTERNS = {
    'python': ['__main__.py', 'main.py', 'app.py', 'manage.py', 'wsgi.py', 'asgi.py'],
    'javascript': ['index.js', 'index.ts', 'main.js', 'main.ts', 'app.js', 'app.ts', 'server.js'],
    'go': ['main.go'],
    'rust': ['main.rs'],
    'java': ['Main.java', 'Application.java'],
}

# 模块边界标记文件
MODULE_MARKERS = {
    '__init__.py', 'package.json', 'Cargo.toml', 'go.mod', 'setup.py',
    'setup.cfg', 'pyproject.toml', 'pom.xml', 'build.gradle', 'CMakeLists.txt',
    'Makefile', 'Dockerfile', '.gitmodules',
}

# 技术债标记
TECH_DEBT_TAGS = ('TODO', 'FIXME', 'HACK', 'XXX', 'TEMP', 'HACKME')

# 代码文件扩展名 → 语言
CODE_EXTENSIONS = {
    '.py': 'python', '.js': 'javascript', '.ts': 'typescript',
    '.jsx': 'javascript', '.tsx': 'typescript', '.go': 'go',
    '.rs': 'rust', '.java': 'java', '.kt': 'kotlin',
    '.rb': 'ruby', '.php': 'php', '.c': 'c', '.cpp': 'cpp',
    '.h': 'c', '.hpp': 'cpp', '.cs': 'csharp', '.swift': 'swift',
    '.scala': 'scala', '.r': 'r', '.lua': 'lua', '.sh': 'shell',
    '.sql': 'sql', '.css': 'css', '.scss': 'scss', '.html': 'html',
    '.vue': 'vue', '.svelte': 'svelte',
}

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    """输出进度到 stderr"""
    print(f"[radar] {msg}", file=sys.stderr)


def safe_read_text(filepath: Path, max_bytes: int = 1_000_000) -> str | None:
    """安全读取文本文件，失败返回 None"""
    try:
        if filepath.stat().st_size > max_bytes:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                return f.read(max_bytes)
        return filepath.read_text(encoding='utf-8', errors='ignore')
    except (OSError, PermissionError):
        return None


def count_lines(filepath: Path) -> int:
    """快速统计文件行数"""
    try:
        # 大文件用 wc -l 更快
        if filepath.stat().st_size > 100_000:
            result = subprocess.run(
                ['wc', '-l', str(filepath)],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                return int(result.stdout.split()[0])
        # 小文件直接读
        with open(filepath, 'rb') as f:
            return sum(1 for _ in f)
    except (OSError, PermissionError, subprocess.TimeoutExpired):
        return 0


def run_git(args: list[str], cwd: Path, timeout: int = 15) -> str | None:
    """执行 git 命令，失败返回 None"""
    try:
        result = subprocess.run(
            ['git'] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def compare_versions(a: str, b: str) -> int:
    """语义版本比较：a < b 返回 -1，相等 0，a > b 返回 1。

    数字段按 . - + 分割，取每段前导数字；段数不同时缺省段按 0 补齐
    （如 "1.2" == "1.2.0"）；任一版本解析不出数字时回退字符串比较。
    """
    def _parse(v: str) -> tuple:
        nums = []
        for part in re.split(r'[.\-+]', v):
            m = re.match(r'\d+', part)
            if not m:
                break
            nums.append(int(m.group()))
        return tuple(nums)

    ka, kb = _parse(a), _parse(b)
    if not ka or not kb:
        return (a > b) - (a < b)
    n = max(len(ka), len(kb))
    for i in range(n):
        va = ka[i] if i < len(ka) else 0
        vb = kb[i] if i < len(kb) else 0
        if va != vb:
            return 1 if va > vb else -1
    return 0


def detect_naming_style(name: str) -> str | None:
    """检测命名风格"""
    if not name or len(name) < 2:
        return None
    # 去掉前后缀下划线/数字
    clean = name.strip('_')
    if not clean:
        return None
    if clean.islower() and '_' in clean:
        return 'snake_case'
    if clean.islower() and clean.isalpha():
        return 'flatcase'
    if clean[0].isupper() and clean[1:].isalpha() and clean[1:].islower():
        return 'PascalCase'
    if clean[0].islower() and any(c.isupper() for c in clean):
        return 'camelCase'
    if clean.isupper() and '_' in clean:
        return 'UPPER_SNAKE'
    if clean[0].isupper() and any(c.isupper() for c in clean[1:]):
        return 'PascalCase'
    return None


# ---------------------------------------------------------------------------
# 数据库初始化
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
-- 项目元信息
CREATE TABLE IF NOT EXISTS project_meta (
    id INTEGER PRIMARY KEY,
    root_path TEXT NOT NULL,
    name TEXT,
    scanned_at REAL,
    total_files INTEGER,
    total_lines INTEGER,
    git_branch TEXT,
    git_remote TEXT
);

-- 文件结构
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    extension TEXT,
    size_bytes INTEGER,
    lines INTEGER,
    is_entry BOOLEAN DEFAULT 0,
    module_name TEXT,
    depth INTEGER,
    parent_dir TEXT
);

-- 模块
CREATE TABLE IF NOT EXISTS modules (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    file_count INTEGER,
    main_language TEXT,
    is_leaf BOOLEAN DEFAULT 1
);

-- 依赖
CREATE TABLE IF NOT EXISTS dependencies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT,
    latest_version TEXT,
    is_outdated BOOLEAN DEFAULT 0,
    dep_type TEXT,
    package_manager TEXT,
    file_source TEXT
);

-- 约定统计
CREATE TABLE IF NOT EXISTS conventions (
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT,
    count INTEGER DEFAULT 0,
    sample_files TEXT
);

-- 演化数据
CREATE TABLE IF NOT EXISTS evolution (
    id INTEGER PRIMARY KEY,
    file_id INTEGER,
    commit_count INTEGER DEFAULT 0,
    last_modified TEXT,
    first_seen TEXT,
    author_count INTEGER DEFAULT 0,
    FOREIGN KEY (file_id) REFERENCES files(id)
);

-- 技术债
CREATE TABLE IF NOT EXISTS tech_debt (
    id INTEGER PRIMARY KEY,
    file_id INTEGER,
    line_number INTEGER,
    tag TEXT,
    content TEXT,
    FOREIGN KEY (file_id) REFERENCES files(id)
);

-- 提交历史聚合
CREATE TABLE IF NOT EXISTS commit_stats (
    id INTEGER PRIMARY KEY,
    period TEXT NOT NULL,
    period_start TEXT,
    commit_count INTEGER,
    files_changed INTEGER,
    authors TEXT
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_files_rel_path ON files(rel_path);
CREATE INDEX IF NOT EXISTS idx_files_extension ON files(extension);
CREATE INDEX IF NOT EXISTS idx_files_module ON files(module_name);
CREATE INDEX IF NOT EXISTS idx_evolution_file ON evolution(file_id);
CREATE INDEX IF NOT EXISTS idx_tech_debt_file ON tech_debt(file_id);
CREATE INDEX IF NOT EXISTS idx_deps_name ON dependencies(name);
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    """初始化数据库，返回连接"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn
