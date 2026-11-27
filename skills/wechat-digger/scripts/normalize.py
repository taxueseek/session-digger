#!/usr/bin/env python3
"""
Canonical message schema for wechat-digger.

一行修改消灭一类问题：所有上游格式只在此归一化一次，
分析/索引/渲染层永远只认 CanonicalMessage，不再 if source == ...
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from wechat_schema import MESSAGE_FIELD_CANDIDATES


# Canonical fields — extend only via this schema
CANONICAL_FIELDS = (
    "id",
    "chat_id",
    "chat_name",
    "sender",
    "nickname",
    "timestamp",  # ISO-8601 or unix int seconds
    "ts",         # unix int seconds (normalized)
    "text",
    "msg_type",   # text|image|voice|video|sticker|link|file|system|other
    "reply_to",
    "source",     # vault|wxcli|export|fixture
)


TYPE_ALIASES = {
    1: "text",
    3: "image",
    34: "voice",
    42: "card",
    43: "video",
    47: "sticker",
    48: "location",
    49: "link",
    50: "call",
    10000: "system",
    10002: "revoke",
    "1": "text",
    "3": "image",
    "text": "text",
    "image": "image",
    "voice": "voice",
    "video": "video",
    "sticker": "sticker",
    "link": "link",
    "file": "file",
    "system": "system",
    "call": "call",
    # vault_cli Chinese labels + variants
    "文本": "text",
    "图片": "image",
    "语音": "voice",
    "视频": "video",
    "表情": "sticker",
    "位置": "location",
    "链接/文件": "link",
    "链接": "link",
    "文件": "file",
    "通话": "call",
    "系统": "system",
    "撤回": "revoke",
    "名片": "card",
}


def _to_ts(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        # ms vs s heuristic
        v = int(value)
        if v > 10_000_000_000:  # ms
            return v // 1000
        return v
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return _to_ts(int(s))
    try:
        # ISO-8601（含 Z/±hh:mm 偏移）；naive 字符串按本地时区——
        # 微信导出时间为本地时间，与 fts 层 _as_epoch 同口径
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(s, fmt).timestamp())
        except ValueError:
            continue
    return None


def _first(d: dict, *keys: str, default: Any = "") -> Any:
    """Return the first present, non-empty candidate value.

    Empty strings and None count as absent, so a key that exists but carries
    no payload does not shadow a later candidate that does.
    """
    for k in keys:
        if k in d and d[k] is not None and d[k] != "":
            return d[k]
    return default


def _first_traced(
    d: dict,
    field: str,
    default: Any = "",
    trace: Optional[dict] = None,
) -> Any:
    """Resolve a canonical field and record which candidate matched.

    Same precedence rule as ``_first`` (empty/None is absent), but the
    winning key name is recorded into ``trace`` so field coverage across
    data sources becomes measurable instead of invisible. A miss is recorded
    under ``miss:<field>`` — that counter is the signal that a source uses a
    key spelling the candidate list does not know about.
    """
    candidates = MESSAGE_FIELD_CANDIDATES.get(field, ())
    for key in candidates:
        value = d.get(key)
        if value is not None and value != "":
            if trace is not None:
                trace[f"hit:{field}:{key}"] = trace.get(f"hit:{field}:{key}", 0) + 1
            return value
    if trace is not None:
        trace[f"miss:{field}"] = trace.get(f"miss:{field}", 0) + 1
    return default


def field_coverage(trace: dict, total: int) -> dict:
    """Summarize a trace into per-field hit rates.

    ``total`` is the number of messages traced, so each field gets a
    percentage plus the key spellings that actually matched. Fields whose
    miss rate is non-zero are the ones silently degrading.
    """
    report: dict[str, dict] = {}
    for field in MESSAGE_FIELD_CANDIDATES:
        hits: dict[str, int] = {}
        for key, count in trace.items():
            prefix = f"hit:{field}:"
            if key.startswith(prefix):
                hits[key[len(prefix):]] = count
        missed = trace.get(f"miss:{field}", 0)
        resolved = sum(hits.values())
        denominator = resolved + missed
        report[field] = {
            "resolved": resolved,
            "missed": missed,
            "miss_rate": round(missed / denominator, 4) if denominator else 0.0,
            "sources": dict(sorted(hits.items(), key=lambda kv: -kv[1])),
        }
    report["_total_messages"] = total
    return report


def _is_meaningful_char(ch: str) -> bool:
    o = ord(ch)
    if ch in "\n\t ":
        return True
    if "\u4e00" <= ch <= "\u9fff":
        return True
    if ch.isalnum() or ch in ".,;:!?，。？！、：；·…—-_/\\@#&%+*=[](){}<>\"'":
        return True
    # common emoji block (rough)
    if 0x1F300 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF:
        return True
    return False


def _clean_text(text: str, msg_type: str) -> str:
    """Drop binary garbage; keep readable text only."""
    if not text:
        return ""
    text = text.replace("\x00", "")
    # zstd magic mis-decoded as latin-1 often starts with '(' + high bytes
    if len(text) >= 4 and ord(text[0]) < 40 and any(ord(c) > 127 for c in text[:8]):
        return f"[{msg_type or 'binary'}]"

    meaningful = sum(1 for ch in text if _is_meaningful_char(ch))
    weird = len(text) - meaningful
    ratio = meaningful / max(len(text), 1)

    # any significant weird density → salvage or drop
    if weird >= 4 and (weird / max(len(text), 1) > 0.12 or ratio < 0.88):
        cleaned = "".join(ch for ch in text if _is_meaningful_char(ch) or ch in "\n\t")
        # collapse leftover noise tokens of latin junk shorter than 3 around CJK
        cleaned = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9\s\n\t.,;:!?，。？！、：；·—_/@#&%+*=\[\](){}<>\"'-]{1,}", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        cjk = sum(1 for ch in cleaned if "\u4e00" <= ch <= "\u9fff")
        if cjk >= 6:
            return cleaned
        if re.search(r"https?://\S+", cleaned):
            return cleaned
        words = re.findall(r"[A-Za-z]{3,}", cleaned)
        # readable short ascii note (e.g. "DuMate", "Orca") vs random binary alnum
        if len(words) >= 1 and len(cleaned) <= 40 and weird < 2:
            return cleaned
        if len(words) >= 3 and len("".join(words)) / max(len(cleaned), 1) > 0.5:
            return cleaned
        return f"[{msg_type or 'binary'}]"
    return text.strip()


def normalize_message(
    raw: dict,
    source: str = "unknown",
    chat_hint: Optional[dict] = None,
    trace: Optional[dict] = None,
) -> dict:
    """Normalize one heterogeneous message dict into CanonicalMessage.

    ``trace``, when supplied, accumulates per-field hit/miss counters so
    callers can report which key spellings a data source actually uses.
    Omit it to get the previous behavior with no bookkeeping overhead.
    """
    if not isinstance(raw, dict):
        raise TypeError("message must be a dict")

    chat_hint = chat_hint or {}
    # prefer local_type (int) when present — vault puts Chinese label in type
    msg_type_raw = _first_traced(raw, "msg_type", default=1, trace=trace)
    msg_type = TYPE_ALIASES.get(msg_type_raw, TYPE_ALIASES.get(str(msg_type_raw), "other"))

    ts = _to_ts(_first_traced(raw, "ts", default=None, trace=trace))
    text = _clean_text(
        str(_first_traced(raw, "text", default="", trace=trace)),
        msg_type,
    )
    # non-text payloads often embed compressed bytes; never pass raw binary to analyzers
    if msg_type not in ("text", "system", "other") and text.startswith("["):
        pass
    elif msg_type not in ("text", "system") and not text.startswith(("http://", "https://", "[")):
        letters = sum(1 for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
        if letters < max(12, len(text) // 3):
            text = f"[{msg_type}]"

    # vault: sender=display, sender_username=wxid; wxcli may invert
    nickname = str(
        _first_traced(raw, "nickname", default="", trace=trace)
        or _first_traced(raw, "sender_fallback", default="", trace=trace)
    )
    sender = str(
        _first_traced(raw, "sender", default="", trace=trace)
        or _first_traced(raw, "sender_fallback", default=nickname, trace=trace)
    )
    if not nickname:
        nickname = sender

    chat_id = str(
        _first_traced(raw, "chat_id", default="", trace=trace)
        or chat_hint.get("chat_id")
        or chat_hint.get("id")
        or chat_hint.get("username")
        or ""
    )
    chat_name = str(
        _first_traced(raw, "chat_name", default="", trace=trace)
        or chat_hint.get("chat_name")
        or chat_hint.get("name")
        or chat_hint.get("chat")
        or ""
    )

    msg_id = str(_first_traced(raw, "msg_id", default="", trace=trace))
    if not msg_id and ts is not None:
        msg_id = f"{chat_id}:{ts}:{sender}:{hash(text) & 0xFFFFFFFF:x}"

    iso = ""
    if ts is not None:
        iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "id": msg_id,
        "chat_id": chat_id,
        "chat_name": chat_name,
        "sender": sender,
        "nickname": nickname,
        "timestamp": iso or str(_first_traced(raw, "time", default="", trace=trace)),
        "ts": ts,
        "text": text,
        "msg_type": msg_type,
        "reply_to": _first_traced(raw, "reply_to", default=None, trace=trace) or None,
        "source": source,
    }


def normalize_messages(
    items: Iterable[Any],
    source: str = "unknown",
    chat_hint: Optional[dict] = None,
    trace: Optional[dict] = None,
) -> list[dict]:
    """Normalize a list or nested envelope into CanonicalMessage list.

    Pass ``trace`` to accumulate field-resolution counters across the batch;
    consume the result with ``field_coverage(trace, len(out))``.
    """
    if items is None:
        return []

    # unwrap common envelopes
    if isinstance(items, dict):
        for key in ("messages", "data", "items", "results", "history"):
            if key in items and isinstance(items[key], list):
                items = items[key]
                break
        else:
            # single message dict
            if any(k in items for k in ("text", "content", "msg", "sender", "from")):
                items = [items]
            else:
                return []

    out: list[dict] = []
    dropped = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            out.append(
                normalize_message(it, source=source, chat_hint=chat_hint, trace=trace)
            )
        except Exception:
            # A single malformed record must not abort the batch, but the
            # count is surfaced so silent data loss is visible.
            dropped += 1
            continue
    if trace is not None:
        trace["_dropped_records"] = trace.get("_dropped_records", 0) + dropped
        trace["_normalized"] = trace.get("_normalized", 0) + len(out)
    # stable sort by time when available
    out.sort(key=lambda m: (m.get("ts") is None, m.get("ts") or 0, m.get("id") or ""))
    return out


def redact_for_display(msg: dict) -> dict:
    """Drop identifiers that must not appear in user-facing text by default."""
    safe = dict(msg)
    # keep nickname, drop raw wxid-looking sender in display copy
    sender = safe.get("sender") or ""
    if re.match(r"^(wxid_|[a-z0-9_-]{6,}@chatroom)$", sender):
        safe["sender_display"] = safe.get("nickname") or "成员"
    else:
        safe["sender_display"] = safe.get("nickname") or sender
    return safe
