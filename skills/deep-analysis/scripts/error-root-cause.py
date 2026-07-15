#!/usr/bin/env python3
"""
error-root-cause.py — Extract error root causes from session JSONL files.

Supports: Claude Code, Grok Build, Kimi Code, Codex.

Usage:
    python3 error-root-cause.py --session <session_id>
    python3 error-root-cause.py --jsonl <path>

Output: JSON array of error objects:
    [{tool, error_type, error_message, timestamp, agent}]

error_type: COMMAND_NOT_FOUND | PERMISSION_DENIED | TIMEOUT | EISDIR |
            FILE_NOT_FOUND | RATE_LIMIT | UNKNOWN
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Error classification patterns
# ---------------------------------------------------------------------------

ERROR_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("COMMAND_NOT_FOUND", re.compile(
        r"command not found|No such file or directory.*executable|"
        r"not recognized as an internal or external command|"
        r"zsh:\s*(command not found|127)|bash:\s*.*:\s*command not found|"
        r"\w+:\s*command not found",
        re.IGNORECASE
    )),
    ("PERMISSION_DENIED", re.compile(
        r"Permission denied|EACCES|access denied|Operation not permitted|"
        r"EPERM",
        re.IGNORECASE
    )),
    ("TIMEOUT", re.compile(
        r"timeout|timed out|deadline exceeded|ETIMEDOUT|"
        r"execution took longer",
        re.IGNORECASE
    )),
    ("EISDIR", re.compile(
        r"EISDIR|is a directory.*read|readdit_is_a_directory",
        re.IGNORECASE
    )),
    ("FILE_NOT_FOUND", re.compile(
        r"File does not exist|File not found|No such file \(or directory\)|"
        r"ENOENT|String to replace not found",
        re.IGNORECASE
    )),
    ("RATE_LIMIT", re.compile(
        r"rate limit|429|too many requests|throttl|quota exceeded",
        re.IGNORECASE
    )),
]


def classify_error(message: str) -> str:
    """Classify an error message into a canonical error_type."""
    for error_type, pattern in ERROR_PATTERNS:
        if pattern.search(message):
            return error_type
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Session ID resolver
# ---------------------------------------------------------------------------

def resolve_jsonl_path(session_id: str) -> Optional[str]:
    """Resolve a session_id to a JSONL file path across known environments."""
    # Claude Code
    claude_root = Path.home() / ".claude" / "projects"
    if claude_root.exists():
        for project_dir in claude_root.iterdir():
            candidate = project_dir / f"{session_id}.jsonl"
            if candidate.exists():
                return str(candidate)

    # Grok Build
    grok_root = Path.home() / ".grok" / "sessions"
    if grok_root.exists():
        for cwd_dir in grok_root.iterdir():
            for session_dir in cwd_dir.iterdir():
                chat_file = session_dir / "chat_history.jsonl"
                if chat_file.exists():
                    # Check if session_id matches the directory name
                    if session_dir.name == session_id:
                        return str(chat_file)

    # Kimi Code
    kimi_root = Path.home() / ".kimi" / "sessions"
    if kimi_root.exists():
        for project_dir in kimi_root.iterdir():
            for session_dir in project_dir.iterdir():
                wire_file = session_dir / "wire.jsonl"
                if wire_file.exists() and session_dir.name == session_id:
                    return str(wire_file)

    # Codex
    codex_root = Path.home() / ".codex" / "sessions"
    if codex_root.exists():
        for year_dir in codex_root.iterdir():
            if year_dir.is_dir() and year_dir.name.isdigit():
                for month_dir in year_dir.iterdir():
                    if month_dir.is_dir() and month_dir.name.isdigit():
                        for day_dir in month_dir.iterdir():
                            if day_dir.is_dir() and day_dir.name.isdigit():
                                for jsonl_file in day_dir.glob("*.jsonl"):
                                    if session_id in jsonl_file.name:
                                        return str(jsonl_file)

    return None


# ---------------------------------------------------------------------------
# Claude Code parser
# ---------------------------------------------------------------------------

def parse_claude_code(jsonl_path: str) -> Generator[Dict[str, Any], None, None]:
    """Extract errors from Claude Code JSONL format.

    Claude Code stores errors as user messages with tool_result blocks
    where is_error=true. The corresponding tool name is found by matching
    tool_use_id back to the parent assistant message.
    """
    # Build a map of tool_use_id -> tool name from assistant messages
    tool_name_map: Dict[str, str] = {}
    error_results: Dict[str, Dict[str, Any]] = {}

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            record_type = record.get("type")
            message = record.get("message", {})
            role = message.get("role")

            # Collect tool names from assistant tool_use blocks
            if record_type == "assistant" and role == "assistant":
                content = message.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if block.get("type") == "tool_use":
                            tool_id = block.get("id", "")
                            if tool_id:
                                tool_name_map[tool_id] = block.get("name", "unknown")

            # Collect error results from user tool_result blocks
            if record_type == "user" and role == "user":
                content = message.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if block.get("type") == "tool_result" and block.get("is_error"):
                            tool_id = block.get("tool_use_id", "")
                            error_text = block.get("content", "")
                            if isinstance(error_text, dict):
                                error_text = json.dumps(error_text)
                            error_results[tool_id] = {
                                "error_message": str(error_text),
                                "timestamp": record.get("timestamp", ""),
                                "parent_uuid": record.get("parentUuid", ""),
                            }

    # Yield joined results
    for tool_id, err_info in error_results.items():
        tool = tool_name_map.get(tool_id, "unknown")
        error_type = classify_error(err_info["error_message"])
        yield {
            "tool": tool,
            "error_type": error_type,
            "error_message": err_info["error_message"],
            "timestamp": err_info["timestamp"],
            "agent": "claude_code",
        }


# ---------------------------------------------------------------------------
# Grok Build parser
# ---------------------------------------------------------------------------

def parse_grok(jsonl_path: str) -> Generator[Dict[str, Any], None, None]:
    """Extract errors from Grok Build chat_history.jsonl format.

    Grok stores tool_use and tool_result as separate records.
    tool_result has no is_error flag; errors are detected from content text.
    """
    tool_name_map: Dict[str, str] = {}
    timestamp_map: Dict[str, str] = {}
    error_results: List[Dict[str, Any]] = []
    # Track seen tool_call_ids to detect missing results
    all_tool_ids: Dict[str, Dict[str, Any]] = {}

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            record_type = record.get("type")

            if record_type == "tool_use":
                tool_id = record.get("tool_call_id") or record.get("id", "")
                tool_name = record.get("name", "")
                if tool_id:
                    tool_name_map[tool_id] = tool_name
                    all_tool_ids[tool_id] = {
                        "name": tool_name,
                        # Grok doesn't have timestamp per tool_use here
                    }

            elif record_type == "tool_result":
                tool_id = record.get("tool_call_id", "")
                content = record.get("content", "")
                if isinstance(content, dict):
                    content = json.dumps(content)

                # Detect errors in content
                is_error = _is_error_content(content)
                if is_error:
                    error_results.append({
                        "tool_call_id": tool_id,
                        "error_message": str(content),
                        "timestamp": record.get("timestamp", ""),
                    })

    for err in error_results:
        tool = tool_name_map.get(err["tool_call_id"], "unknown")
        error_type = classify_error(err["error_message"])
        yield {
            "tool": tool,
            "error_type": error_type,
            "error_message": err["error_message"],
            "timestamp": err["timestamp"],
            "agent": "grok",
        }


def _is_error_content(content: str) -> bool:
    """Heuristic: does the tool_result content indicate an error?"""
    error_indicators = [
        "error", "failed", "failure", "fatal", "traceback",
        "exception", "cannot", "could not", "unable to",
    ]
    content_lower = content.lower().strip()
    # Positive patterns that override (these are normal output)
    normal_indicators = ["successfully", "done", "completed"]

    if any(n in content_lower for n in normal_indicators):
        return False

    # Check for explicit error markers
    if content.strip().startswith("<tool_use_error"):
        return True
    if "Error:" in content or "error:" in content_lower[:100]:
        return True
    if "Failed" in content or "failed" in content_lower[:100]:
        return True

    return any(indicator in content_lower for indicator in error_indicators)


# ---------------------------------------------------------------------------
# Kimi Code parser
# ---------------------------------------------------------------------------

def parse_kimi(wire_path: str) -> Generator[Dict[str, Any], None, None]:
    """Extract errors from Kimi Code wire.jsonl format.

    Kimi stores ToolResult messages with return_value.is_error flag.
    Tool name must be looked up via tool_call_id from ToolCall messages.
    """
    tool_name_map: Dict[str, str] = {}

    with open(wire_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            message = record.get("message", {})
            msg_type = message.get("type")
            payload = message.get("payload", {})

            if msg_type == "ToolCall":
                tool_id = payload.get("tool_call_id", "")
                tool_name = payload.get("tool_name") or payload.get("name", "")
                if tool_id:
                    tool_name_map[tool_id] = tool_name

            elif msg_type == "ToolResult":
                return_value = payload.get("return_value", {})
                if return_value.get("is_error"):
                    tool_id = payload.get("tool_call_id", "")
                    output = return_value.get("output", "")
                    if isinstance(output, dict):
                        output = json.dumps(output)
                    error_type = classify_error(str(output))
                    yield {
                        "tool": tool_name_map.get(tool_id, "unknown"),
                        "error_type": error_type,
                        "error_message": str(output),
                        "timestamp": record.get("timestamp", ""),
                        "agent": "kimi_code",
                    }


# ---------------------------------------------------------------------------
# Codex parser
# ---------------------------------------------------------------------------

def parse_codex(jsonl_path: str) -> Generator[Dict[str, Any], None, None]:
    """Extract errors from Codex JSONL format.

    Codex embeds tool calls/responses in assistant messages as text blocks
    using [external_agent_tool_call] / [external_agent_result] tags.
    Errors appear as [external_agent_result: error] or contain error text.
    """
    current_tool_name: Optional[str] = None

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            record_type = record.get("type")
            if record_type != "response_item":
                continue

            payload = record.get("payload", {})
            if not isinstance(payload, dict):
                continue
            if payload.get("type") != "message":
                continue

            content = payload.get("content", [])
            if not isinstance(content, list):
                continue

            for block in content:
                text = block.get("text", "")
                if not text:
                    continue

                # Parse tool call
                call_match = re.search(
                    r"\[external_agent_tool_call:\s*([^\]]+)\]\s*\n"
                    r"(?:description:\s*)?(.*?)\n"
                    r"\[/external_agent_tool_call\]",
                    text, re.DOTALL
                )
                if call_match:
                    current_tool_name = call_match.group(1).strip()
                    continue

                # Parse tool result with error
                result_match = re.search(
                    r"\[external_agent_result(?::\s*error)?\]\s*\n"
                    r"(.*?)\n"
                    r"\[/external_agent_result\]",
                    text, re.DOTALL
                )
                if result_match:
                    result_text = result_match.group(1)
                    is_error_marker = ": error" in text[:50]

                    # Detect if it's actually an error
                    if is_error_marker or _is_error_content(result_text):
                        error_type = classify_error(result_text)
                        tool = current_tool_name or "unknown"
                        yield {
                            "tool": tool,
                            "error_type": error_type,
                            "error_message": result_text,
                            "timestamp": record.get("timestamp", ""),
                            "agent": "codex",
                        }
                    current_tool_name = None


# ---------------------------------------------------------------------------
# Agent type detector
# ---------------------------------------------------------------------------

def detect_agent(jsonl_path: str) -> str:
    """Detect which agent produced this JSONL file."""
    path_lower = jsonl_path.lower()

    if ".codex" in path_lower:
        return "codex"
    if ".kimi" in path_lower:
        return "kimi_code"
    if ".grok" in path_lower:
        return "grok"

    # Claude Code: check the structure
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= 20:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "user":
                    msg = record.get("message", {})
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        content = msg.get("content", [])
                        if isinstance(content, list):
                            for block in content:
                                if block.get("type") == "tool_result":
                                    return "claude_code"
    except (IOError, OSError):
        pass

    return "unknown"


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

def extract_errors(jsonl_path: str) -> List[Dict[str, Any]]:
    """Extract all errors from a JSONL file, auto-detecting agent type."""
    agent = detect_agent(jsonl_path)

    if agent == "claude_code":
        return list(parse_claude_code(jsonl_path))
    elif agent == "grok":
        return list(parse_grok(jsonl_path))
    elif agent == "kimi_code":
        return list(parse_kimi(jsonl_path))
    elif agent == "codex":
        return list(parse_codex(jsonl_path))
    else:
        # Try all parsers and pick the one that finds the most results
        results = {
            "claude_code": list(parse_claude_code(jsonl_path)),
            "grok": list(parse_grok(jsonl_path)),
            "kimi_code": list(parse_kimi(jsonl_path)),
            "codex": list(parse_codex(jsonl_path)),
        }
        best_agent = max(results, key=lambda k: len(results[k]))
        return results[best_agent]


def main():
    parser = argparse.ArgumentParser(
        description="Extract error root causes from session JSONL files"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--session", help="Session ID to analyze")
    group.add_argument("--jsonl", help="Direct path to JSONL file")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print output")

    args = parser.parse_args()

    if args.jsonl:
        jsonl_path = args.jsonl
    else:
        resolved = resolve_jsonl_path(args.session)
        if not resolved:
            print(json.dumps({
                "error": f"Cannot resolve session: {args.session}",
                "errors": []
            }), file=sys.stderr)
            sys.exit(1)
        jsonl_path = resolved

    if not os.path.exists(jsonl_path):
        print(json.dumps({
            "error": f"File not found: {jsonl_path}",
            "errors": []
        }), file=sys.stderr)
        sys.exit(1)

    errors = extract_errors(jsonl_path)
    output = {
        "session_path": jsonl_path,
        "agent": detect_agent(jsonl_path),
        "total_errors": len(errors),
        "errors": errors,
        "summary": _summarize_errors(errors),
    }

    indent = 2 if args.pretty else None
    print(json.dumps(output, ensure_ascii=False, indent=indent))


def _summarize_errors(errors: List[Dict[str, Any]]) -> Dict[str, int]:
    """Count errors by type."""
    summary: Dict[str, int] = {}
    for err in errors:
        et = err.get("error_type", "UNKNOWN")
        summary[et] = summary.get(et, 0) + 1
    return summary


if __name__ == "__main__":
    main()
