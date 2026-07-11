import json
import math
import os
import re
import sqlite3
import sys
import concurrent.futures
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
import time as _time

CLAUDE_DIR = Path.home() / ".claude" / "projects"

NOISE_TYPES = frozenset({"progress", "queue-operation"})

KNOWN_TYPES = frozenset({
    "user", "assistant", "system", "summary", "progress",
    "queue-operation", "file-history-snapshot", "pr-link",
})

_NOISE_STRINGS = ('"queue-operation"', '"progress"')

def _iter_jsonl(path):
    """Yield parsed JSON records from a JSONL file, skipping blank/error lines.

    Centralises the open-strip-parse-error_skip pattern repeated across 30+
    adapter functions.  Always uses errors="replace" and swallows OSError.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        pass

def _strip_system_reminder(text):
    """Remove <system-reminder ...>...</system-reminder> wrapper, return real content.

    Handles tags with attributes (e.g. <system-reminder data-role="user-context">).
    Returns None if the entire text is a system-reminder block (nothing left).
    """
    if not text:
        return text
    stripped = text.strip()
    if stripped.startswith("<system-reminder"):
        # Match both <system-reminder> and <system-reminder attr="...">
        if "</system-reminder>" in stripped:
            after = stripped.split("</system-reminder>", 1)[-1].strip()
            return after if after else None
        return None
    return text

def _extract_text_from_block(block):
    """Best-effort single-string extraction from one content block.

    Handles three shapes in priority order:
    1. Anthropic-style blocks: ``{"type": "text"|"input_text", "text": ...}``.
    2. Any caller-supplied dict ``keys`` (resolved in the outer helper).
    3. Bare string.
    Returns "" if nothing usable could be extracted.
    """
    if isinstance(block, str):
        return block.strip()
    if not isinstance(block, dict):
        return ""
    if block.get("type") in (None, "text") and block.get("text"):
        return str(block["text"]).strip()
    if block.get("type") == "input_text" and block.get("text"):
        return str(block["text"]).strip()
    return ""


def _extract_content_text(content, keys=("text", "message"), max_len=0):
    """Extract text from a content field that may be str, list, or dict.

    - str: returned directly
    - list: each item unpacked via ``_extract_text_from_block`` (Anthropic-style
      text/input_text first), then any dict ``keys`` fallback, else bare strings
    - dict: checked for one of *keys*; list-valued keys are unpacked block-by-block
    Returns the extracted text (optionally truncated), or "" if nothing found.

    Merged from ``_text_from_message_blob`` (index_builder/_builder.py) so
    that heterogeneous message-shape handling lives in one place.
    """
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        texts = []
        for block in content:
            extracted = _extract_text_from_block(block)
            if extracted:
                texts.append(extracted)
                continue
            if isinstance(block, dict):
                for k in keys:
                    v = block.get(k, "")
                    if isinstance(v, str) and v.strip():
                        texts.append(v)
                        break
        text = " ".join(texts) if texts else ""
    elif isinstance(content, dict):
        text = ""
        for k in keys:
            v = content.get(k, "")
            if isinstance(v, str) and v.strip():
                text = v
                break
            if isinstance(v, list):
                parts = []
                for b in v:
                    ex = _extract_text_from_block(b)
                    if ex:
                        parts.append(ex)
                    elif isinstance(b, dict):
                        for kk in keys:
                            vv = b.get(kk, "")
                            if isinstance(vv, str) and vv.strip():
                                parts.append(vv)
                                break
                if parts:
                    text = " ".join(parts)
                    break
    else:
        text = ""
    if max_len and text:
        return text[:max_len]
    return text

def _match_call_results(calls, results, errors_only=False, limit=0):
    """Yield matched tool calls from calls/results dicts keyed by call_id.

    Shared by codex_extract_tools and workbuddy_extract_tools which both
    use a two-pass pattern: collect calls and outputs separately, then
    join them by call_id.
    """
    count = 0
    for call_id, info in sorted(calls.items(), key=lambda x: x[1].get("ts", "")):
        result_info = results.get(call_id, {"preview": "(no result)", "is_error": False})
        is_error = result_info.get("is_error", False)
        if errors_only and not is_error:
            continue
        if limit and count >= limit:
            return
        yield {
            "timestamp": info.get("ts", ""),
            "name": info.get("name", ""),
            "status": "error" if is_error else "ok",
            "key_input": info.get("input_preview", ""),
            "result_preview": result_info.get("preview", ""),
        }
        count += 1
GROK_DIR = Path.home() / ".grok" / "sessions"

KIMI_DIR = Path.home() / ".kimi" / "sessions"
KIMI_CODE_DIR = Path.home() / ".kimi-code" / "sessions"

CODEX_DIR = Path.home() / ".codex"

WORKBUDDY_DIR = Path.home() / ".workbuddy"

TRAE_DIR = Path.home() / ".trae-cn"

ZCODE_DIR = Path.home() / ".zcode" / "cli" / "agents"

DIM_DIR = Path.home() / ".dim" / "memory"

DIMCODE_DB_PATH = Path.home() / ".dimcode" / "v2" / "dimcode.sqlite"

REASONIX_DIR = Path.home() / ".reasonix" / "sessions"


