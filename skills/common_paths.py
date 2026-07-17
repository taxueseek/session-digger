"""子技能共享路径解析（只读）。

原则：
- 会话真源优先走 session-digger 索引（index.db 的 jsonl_path）
- 索引未命中再降级扫各环境目录
- 禁止硬编码本机用户路径；尊重 SESSION_DIGGER_DATA_DIR / SESSION_DIGGER_ROOT
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional


def digger_root() -> Path:
    """解析 session-digger 安装根。"""
    env = os.environ.get("SESSION_DIGGER_ROOT") or os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env:
        p = Path(env)
        if (p / "scripts" / "sd-recall.py").is_file() or (p / "skills").is_dir():
            return p
    home = Path.home()
    for c in (
        home / ".agents/skills/session-digger",
        home / ".claude/plugins/session-digger",
        home / ".claude/skills/session-digger",
        home / ".grok/skills/session-digger",
    ):
        if (c / "scripts" / "sd-recall.py").is_file() or (c / "skills").is_dir():
            return c
    # 相对本文件：skills/common_paths.py → 仓库根
    here = Path(__file__).resolve().parent.parent
    return here


def data_dir() -> Path:
    """索引与报告数据目录。"""
    env = os.environ.get("SESSION_DIGGER_DATA_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude" / ".session-digger"


def index_db_path() -> Path:
    return data_dir() / "index.db"


def resolve_session_jsonl(session_id: str) -> Optional[Path]:
    """session_id → 可读 JSONL 路径。

    1) index.db（当前 digger 环境真源，含多环境入库路径）
    2) 常见环境目录扫描（无索引时的降级）
    """
    if not session_id or not str(session_id).strip():
        return None
    sid = str(session_id).strip()

    db = index_db_path()
    if db.is_file():
        try:
            # 普通路径连接更稳（部分环境 file: URI + mode=ro 会 open 失败）
            conn = sqlite3.connect(str(db.resolve()))
            try:
                conn.execute("PRAGMA query_only = ON")
            except sqlite3.Error:
                pass
            try:
                # 1) 精确 id
                row = conn.execute(
                    "SELECT jsonl_path FROM sessions WHERE id = ? LIMIT 1",
                    (sid,),
                ).fetchone()
                # 2) 仅当 sid 足够长时才模糊（避免 "context"/"wire" 误匹配）
                if not row and len(sid) >= 12:
                    row = conn.execute(
                        """SELECT jsonl_path FROM sessions
                           WHERE id LIKE ? OR jsonl_path LIKE ?
                           LIMIT 1""",
                        (f"%{sid}%", f"%{sid}%"),
                    ).fetchone()
            finally:
                conn.close()
            if row and row[0]:
                p = Path(row[0])
                if p.is_file():
                    return p
                # grok 等可能是目录
                if p.is_dir():
                    for name in (
                        "chat_history.jsonl",
                        "updates.jsonl",
                        "wire.jsonl",
                        "transcript.jsonl",
                    ):
                        cand = p / name
                        if cand.is_file():
                            return cand
        except sqlite3.Error:
            pass

    return _scan_filesystem(sid)


def _scan_filesystem(session_id: str) -> Optional[Path]:
    """无索引时的目录扫描降级（保持旧行为，覆盖主环境）。"""
    home = Path.home()

    claude_root = home / ".claude" / "projects"
    if claude_root.is_dir():
        for project_dir in claude_root.iterdir():
            if not project_dir.is_dir():
                continue
            candidate = project_dir / f"{session_id}.jsonl"
            if candidate.is_file():
                return candidate
            # 部分布局：project/session_id/*.jsonl
            nested = project_dir / session_id
            if nested.is_dir():
                hits = list(nested.glob("*.jsonl"))
                if hits:
                    return hits[0]

    grok_root = home / ".grok" / "sessions"
    if grok_root.is_dir():
        for cwd_dir in grok_root.rglob("*"):
            if cwd_dir.is_dir() and cwd_dir.name == session_id:
                chat = cwd_dir / "chat_history.jsonl"
                if chat.is_file():
                    return chat

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
                # nested agents/main/wire.jsonl
                w2 = session_dir / "agents" / "main" / wire
                if w2.is_file():
                    return w2

    codex_root = home / ".codex" / "sessions"
    if codex_root.is_dir():
        for jsonl_file in codex_root.rglob("*.jsonl"):
            if session_id in jsonl_file.name or session_id in str(jsonl_file.parent):
                return jsonl_file

    cursor_root = home / ".cursor" / "projects"
    if cursor_root.is_dir():
        for jsonl_file in cursor_root.rglob("*.jsonl"):
            if session_id in jsonl_file.parts or session_id in jsonl_file.stem:
                return jsonl_file

    return None
