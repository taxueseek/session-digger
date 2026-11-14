"""Tests for session-digger MCP server and bin/session-digger.js entry."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import mcp_server  # noqa: E402


def test_initialize_and_tools_list():
    init = mcp_server.handle_rpc("initialize", {})
    assert init["serverInfo"]["name"] == "session-digger"
    assert init["serverInfo"]["version"] == mcp_server.VERSION

    listed = mcp_server.handle_rpc("tools/list", {})
    names = [t["name"] for t in listed["tools"]]
    assert names == ["sd_search", "sd_sessions", "sd_stats", "sd_index", "sd_trend", "sd_reflect"]
    for tool in listed["tools"]:
        props = tool["inputSchema"].get("properties", {})
        for spec in props.values():
            assert "default" not in spec
            assert "minimum" not in spec


def test_tool_validation_and_unknown_tool():
    res_unknown = mcp_server.execute_tool("no_such_tool", {})
    assert res_unknown["isError"] is True

    res_empty = mcp_server.execute_tool("sd_search", {"keyword": ""})
    assert res_empty["isError"] is True


def test_node_cli_version_and_help():
    node = shutil.which("node")
    if not node:
        return
    ver = subprocess.run(
        [node, str(REPO_ROOT / "bin" / "session-digger.js"), "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert ver.stdout.strip() == mcp_server.VERSION

    hlp = subprocess.run(
        [node, str(REPO_ROOT / "bin" / "session-digger.js"), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "session-digger" in hlp.stdout
    assert "MCP stdio server" in hlp.stdout


def test_mcp_script_self_test():
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "mcp_server.py"), "--test"],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout)
    assert data["ok"] is True
