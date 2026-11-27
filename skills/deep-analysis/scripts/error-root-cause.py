#!/usr/bin/env python3
"""
error-root-cause.py — Extract error root causes across all session-digger environments.

Delegates transcript/DB/zstd parsing to ``echolib`` + ``skills/common_paths.py``.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SKILLS_DIR = _REPO_ROOT / "skills"
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _p in (_SCRIPTS_DIR, _SKILLS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import common_paths  # noqa: E402
import echolib  # noqa: E402
from index_builder._reader import is_virtual_path  # noqa: E402

ERROR_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("SQLITE_LOCK_OR_CORRUPT", re.compile(
        r"database is locked|database table is locked|database disk image is malformed|"
        r"SQLITE_BUSY|SQLITE_CORRUPT|unable to open database file",
        re.IGNORECASE,
    )),
    ("GIT_ERROR", re.compile(
        r"fatal:\s*(?:Unable to create|not a git repository|bad object|Authentication failed)|"
        r"\.git/index\.lock|CONFLICT \(|merge conflict|non-fast-forward",
        re.IGNORECASE,
    )),
    ("PARSE_OR_FORMAT_ERROR", re.compile(
        r"JSONDecodeError|Expecting value:|YAMLError|TOMLDecodeError|"
        r"UnicodeDecodeError|invalid literal for int|Syntax error in JSON",
        re.IGNORECASE,
    )),
    ("POLICY_OR_TOOL_BLOCKED", re.compile(
        r"pre-?tool hook|privacy hook|policy blocked|hook blocked|"
        r"not allowed by policy|tool is disabled|outside.*workspace",
        re.IGNORECASE,
    )),
    ("COMMAND_NOT_FOUND", re.compile(
        r"command not found|not recognized as an internal|No such file or directory.*"
        r"(?:bin|usr|executable)|zsh:\d+:\s*command not found",
        re.IGNORECASE,
    )),
    ("PERMISSION_DENIED", re.compile(
        r"Permission denied|EACCES|Operation not permitted|Access is denied|not authorized",
        re.IGNORECASE,
    )),
    ("TIMEOUT", re.compile(
        r"timeout|timed out|deadline exceeded|ETIMEDOUT|Command killed after",
        re.IGNORECASE,
    )),
    ("CONTEXT_LIMIT", re.compile(
        r"context.?length|maximum context|token limit|prompt is too long|context_window",
        re.IGNORECASE,
    )),
    ("RATE_LIMIT", re.compile(
        r"rate limit|429|too many requests|throttl|quota exceeded",
        re.IGNORECASE,
    )),
    ("EISDIR", re.compile(
        r"EISDIR|is a directory|illegal operation on a directory",
        re.IGNORECASE,
    )),
    ("FILE_NOT_FOUND", re.compile(
        r"File does not exist|File not found|No such file|ENOENT|String to replace not found",
        re.IGNORECASE,
    )),
    ("USER_CANCELLED", re.compile(
        r"User cancelled|Request interrupted|user rejected|cancelled by user",
        re.IGNORECASE,
    )),
    ("TEST_FAILURE", re.compile(
        r"AssertionError|FAILED \(|pytest.*failed|\d+ failed",
        re.IGNORECASE,
    )),
    ("SYNTAX_OR_TYPE_ERROR", re.compile(
        r"SyntaxError|TypeError|AttributeError|KeyError|ImportError|ModuleNotFoundError|Traceback \(most recent call last\)",
        re.IGNORECASE,
    )),
    ("NETWORK_ERROR", re.compile(
        r"ECONNREFUSED|ECONNRESET|ENOTFOUND|ConnectionError|SSL|Redirect changed host|HTTP [45]\d\d",
        re.IGNORECASE,
    )),
    ("TOOL_ARG_ERROR", re.compile(
        r"invalid argument|missing required|schema validation|Target must exactly match",
        re.IGNORECASE,
    )),
]


def classify_error(message: str) -> str:
    """Classify an error message into a root cause category."""
    if not message:
        return "UNKNOWN"
    for category, pattern in ERROR_PATTERNS:
        if pattern.search(message):
            return category
    return "UNKNOWN"


def resolve_jsonl_path(session_id: str) -> Optional[str]:
    """Resolve session_id -> transcript path or virtual URI via shared common_paths."""
    return common_paths.resolve_session_ref(session_id)


def detect_agent(jsonl_path: str) -> str:
    """Detect which agent produced this session via echolib."""
    return echolib.dispatch_resolve_agent(jsonl_path)


def extract_errors_from_tools(tools: Iterable[Dict[str, Any]], agent: str) -> List[Dict[str, Any]]:
    """Normalize & classify error tool records (shared by error-root-cause & intent-classify)."""
    out: List[Dict[str, Any]] = []
    for t in tools:
        if t.get("status") not in ("error", "failed") and not t.get("is_error"):
            continue
        msg = (t.get("result_preview") or t.get("error") or "").strip()
        if not msg:
            msg = (t.get("key_input") or "").strip() or f"tool {t.get('name', 'unknown')} failed"
        out.append({
            "tool": t.get("name") or "unknown",
            "error_type": classify_error(msg),
            "error_message": msg[:500],
            "timestamp": t.get("timestamp") or "",
            "agent": agent,
        })
    return out


def extract_errors(jsonl_path: str) -> List[Dict[str, Any]]:
    """Extract all tool errors from any supported session via echolib."""
    agent = detect_agent(jsonl_path)
    tools = echolib.dispatch_extract_tools(jsonl_path, errors_only=True, limit=0)
    return extract_errors_from_tools(tools, agent)


def summarize_errors(errors: List[Dict[str, Any]]) -> Dict[str, int]:
    summary: Dict[str, int] = {}
    for err in errors:
        et = err.get("error_type", "UNKNOWN")
        summary[et] = summary.get(et, 0) + 1
    return summary


_summarize_errors = summarize_errors


def main():
    parser = argparse.ArgumentParser(
        description="Extract error root causes across all session-digger environments"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--session", help="Session ID to analyze")
    group.add_argument("--jsonl", help="Direct path or virtual URI (zcode://, dimcode://)")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print output")
    args = parser.parse_args()

    jsonl_path = args.jsonl if args.jsonl else resolve_jsonl_path(args.session)
    if not jsonl_path:
        print(json.dumps({"error": f"Cannot resolve session: {args.session}", "errors": []}), file=sys.stderr)
        sys.exit(1)
    if not is_virtual_path(jsonl_path) and not os.path.exists(jsonl_path):
        print(json.dumps({"error": f"File not found: {jsonl_path}", "errors": []}), file=sys.stderr)
        sys.exit(1)

    errors = extract_errors(jsonl_path)
    output = {
        "session_path": jsonl_path,
        "agent": detect_agent(jsonl_path),
        "total_errors": len(errors),
        "errors": errors,
        "summary": summarize_errors(errors),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2 if args.pretty else None))


if __name__ == "__main__":
    main()
