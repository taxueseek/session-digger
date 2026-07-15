#!/usr/bin/env bash
#
# cross-path-audit.sh — 检测 PATH 中的可执行文件冲突
#
# 为什么原生做不到：PATH 冲突是操作系统级概念，
# 单环境不感知其他路径有同名可执行文件。
#
# 输出: JSON
# 用法: bash scripts/cross-path-audit.sh

exec python3 - "$@" <<'PYEOF'
import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

# ── 收集 PATH 中所有 AI 编码工具 ──
path_dirs = os.environ.get("PATH", "").split(":")

# 扫描目标：主流 AI 编码 CLI 工具
TARGETS = [
    "claude", "codex", "grok", "kimi", "kimi-code",
    "cursor", "windsurf", "aider", "cline", "continue",
    "opencode", "mimo", "mimocode",
]

all_found = {}  # cmd -> 第一个找到的路径
conflicts = {}  # cmd -> [paths]

# which -a 等价
for target in TARGETS:
    found = []
    for d in path_dirs:
        p = Path(d) / target
        if p.exists() and os.access(p, os.X_OK):
            found.append(str(p))
    if found:
        all_found[target] = found[0]
        if len(found) > 1:
            conflicts[target] = found

# ── 输出 ──
import json

report = {
    "probe_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "path_env": os.environ.get("PATH", ""),
    "conflicts": {
        cmd: {
            "count": len(paths),
            "paths": paths,
            "status": "conflict",
            "suggestion": f"只保留一个 {cmd} 路径",
        }
        for cmd, paths in conflicts.items()
    },
    "all_found": all_found,
}
print(json.dumps(report, ensure_ascii=False, indent=2))
PYEOF
