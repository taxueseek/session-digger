#!/usr/bin/env python3
"""
dialog-adapter.py — Universal conversation importer.

Converts various chat/dialog formats into session-digger's JSONL schema.
Supported inputs:
  - WeChat export (JSON/CSV): WeChat 4.x plaintext export format
  - Generic JSON array: [{"sender": "X", "time": "...", "content": "..."}]
  - Generic CSV: columns include sender/time/content variants
  - Plain text transcript: "HH:MM Name: message" line format

Output: JSONL file in session-digger schema that can be indexed and searched
just like any Claude Code session.

Usage:
  dialog-adapter.py <input-file> --format <auto|wechat|json|csv|transcript> [-o output.jsonl]

v1.0: Inspired by echo-sleuth wechat-local-vault + baoyu-wechat-summary.
     Enables session-digger to analyze ANY conversation history.
"""

from _common import read_json, write_json, read_text, json_out
import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

def detect_format(text, filename=""):
    """Auto-detect input format from content + filename hints."""
    fname = filename.lower()

    if "wechat" in fname or "微信" in fname:
        return "wechat"
    if fname.endswith(".csv"):
        return "csv"
    if fname.endswith(".json") or fname.endswith(".jsonl"):
        return "json"
    if text.strip().startswith(("{", "[")):
        return "json"

    # Transcript detection: "HH:MM Name: message" pattern
    transcript_pattern = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?\s+", re.MULTILINE)
    if len(transcript_pattern.findall(text[:5000])) > 3:
        return "transcript"

    return "transcript"  # Default fallback


def make_record(msg_type, timestamp, content, session_id="imported", uuid=""):
    """Create a JSONL record in session-digger schema."""
    record = {
        "type": msg_type,
        "timestamp": timestamp or datetime.now().isoformat(),
        "uuid": uuid or f"imp-{hash(content) % 10**16:016x}",
        "sessionId": session_id,
        "version": "dialog-adapter-v1.0",
    }

    if msg_type == "user":
        record["message"] = {
            "role": "user",
            "content": [{"type": "text", "text": content}],
        }
    elif msg_type == "assistant":
        record["message"] = {
            "role": "assistant",
            "content": [{"type": "text", "text": content}],
        }
    return record


def parse_wechat_json(text):
    """Parse WeChat JSON export format."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []

    records = []

    # Common WeChat export structures
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            sender = item.get("sender") or item.get("from") or item.get("nickname") or item.get("UserName", "")
            ts = item.get("time") or item.get("CreateTime") or item.get("timestamp") or ""
            content_raw = item.get("content") or item.get("msg") or item.get("text") or item.get("Message", "")
            msg_type = item.get("type", "")

            # Skip non-text (images, system, etc.)
            if msg_type in (3, 34, 42, 43, 47, 48, 49):
                continue
            if not content_raw or not content_raw.strip():
                continue
            if not isinstance(content_raw, str):
                content_raw = str(content_raw)

            record_type = "user" if sender else "assistant"
            records.append((record_type, _normalize_ts(ts), content_raw))

    elif isinstance(data, dict):
        # WeChat chatroom export: {"chatroom": "xxx", "messages": [...]}
        messages = data.get("messages") or data.get("msg_list") or data.get("MsgList") or []
        for item in messages:
            sender = item.get("sender") or item.get("from") or item.get("nickname") or ""
            ts = item.get("time") or item.get("timestamp") or item.get("CreateTime") or ""
            content_raw = item.get("content") or item.get("text") or item.get("msg") or ""
            if not content_raw or not content_raw.strip():
                continue
            record_type = "user" if sender else "assistant"
            records.append((record_type, _normalize_ts(ts), str(content_raw)))

    return records


def parse_generic_json(text):
    """Parse a plain JSON array of messages."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []

    records = []
    if not isinstance(data, list):

        # Single message or nested
        if isinstance(data, dict):
            data = [data]
        else:
            return []

    for item in data:
        if isinstance(item, str):
            records.append(("user", "", item))
            continue
        if not isinstance(item, dict):
            continue

        # Flexible field mapping
        sender = (item.get("sender") or item.get("from") or item.get("author")
                  or item.get("name") or item.get("role") or item.get("nickname") or "")
        ts = (item.get("time") or item.get("timestamp") or item.get("date")
              or item.get("created_at") or item.get("ts") or "")
        content_raw = (item.get("content") or item.get("text") or item.get("msg")
                       or item.get("body") or item.get("message") or "")

        if not content_raw or not str(content_raw).strip():
            continue

        role_hint = str(item.get("role", sender)).lower()
        if role_hint in ("assistant", "bot", "ai", "system"):
            msg_type = "assistant"
        else:
            msg_type = "user"

        records.append((msg_type, _normalize_ts(ts), str(content_raw)))

    return records


def parse_csv_file(text):
    """Parse CSV with flexible column detection."""
    import io
    records = []
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []

    # Detect columns
    col_map = {}
    for col in reader.fieldnames:
        cl = col.lower().strip()
        if cl in ("sender", "from", "author", "name", "nickname", "username"):
            col_map["sender"] = col
        elif cl in ("time", "timestamp", "date", "created_at", "ts", "datetime"):
            col_map["time"] = col
        elif cl in ("content", "text", "msg", "message", "body"):
            col_map["content"] = col
        elif cl in ("role", "type"):
            col_map["role"] = col

    sender_col = col_map.get("sender") or col_map.get("role") or reader.fieldnames[0]
    time_col = col_map.get("time", "")
    content_col = col_map.get("content", "")
    if not content_col:
        # Guess: last non-time column
        for c in reversed(reader.fieldnames):
            if c != time_col and c != sender_col:
                content_col = c
                break

    if not content_col:
        return []

    for row in reader:
        content_raw = row.get(content_col, "").strip()
        if not content_raw:
            continue
        sender = row.get(sender_col, "")
        ts = row.get(time_col, "") if time_col else ""

        role_hint = row.get(col_map.get("role", ""), sender).lower()
        if role_hint in ("assistant", "bot", "ai"):
            msg_type = "assistant"
        else:
            msg_type = "user"

        records.append((msg_type, _normalize_ts(ts), content_raw))

    return records


def parse_transcript(text):
    """
    Parse plain text transcript formats:
      "HH:MM Name: message"
      "[2024-01-01 12:00] Name: message"
      "Name (12:00): message"
    """
    records = []

    patterns = [
        # [2024-01-01 12:00:00] Name: message
        (re.compile(r"^\[(\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}(:\d{2})?)\]\s*([^:]+):\s*(.+)", re.MULTILINE),
         "bracket"),

        # HH:MM Name: message
        (re.compile(r"^(\d{1,2}:\d{2}(:\d{2})?)\s+([^\n:]+):\s*(.+)", re.MULTILINE),
         "simple"),

        # Name: message (no timestamp, single-line)
        (re.compile(r"^([A-Za-z0-9_\u4e00-\u9fff]{1,20}):\s*(.+)", re.MULTILINE),
         "nosender"),
    ]

    for pattern, ptype in patterns:
        matches = pattern.findall(text)
        if len(matches) >= 3:
            for m in matches:
                if ptype == "bracket":
                    ts_raw = m[0]
                    sender = m[2].strip()
                    content = m[3].strip()
                elif ptype == "simple":
                    ts_raw = m[0]
                    sender = m[2].strip()
                    content = m[3].strip()
                else:  # nosender
                    sender = m[0].strip()
                    content = m[1].strip()
                    ts_raw = ""

                if not content or len(content) < 2:
                    continue
                records.append(("user" if sender else "assistant", _normalize_ts(ts_raw), content))
            break

    return records


def _normalize_ts(ts_raw):
    """Normalize various timestamp formats to ISO."""
    if not ts_raw:
        return datetime.now().isoformat()

    ts_str = str(ts_raw).strip()

    # Unix timestamp
    try:
        ts_num = float(ts_str)
        if ts_num > 1e12:
            ts_num /= 1000
        return datetime.fromtimestamp(ts_num).isoformat()
    except (ValueError, OSError):
        pass

    # Common formats
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%d/%m/%Y %H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%H:%M:%S",
        "%H:%M",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(ts_str[:len("2024-01-01 12:00:00")], fmt)
            # For time-only formats, use today's date
            if fmt in ("%H:%M:%S", "%H:%M"):
                today = datetime.now()
                dt = dt.replace(year=today.year, month=today.month, day=today.day)
            return dt.isoformat()
        except ValueError:
            continue

    return ts_str  # Return as-is if no format matches


def convert(input_text, fmt, output_path, session_id=None):
    """Convert input text to session-digger JSONL."""
    if fmt == "auto":
        fmt = detect_format(input_text, output_path)

    # Parse
    if fmt == "wechat":
        records = parse_wechat_json(input_text)
    elif fmt == "json":
        records = parse_generic_json(input_text)
    elif fmt == "csv":
        records = parse_csv_file(input_text)
    elif fmt == "transcript":
        records = parse_transcript(input_text)
    else:
        print(f"Unknown format: {fmt}", file=sys.stderr)
        sys.exit(1)

    if not records:
        print(f"No records parsed from input (format={fmt}).", file=sys.stderr)
        sys.exit(1)

    # Generate session ID from filename if not provided
    if not session_id:
        session_id = Path(output_path).stem

    # Write JSONL
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as f:
        # Write header record (session metadata)
        # Privacy marker: external imports carry a privacy flag so downstream
        # analysis can avoid leaking raw content to LLM summaries without consent.
        privacy = "plaintext_chat" if fmt in ("wechat", "transcript") else "imported"
        header = {
            "type": "system",
            "timestamp": records[0][1] if records else datetime.now().isoformat(),
            "sessionId": session_id,
            "version": "dialog-adapter-v1.0",
            "subtype": "import",
            "source_format": fmt,
            "privacy": privacy,
        }
        f.write(json.dumps(header, ensure_ascii=False) + "\n")

        # Write messages
        for msg_type, ts, content in records:
            rec = make_record(msg_type, ts, content, session_id=session_id)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    return len(records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Universal conversation importer")
    parser.add_argument("input_file", help="Input file path")
    parser.add_argument("--format", default="auto",
                        choices=["auto", "wechat", "json", "csv", "transcript"],
                        help="Input format")
    parser.add_argument("-o", "--output", default=None, help="Output JSONL path")
    parser.add_argument("--session-id", default=None, help="Session ID for output")

    args = parser.parse_args()

    input_path = Path(args.input_file)
    if not input_path.exists():
        print(f"Input file not found: {args.input_file}", file=sys.stderr)
        sys.exit(1)

    input_text = input_path.read_text(encoding="utf-8", errors="replace")

    output = args.output or str(
        Path.home() / ".claude" / "projects" / "_imported" / f"{input_path.stem}.jsonl"
    )

    n = convert(input_text, args.format, output, session_id=args.session_id)
    print(f"Converted {n} messages → {output}")
    print(f"Next: sd-recall search <keyword>  OR  index-builder.py build")
