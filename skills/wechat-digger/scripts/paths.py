#!/usr/bin/env python3
"""
Path resolution for wechat-digger.

原则（对齐 session-digger）：
- 禁止硬编码本机个人仓库路径
- 优先环境变量 → 宿主插件根 → 本文件相对 → 常见 skill 安装位
- 多副本并存时 export WECHAT_DIGGER_ROOT
- 采集栈优先用本 skill 内嵌 scripts/acquire/（不再强制依赖 wechat-local-vault）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable, Optional


# 一行扩展一类工具：name → (env, marker relative to root OR absolute marker under scripts)
ACQUIRE_TOOLS = {
    "vault_cli": "vault_cli.py",
    "decrypt": "decrypt_all_dbs.py",
    "keys": "extract_keys.py",
    "export_chat": "export_chat.py",
    "list_contacts": "list_contacts.py",
    "search_sns": "search_sns.py",
}


def _candidates_for_skill(name: str) -> list[Path]:
    """Generate candidate roots for a named skill package."""
    env_map = {
        "wechat-digger": "WECHAT_DIGGER_ROOT",
        "wechat-local-vault": "WECHAT_VAULT_ROOT",
        "wechat-insight": "WECHAT_INSIGHT_ROOT",
    }
    out: list[Path] = []
    env_key = env_map.get(name)
    if env_key and os.environ.get(env_key):
        out.append(Path(os.environ[env_key]).expanduser())

    for host in ("CLAUDE_PLUGIN_ROOT", "HERDR_PLUGIN_ROOT", "GROK_SKILL_ROOT"):
        if os.environ.get(host):
            out.append(Path(os.environ[host]).expanduser())

    home = Path.home()
    for base in (
        home / ".agents" / "skills" / name,
        home / ".claude" / "skills" / name,
        home / ".claude" / "plugins" / name,
        home / ".grok" / "skills" / name,
        home / ".codex" / "skills" / name,
        home / ".kimi" / "skills" / name,
    ):
        out.append(base)

    seen = set()
    unique: list[Path] = []
    for p in out:
        try:
            key = str(p.resolve()) if p.exists() else str(p)
        except OSError:
            key = str(p)
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def find_skill_root(name: str, marker_rel: str) -> Optional[Path]:
    """Return first existing skill root that contains marker_rel."""
    for cand in _candidates_for_skill(name):
        marker = cand / marker_rel
        if marker.is_file() or marker.is_dir():
            return cand
    return None


def digger_root() -> Path:
    """Resolve wechat-digger package root."""
    env = os.environ.get("WECHAT_DIGGER_ROOT")
    if env:
        p = Path(env).expanduser()
        if (p / "scripts" / "wd.py").is_file():
            return p.resolve()

    here = Path(__file__).resolve().parent.parent
    if (here / "scripts" / "wd.py").is_file():
        return here

    found = find_skill_root("wechat-digger", "scripts/wd.py")
    if found:
        return found.resolve()

    return Path(__file__).resolve().parent.parent


def digger_python() -> str:
    """
    Interpreter for acquire scripts.

    Prefer skill-local .venv (has pycryptodome/zstandard) over system python.
    Override: WECHAT_DIGGER_PYTHON
    """
    env = os.environ.get("WECHAT_DIGGER_PYTHON")
    if env and Path(env).expanduser().is_file():
        return str(Path(env).expanduser())
    venv_py = digger_root() / ".venv" / "bin" / "python"
    if venv_py.is_file():
        return str(venv_py)
    return sys.executable


def acquire_dir() -> Optional[Path]:
    """
    Resolve directory that contains vault_cli.py / decrypt / keys.

    Priority (one table — 新增候选零改核心调用方):
      1. WECHAT_ACQUIRE_DIR
      2. digger bundled scripts/acquire/
      3. WECHAT_VAULT_ROOT/scripts or wechat-local-vault skill
    """
    env = os.environ.get("WECHAT_ACQUIRE_DIR")
    if env:
        p = Path(env).expanduser()
        if (p / "vault_cli.py").is_file():
            return p.resolve()

    bundled = digger_root() / "scripts" / "acquire"
    if (bundled / "vault_cli.py").is_file():
        return bundled.resolve()

    # legacy external vault skill
    if os.environ.get("WECHAT_VAULT_ROOT"):
        root = Path(os.environ["WECHAT_VAULT_ROOT"]).expanduser()
        for sub in (root / "scripts", root):
            if (sub / "vault_cli.py").is_file():
                return sub.resolve()

    external = find_skill_root("wechat-local-vault", "scripts/vault_cli.py")
    if external:
        return (external / "scripts").resolve()

    return None


def acquire_tool(name: str) -> Optional[Path]:
    """Resolve a named acquire script via ACQUIRE_TOOLS table."""
    filename = ACQUIRE_TOOLS.get(name)
    if not filename:
        return None
    # explicit override per tool
    env_key = f"WECHAT_ACQUIRE_{name.upper()}"
    if os.environ.get(env_key):
        p = Path(os.environ[env_key]).expanduser()
        if p.is_file():
            return p.resolve()
    base = acquire_dir()
    if not base:
        return None
    path = base / filename
    return path.resolve() if path.is_file() else None


def vault_cli_path() -> Optional[Path]:
    return acquire_tool("vault_cli")


def vault_root() -> Optional[Path]:
    """
    Backward-compatible: directory that *owns* vault scripts.
    Prefer digger package root when acquire is bundled; else external skill root.
    """
    tool = vault_cli_path()
    if not tool:
        return None
    # .../wechat-digger/scripts/acquire/vault_cli.py → digger root
    if tool.parent.name == "acquire" and tool.parent.parent.name == "scripts":
        return tool.parent.parent.parent
    # .../wechat-local-vault/scripts/vault_cli.py → skill root
    if tool.parent.name == "scripts":
        return tool.parent.parent
    return tool.parent


def insight_root() -> Optional[Path]:
    # analysis is bundled; optional external insight for comparison only
    if (digger_root() / "scripts" / "analyze.py").is_file():
        return digger_root()
    return find_skill_root("wechat-insight", "scripts/text_analyzer.py")


def default_data_root() -> Path:
    """Writable state root for indexes, digests, insight history."""
    env = os.environ.get("WECHAT_DIGGER_DATA")
    if env:
        return Path(env).expanduser()
    xdg = Path.home() / ".local" / "share" / "wechat-digger"
    legacy_docs = Path.home() / "Documents" / "wechat-digests"
    if legacy_docs.exists():
        return legacy_docs
    return xdg


def default_index_path() -> Path:
    return default_data_root() / "index" / "messages.db"


def private_vault_dir() -> Path:
    """Decrypted DB vault (shared with legacy wechat-local-vault config)."""
    env = os.environ.get("WECHAT_PRIVATE_VAULT")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library" / "Application Support" / "wechat-local-vault"


def iter_existing(paths: Iterable[Path]) -> list[Path]:
    return [p for p in paths if p.exists()]
