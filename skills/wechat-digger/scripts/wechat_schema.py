#!/usr/bin/env python3
"""WeChat message schema: single source of truth for table and field naming.

Two problems this module solves, both structural rather than functional:

1. **Table-name logic was duplicated eight times.** ``"Msg_" + md5(username)``
   appeared in ``vault_cli`` (twice, once each direction), ``export_chat``,
   and ``list_contacts``; three more sites matched ``LIKE 'Msg_%'`` raw. The
   forward and reverse derivations could drift independently, and a change to
   the hash (encoding, casing, digest) meant editing every copy.

2. **Field resolution was unobservable.** ``normalize._first`` probes an
   ordered list of candidate keys per logical field and silently falls back
   to a default when none match. That design is deliberately tolerant — a new
   data source only needs vaguely compatible key names to work — but nothing
   ever reported *which* candidate matched, so silent field loss across
   244k messages was invisible. ``MESSAGE_FIELD_CANDIDATES`` below is the
   canonical order; ``resolve_field`` returns the winning key so callers can
   measure coverage instead of guessing.

Compatibility note: ``scripts/acquire/*`` run standalone as subprocesses and
add no ``sys.path`` entry for the parent directory, so this module is
imported by those scripts via the ``_import_schema`` bootstrap rather than a
plain ``import``.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping, NamedTuple, Optional

# Table names in the decrypted WeChat DBs are ``Msg_<md5(username)>``.
MSG_TABLE_PREFIX = "Msg_"

# The one authoritative candidate order for each logical message field.
#
# Ordering is significant: it encodes priority, not just spelling. Where a
# value could be either an integer enum or a human label (``local_type`` is
# an int in the vault, ``type`` may be a Chinese label from an export), the
# richer source comes first.
#
# Adding a data source means adding its key spelling here, in the position
# that reflects its reliability. No call site changes.
MESSAGE_FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "msg_type": ("local_type", "msg_type", "type", "msgType", "messageType"),
    "ts": ("ts", "timestamp", "createTime", "create_time", "time", "msgCreateTime"),
    "text": ("text", "content", "message", "msg", "body"),
    "nickname": ("nickname", "displayName", "senderName", "fromName", "name"),
    "sender": ("sender_username", "fromUser", "wxid", "userName", "from"),
    "sender_fallback": ("sender",),
    "chat_id": ("chat_id", "chatId", "sessionId", "roomId", "username"),
    "chat_name": ("chat_name", "chatName", "sessionName", "roomName", "chat"),
    "msg_id": ("id", "msgId", "messageId", "server_id", "local_id", "localId", "msgSvrId"),
    "time": ("time", "timestamp"),
    "reply_to": ("reply_to", "replyTo", "referMsgId"),
}

# Decrypted-DB column aliases, keyed by logical role. Unlike the field
# candidates above (which read normalized/exported dicts), these resolve
# against the live ``PRAGMA table_info`` of a message shard.
MESSAGE_COLUMN_CANDIDATES: dict[str, tuple[str, ...]] = {
    "local_id": ("local_id", "id", "rowid"),
    "server_id": ("server_id",),
    "local_type": ("local_type", "type"),
    "create_time": ("create_time", "timestamp"),
    "real_sender_id": ("real_sender_id", "sender_id"),
    "message_content": ("message_content", "content"),
    "compress_content": ("compress_content", "WCDB_CT_message_content"),
    "compression_flag": ("WCDB_CT_message_content",),
}


class FieldHit(NamedTuple):
    """Outcome of resolving one field. ``key`` is None on a miss."""

    value: Any
    key: Optional[str]
    hit: bool

    @property
    def missed(self) -> bool:
        return not self.hit


def message_table(username: str) -> str:
    """Derive the message shard table name for a chat/contact identifier."""
    return MSG_TABLE_PREFIX + hashlib.md5(username.encode()).hexdigest()


def table_username(table: str) -> str:
    """Extract the md5 suffix from a ``Msg_<hash>`` table name.

    Returns an empty string when the name does not carry the prefix, so
    callers can treat "not a message shard" as a miss rather than an error.
    """
    if not table.startswith(MSG_TABLE_PREFIX):
        return ""
    return table[len(MSG_TABLE_PREFIX):]


def is_message_table(table: str) -> bool:
    return table.startswith(MSG_TABLE_PREFIX)


def username_for_table(table: str, usernames: Iterable[str]) -> str:
    """Reverse-resolve a shard table name back to its username.

    Requires the username set (from ``Name2Id`` or contacts) because md5 is
    not invertible. Kept here so the forward and reverse derivations cannot
    drift: both call ``message_table``.
    """
    target = table_username(table)
    if not target:
        return ""
    for username in usernames:
        if message_table(username) == table:
            return username
    return ""


def message_table_like_pattern() -> str:
    """SQL LIKE pattern matching all message shards.

    Centralized so the three raw ``LIKE 'Msg_%'`` sites cannot diverge from
    ``MSG_TABLE_PREFIX``.
    """
    return MSG_TABLE_PREFIX + "%"


def resolve_field(
    raw: Mapping[str, Any],
    field: str,
    default: Any = "",
) -> FieldHit:
    """Resolve one logical field, reporting which candidate matched.

    Unlike a bare ``_first``, the caller can now distinguish "matched" from
    "fell back", which is what makes cross-source field coverage measurable.
    """
    candidates = MESSAGE_FIELD_CANDIDATES.get(field, ())
    for key in candidates:
        if key in raw:
            return FieldHit(raw[key], key, True)
    return FieldHit(default, None, False)


def resolve_column(
    available: Mapping[str, Any] | Iterable[str],
    role: str,
) -> Optional[str]:
    """Resolve a logical column role against a live table's column list.

    ``rowid`` is always considered present: it exists implicitly on every
    SQLite table and is the documented fallback for ``local_id``.
    """
    present = available.keys() if isinstance(available, Mapping) else available
    present = set(present)
    for choice in MESSAGE_COLUMN_CANDIDATES.get(role, ()):
        if choice == "rowid" or choice in present:
            return choice
    return None


def resolve_columns(available: Mapping[str, Any] | Iterable[str]) -> dict[str, str]:
    """Resolve every column role at once, omitting unresolved roles."""
    result: dict[str, str] = {}
    for role in MESSAGE_COLUMN_CANDIDATES:
        chosen = resolve_column(available, role)
        if chosen is not None:
            result[role] = chosen
    return result
