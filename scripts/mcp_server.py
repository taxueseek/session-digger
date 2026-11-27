#!/usr/bin/env python3
"""mcp_server.py — Zero-dependency MCP stdio & CLI-call server for session-digger.

Usage:
  python3 scripts/mcp_server.py                    # Start MCP stdio server
  python3 scripts/mcp_server.py --call <tool> '{}' # Single-shot tool execution
  python3 scripts/mcp_server.py --test             # Offline smoke test
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
def _read_version() -> str:
    pkg = REPO_ROOT / "package.json"
    if pkg.is_file():
        try:
            ver = json.loads(pkg.read_text(encoding="utf-8")).get("version")
            if isinstance(ver, str) and ver.strip():
                return ver.strip()
        except Exception:
            pass
    return "0.9.39"


VERSION = _read_version()

TOOLS: list[dict[str, Any]] = [
    {
        "name": "sd_search",
        "description": (
            "Search past coding agent sessions across 20+ environments (Claude Code, Codex, "
            "Cursor, Grok, Kimi, Antigravity, DSH, etc.) using SQLite FTS5 or file scan."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Search keyword or CJK/English phrase"},
                "scope": {
                    "type": "string",
                    "enum": ["current", "all"],
                    "description": "Search current workspace ('current') or all projects ('all')",
                },
                "agent": {"type": "string", "description": "Filter by agent name or 'cross' (all)"},
                "limit": {"type": "integer", "description": "Max sessions to return (1-50)"},
                "decisions": {"type": "boolean", "description": "Focus on decision points to cut tokens"},
                "include_tools": {"type": "boolean", "description": "Also search tool output digests"},
                "cwd": {"type": "string", "description": "Project directory when scope='current'"},
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "sd_sessions",
        "description": "List recent AI coding sessions across environments with metadata and summary.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["current", "all"], "description": "Workspace scope"},
                "agent": {"type": "string", "description": "Agent filter or 'cross'"},
                "limit": {"type": "integer", "description": "Max sessions to list"},
                "keyword": {"type": "string", "description": "Optional text filter"},
                "cwd": {"type": "string", "description": "Project directory when scope='current'"},
            },
        },
    },
    {
        "name": "sd_stats",
        "description": "Get aggregate session counts, token usage, and environment breakdown.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "Agent filter or 'cross' for all"},
            },
        },
    },
    {
        "name": "sd_index",
        "description": "Build or incrementally update the local SQLite FTS5 index (~/.session-digger/index.db).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "rebuild": {"type": "boolean", "description": "Force full rebuild instead of incremental"},
                "agent": {"type": "string", "description": "Target agent or 'all'"},
            },
        },
    },
    {
        "name": "sd_trend",
        "description": "Run longitudinal trend or regression analysis over the indexed sessions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["period-over-period", "by-theme", "regressions"],
                    "description": "Analysis mode",
                },
                "unit": {"type": "string", "enum": ["week", "month"], "description": "Time bucket"},
                "lookback": {"type": "integer", "description": "Number of periods to inspect"},
                "group_by": {
                    "type": "string",
                    "enum": ["project", "tag", "agent"],
                    "description": "Grouping key for by-theme mode",
                },
            },
        },
    },
    {
        "name": "sd_reflect",
        "description": "Generate a self-contained offline HTML usage recap report (and optional JSON summary).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "months": {
                    "type": "integer",
                    "enum": [1, 3, 6, 12],
                    "description": "Default time window in the generated HTML report",
                },
                "lookback": {"type": "integer", "description": "Months of sessions to include in payload"},
                "open_browser": {"type": "boolean", "description": "Open the HTML report in default browser"},
            },
        },
    },
]

_TOOL_NAMES = {t["name"] for t in TOOLS}
_response_format = "content-length"


def _ok(payload: Any) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _err(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps({"error": message}, ensure_ascii=False)}], "isError": True}


def _run_py(script_name: str, args: list[str], cwd: str | None = None, timeout: int = 120) -> tuple[int, str, str]:
    script_path = SCRIPTS_DIR / script_name
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    proc = subprocess.run(
        [sys.executable, str(script_path), *args],
        cwd=cwd or str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _parse_json_or_text(raw: str) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def execute_tool(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    args = arguments if isinstance(arguments, dict) else {}
    if name not in _TOOL_NAMES:
        return _err(f"Unknown tool: {name}")

    try:
        if name == "sd_search":
            keyword = str(args.get("keyword", "")).strip()
            if not keyword:
                return _err("'keyword' is required")
            cmd = [
                "search",
                keyword,
                "--scope",
                str(args.get("scope") or "all"),
                "--limit",
                str(max(1, min(50, int(args.get("limit") or 5)))),
                "--agent",
                str(args.get("agent") or "cross"),
                "--json",
            ]
            if args.get("decisions"):
                cmd.append("--decisions")
            if args.get("include_tools"):
                cmd.append("--include-tools")
            rc, out, err = _run_py("sd-recall.py", cmd, cwd=args.get("cwd"))
            if rc != 0:
                return _err(err or out or f"sd-recall search exited {rc}")
            return _ok(_parse_json_or_text(out))

        if name == "sd_sessions":
            cmd = [
                "sessions",
                "--scope",
                str(args.get("scope") or "all"),
                "--limit",
                str(max(1, min(100, int(args.get("limit") or 20)))),
                "--agent",
                str(args.get("agent") or "cross"),
                "--json",
            ]
            if args.get("keyword"):
                cmd.append(str(args["keyword"]))
            rc, out, err = _run_py("sd-recall.py", cmd, cwd=args.get("cwd"))
            if rc != 0:
                return _err(err or out or f"sd-recall sessions exited {rc}")
            return _ok(_parse_json_or_text(out))

        if name == "sd_stats":
            cmd = ["stats", "--agent", str(args.get("agent") or "cross"), "--json"]
            rc, out, err = _run_py("sd-recall.py", cmd)
            if rc != 0:
                return _err(err or out or f"sd-recall stats exited {rc}")
            return _ok(_parse_json_or_text(out))

        if name == "sd_index":
            cmd = ["build"]
            if args.get("rebuild"):
                cmd.append("--rebuild")
            if args.get("agent"):
                cmd.extend(["--agent", str(args["agent"])])
            rc, out, err = _run_py("index-builder.py", cmd, timeout=300)
            if rc != 0:
                return _err(err or out or f"index-builder exited {rc}")
            return _ok(_parse_json_or_text(out))

        if name == "sd_trend":
            mode = str(args.get("mode") or "period-over-period")
            if mode == "by-theme":
                cmd = ["by-theme", "--group-by", str(args.get("group_by") or "project")]
            elif mode == "regressions":
                cmd = [
                    "regressions",
                    "--unit",
                    str(args.get("unit") or "week"),
                    "--lookback",
                    str(int(args.get("lookback") or 8)),
                ]
            else:
                cmd = [
                    "period-over-period",
                    "--unit",
                    str(args.get("unit") or "week"),
                    "--lookback",
                    str(int(args.get("lookback") or 8)),
                ]
            rc, out, err = _run_py("trend-engine.py", cmd)
            if rc != 0:
                return _err(err or out or f"trend-engine exited {rc}")
            return _ok(_parse_json_or_text(out))

        if name == "sd_reflect":
            months = int(args.get("months") or 3)
            if months not in (1, 3, 6, 12):
                months = 3
            cmd = ["--months", str(months), "--stats"]
            if args.get("lookback"):
                cmd.extend(["--lookback", str(int(args["lookback"]))])
            if args.get("open_browser"):
                cmd.append("--open")
            rc, out, err = _run_py("reflect-report.py", cmd, timeout=180)
            if rc != 0:
                return _err(err or out or f"reflect-report exited {rc}")
            return _ok({"status": "ok", "output": out})

    except subprocess.TimeoutExpired:
        return _err(f"Tool '{name}' timed out")
    except Exception as exc:
        return _err(str(exc))

    return _err(f"Unhandled tool: {name}")


def handle_rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    params = params or {}
    if method == "initialize":
        return {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "session-digger", "version": VERSION},
            "instructions": (
                "session-digger: offline cross-environment coding session search, "
                "token/cache analytics, and local HTML usage reports across 20+ AI agents. "
                "Run sd_index once before sd_search if the SQLite index does not exist yet."
            ),
        }
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        return execute_tool(params.get("name", ""), params.get("arguments"))
    if method == "ping":
        return {}
    if method.startswith("notifications/"):
        return None
    return {"error": {"code": -32601, "message": f"Method not found: {method}"}}


def _send_message(payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if _response_format == "ndjson":
        sys.stdout.buffer.write(encoded + b"\n")
    else:
        header = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii")
        sys.stdout.buffer.write(header + encoded)
    sys.stdout.buffer.flush()


def run_stdio() -> None:
    global _response_format
    sys.stderr.write("[session-digger-mcp] ready, waiting for stdin\n")
    sys.stderr.flush()
    while True:
        req_id = None
        try:
            line = sys.stdin.buffer.readline()
            if not line:
                break
            header = line.decode("utf-8", errors="replace").strip()
            if not header:
                continue
            if header.lower().startswith("content-length:"):
                _response_format = "content-length"
                length = int(header.split(":", 1)[1].strip())
                while True:
                    sep = sys.stdin.buffer.readline()
                    if not sep or sep in (b"\r\n", b"\n"):
                        break
                body = sys.stdin.buffer.read(length).decode("utf-8", errors="replace")
                req = json.loads(body)
            else:
                _response_format = "ndjson"
                req = json.loads(header)

            req_id = req.get("id")
            result = handle_rpc(req.get("method", ""), req.get("params"))
            if result is None:
                continue
            if "error" in result and len(result) == 1:
                _send_message({"jsonrpc": "2.0", "id": req_id, "error": result["error"]})
            else:
                _send_message({"jsonrpc": "2.0", "id": req_id, "result": result})
        except json.JSONDecodeError:
            _send_message({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32700, "message": "Parse error"}})
        except Exception as exc:
            _send_message({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(exc)}})


def _test_mode() -> int:
    init = handle_rpc("initialize", {})
    listed = handle_rpc("tools/list", {})
    assert init and init["serverInfo"]["name"] == "session-digger"
    assert listed and len(listed["tools"]) == len(TOOLS)
    unknown = execute_tool("nonexistent", {})
    assert unknown["isError"] is True
    print(json.dumps({"ok": True, "tools": [t["name"] for t in TOOLS], "version": VERSION}, indent=2))
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "--test":
        return _test_mode()
    if argv and argv[0] == "--call":
        if len(argv) < 2:
            sys.stderr.write("usage: mcp_server.py --call <tool> [json-args]\n")
            return 2
        payload = json.loads(argv[2]) if len(argv) > 2 else {}
        res = execute_tool(argv[1], payload)
        print(res["content"][0]["text"])
        return 1 if res.get("isError") else 0
    run_stdio()
    return 0


if __name__ == "__main__":
    sys.exit(main())
