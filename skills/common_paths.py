"""子技能共享路径解析（只读）。

原则：
- 会话真源优先走 session-digger 索引（index.db 的 jsonl_path，支持文件与虚拟 URI）
- 索引未命中再降级扫各环境目录
- 禁止硬编码本机用户路径；尊重 SESSION_DIGGER_DATA_DIR / SESSION_DIGGER_ROOT
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional


def digger_root() -> Path:
    """解析 session-digger 安装根（环境变量 → 当前仓 → 宿主安装位）。"""
    env = os.environ.get("SESSION_DIGGER_ROOT") or os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env:
        p = Path(env).expanduser()
        if (p / "scripts" / "sd-recall.py").is_file() or (p / "skills").is_dir():
            return p.resolve()

    # 相对本文件：skills/common_paths.py → 仓库根（当前工作副本优先于全局安装旧副本）
    here = Path(__file__).resolve().parent.parent
    if (here / "scripts" / "sd-recall.py").is_file():
        return here

    home = Path.home()
    for c in (
        home / ".gemini/antigravity/skills/session-digger",
        home / ".dsh/skills/session-digger",
        home / ".zcode/skills/session-digger",
        home / ".agents/skills/session-digger",
        home / ".claude/plugins/session-digger",
        home / ".claude/skills/session-digger",
        home / ".grok/skills/session-digger",
    ):
        if (c / "scripts" / "sd-recall.py").is_file() or (c / "skills").is_dir():
            return c.resolve()
    return here


def _scripts_dir() -> Path:
    """``<root>/scripts`` — the package every canonical helper lives in."""
    return digger_root() / "scripts"


def _ensure_scripts_on_path() -> None:
    scripts = str(_scripts_dir())
    if scripts not in sys.path:
        sys.path.insert(0, scripts)


def data_dir() -> Path:
    """索引与报告数据目录，单一真源委托给 index_builder._schema。"""
    _ensure_scripts_on_path()
    from index_builder._schema import DB_DIR
    return DB_DIR


def index_db_path() -> Path:
    _ensure_scripts_on_path()
    from index_builder._schema import DB_PATH
    return DB_PATH


def resolve_session_ref(session_id: str) -> Optional[str]:
    """session_id → 可读文件路径或虚拟 URI（zcode://、dimcode:// 等）。

    1) 若传入本身为虚拟 URI 或本地路径，直接返回
    2) index.db（通过复用只读连接池查询精确 ID / agent:id / 长 ID 后缀）
    3) 无索引时的多宿主目录扫描降级
    """
    if not session_id or not str(session_id).strip():
        return None
    sid = str(session_id).strip()

    _ensure_scripts_on_path()
    from index_builder._reader import is_virtual_path

    if is_virtual_path(sid) or os.path.exists(sid):
        return sid

    db = index_db_path()
    if db.is_file():
        try:
            from echolib._helpers import get_ro_sqlite
            conn = get_ro_sqlite(db)
            row = conn.execute(
                "SELECT jsonl_path FROM sessions WHERE id = ? OR id LIKE ? LIMIT 1",
                (sid, f"%:{sid}"),
            ).fetchone()
            if not row and len(sid) >= 12:
                row = conn.execute(
                    """SELECT jsonl_path FROM sessions
                       WHERE id LIKE ? OR jsonl_path LIKE ?
                       LIMIT 1""",
                    (f"%{sid}%", f"%{sid}%"),
                ).fetchone()
            if row and row[0]:
                ref = str(row[0])
                if is_virtual_path(ref):
                    return ref
                p = Path(ref)
                if p.is_file():
                    return str(p)
                if p.is_dir():
                    for name in (
                        "chat_history.jsonl",
                        "updates.jsonl",
                        "wire.jsonl",
                        "transcript.jsonl",
                    ):
                        cand = p / name
                        if cand.is_file():
                            return str(cand)
        except sqlite3.Error:
            pass

    found = _scan_filesystem(sid)
    return str(found) if found else None


def resolve_session_jsonl(session_id: str) -> Optional[Path]:
    """向后兼容封装：仅当目标为本地路径时返回 Path（虚拟 URI 请改用 resolve_session_ref）。"""
    ref = resolve_session_ref(session_id)
    if not ref:
        return None
    _ensure_scripts_on_path()
    from index_builder._reader import is_virtual_path
    if is_virtual_path(ref):
        return None
    p = Path(ref)
    return p if p.exists() else None


def _scan_filesystem(session_id: str) -> Optional[Path]:
    """无索引时的目录扫描降级（覆盖五大主力环境与兼容目录）。"""
    home = Path.home()

    # Antigravity CLI & IDE
    for ag_root in (
        home / ".gemini" / "antigravity-cli" / "brain",
        home / ".gemini" / "antigravity" / "brain",
    ):
        cand = ag_root / session_id / ".system_generated" / "logs" / "transcript.jsonl"
        if cand.is_file():
            return cand

    # DSH (~/.dsh/sessions/<slug>/<session-dir>/session[.vN].jsonl.zstd)
    dsh_root = home / ".dsh" / "sessions"
    if dsh_root.is_dir():
        for sess_dir in dsh_root.glob(f"*/{session_id}*"):
            if sess_dir.is_dir():
                for zstd_file in sorted(sess_dir.glob("session*.jsonl.zstd"), reverse=True):
                    return zstd_file

    # Claude Code
    claude_root = home / ".claude" / "projects"
    if claude_root.is_dir():
        for project_dir in claude_root.iterdir():
            if not project_dir.is_dir():
                continue
            candidate = project_dir / f"{session_id}.jsonl"
            if candidate.is_file():
                return candidate
            nested = project_dir / session_id
            if nested.is_dir():
                hits = list(nested.glob("*.jsonl"))
                if hits:
                    return hits[0]

    # Grok Build
    grok_root = home / ".grok" / "sessions"
    if grok_root.is_dir():
        for cwd_dir in grok_root.rglob("*"):
            if cwd_dir.is_dir() and cwd_dir.name == session_id:
                chat = cwd_dir / "chat_history.jsonl"
                if chat.is_file():
                    return chat

    # Kimi Code
    for root_name, wire in (
        (".kimi/sessions", "wire.jsonl"),
        (".kimi-code/sessions", "wire.jsonl"),
    ):
        root = home / root_name
        if not root.is_dir():
            continue
        for session_dir in root.rglob("*"):
            if session_dir.is_dir() and session_dir.name == session_id:
                w = session_dir / wire
                if w.is_file():
                    return w
                w2 = session_dir / "agents" / "main" / wire
                if w2.is_file():
                    return w2

    # Codex
    codex_root = home / ".codex" / "sessions"
    if codex_root.is_dir():
        for jsonl_file in codex_root.rglob("*.jsonl"):
            if session_id in jsonl_file.name or session_id in str(jsonl_file.parent):
                return jsonl_file

    # Cursor
    cursor_root = home / ".cursor" / "projects"
    if cursor_root.is_dir():
        for jsonl_file in cursor_root.rglob("*.jsonl"):
            if session_id in jsonl_file.parts or session_id in jsonl_file.stem:
                return jsonl_file

    return None
