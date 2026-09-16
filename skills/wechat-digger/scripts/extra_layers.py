#!/usr/bin/env python3
"""Zero-risk extra vault layers: voice, payments, friend requests.

These databases are already decrypted. This module never attaches to WeChat,
never reads keys, and never returns voice blobs in list endpoints.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fts_engine import _NameBook, _as_epoch, _connect_ro, vault_msg_db_path


def decrypted_root() -> Optional[Path]:
    msg = vault_msg_db_path()
    if msg:
        return msg.parent.parent
    cand = Path.home() / "Library" / "Application Support" / "wechat-local-vault" / "decrypted" / "current"
    return cand if cand.exists() else None


def _fmt(ts: Optional[int]) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError, TypeError):
        return ""


def _book(root: Path) -> _NameBook:
    return _NameBook(root)


def _time_clause(col: str, since: Optional[Any], until: Optional[Any]) -> tuple[str, list]:
    since_e, until_e = _as_epoch(since), _as_epoch(until)
    parts: list[str] = []
    params: list[Any] = []
    if since_e:
        parts.append(f"{col} >= ?")
        params.append(since_e)
    if until_e:
        parts.append(f"{col} < ?")
        params.append(until_e + 86400)
    sql = (" WHERE " + " AND ".join(parts)) if parts else ""
    return sql, params


def list_voice(
    chat: Optional[str] = None,
    since: Optional[Any] = None,
    until: Optional[Any] = None,
    limit: int = 50,
) -> dict[str, Any]:
    root = decrypted_root()
    media = (root / "message" / "media_0.db") if root else None
    if not media or not media.exists():
        return {"error": "voice_unavailable", "message": "media_0.db 未解密"}
    book = _book(root)
    where, params = _time_clause("v.create_time", since, until)
    con = _connect_ro(media)
    try:
        names = {r: u for r, u in con.execute("SELECT rowid, user_name FROM Name2Id")}
        sql = (
            "SELECT v.local_id, v.svr_id, v.chat_name_id, v.create_time, length(v.voice_data) AS nbytes "
            f"FROM VoiceInfo v{where} ORDER BY v.create_time DESC LIMIT ?"
        )
        rows = con.execute(sql, [*params, limit]).fetchall()
    finally:
        con.close()
    items = []
    chat_l = (chat or "").lower()
    for local_id, svr_id, cid, ts, nbytes in rows:
        uname = names.get(cid, "")
        display = book.display(uname) if uname else f"chat:{cid}"
        if chat_l and chat_l not in uname.lower() and chat_l not in display.lower():
            continue
        items.append({
            "id": local_id,
            "server_id": svr_id,
            "chat_id": uname or f"chat:{cid}",
            "chat_name": display,
            "chat_type": book.chat_type(uname) if uname else "unknown",
            "ts": ts,
            "time": _fmt(ts),
            "bytes": nbytes,
            "msg_type": "voice",
            "source": "media_0",
        })
    return {"count": len(items), "voice": items, "note": "仅元数据；导出音频用 extras voice-export --id"}


def export_voice(local_id: int, dest_dir: Optional[Path] = None) -> dict[str, Any]:
    root = decrypted_root()
    media = (root / "message" / "media_0.db") if root else None
    if not media or not media.exists():
        return {"error": "voice_unavailable", "message": "media_0.db 未解密"}
    out_dir = Path(dest_dir).expanduser() if dest_dir else (
        Path.home() / "Library" / "Application Support" / "wechat-local-vault" / "exports" / "voice"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    con = _connect_ro(media)
    try:
        row = con.execute(
            "SELECT voice_data, create_time FROM VoiceInfo WHERE local_id=?",
            (local_id,),
        ).fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        return {"error": "not_found", "id": local_id}
    raw = bytes(row[0])
    silk_at = raw.find(b"#!SILK")
    if silk_at >= 0:
        raw = raw[silk_at:]
        ext = "silk"
    else:
        ext = "bin"
    path = out_dir / f"{local_id}.{ext}"
    path.write_bytes(raw)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return {"written": str(path), "bytes": path.stat().st_size, "id": local_id, "time": _fmt(row[1])}


def list_payments(
    kind: str = "all",
    since: Optional[Any] = None,
    until: Optional[Any] = None,
    limit: int = 50,
) -> dict[str, Any]:
    root = decrypted_root()
    gdb = (root / "general" / "general.db") if root else None
    if not gdb or not gdb.exists():
        return {"error": "payments_unavailable", "message": "general.db 未解密"}
    book = _book(root)
    kind = (kind or "all").lower()
    items: list[dict[str, Any]] = []
    con = _connect_ro(gdb)
    try:
        if kind in ("all", "transfer"):
            where, params = _time_clause("begin_transfer_time", since, until)
            for row in con.execute(
                "SELECT transfer_id, session_name, pay_sub_type, pay_payer, pay_receiver, "
                f"begin_transfer_time, last_update_time FROM transferTable{where} "
                "ORDER BY begin_transfer_time DESC LIMIT ?",
                [*params, limit],
            ):
                session = row[1] or ""
                items.append({
                    "kind": "transfer",
                    "id": row[0],
                    "chat_id": session,
                    "chat_name": book.display(session) if session else "",
                    "payer": book.display(row[3]) if row[3] else "",
                    "receiver": book.display(row[4]) if row[4] else "",
                    "pay_sub_type": row[2],
                    "ts": row[5],
                    "time": _fmt(row[5]),
                    "updated": _fmt(row[6]),
                    "source": "general",
                })
        if kind in ("all", "redpacket"):
            # redEnvelopeTable 无时间列，按当前库全量（23 条）
            for row in con.execute(
                "SELECT message_server_id, session_name, sender_user_name, hb_status, "
                "hb_type, receive_status FROM redEnvelopeTable LIMIT ?",
                (limit,),
            ):
                session = row[1] or ""
                sender = row[2] or ""
                items.append({
                    "kind": "redpacket",
                    "id": row[0],
                    "chat_id": session,
                    "chat_name": book.display(session) if session else "",
                    "sender": book.display(sender) if sender else "",
                    "hb_status": row[3],
                    "hb_type": row[4],
                    "receive_status": row[5],
                    "source": "general",
                })
    finally:
        con.close()
    return {"count": len(items), "payments": items}


def list_friend_requests(
    since: Optional[Any] = None,
    until: Optional[Any] = None,
    limit: int = 50,
) -> dict[str, Any]:
    root = decrypted_root()
    gdb = (root / "general" / "general.db") if root else None
    if not gdb or not gdb.exists():
        return {"error": "requests_unavailable", "message": "general.db 未解密"}
    book = _book(root)
    where, params = _time_clause("timestamp_", since, until)
    con = _connect_ro(gdb)
    try:
        rows = con.execute(
            "SELECT user_name_, type_, timestamp_, content_, is_sender_, scene_ "
            f"FROM FMessageTable{where} ORDER BY timestamp_ DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    finally:
        con.close()
    items = []
    for uname, typ, ts, content, is_sender, scene in rows:
        items.append({
            "user": book.display(uname) if uname else "",
            "type": typ,
            "ts": ts,
            "time": _fmt(ts),
            "text": (content or "")[:200],
            "is_sender": bool(is_sender),
            "scene": scene,
            "source": "general",
        })
    return {"count": len(items), "requests": items}
