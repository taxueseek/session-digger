#!/usr/bin/env python3
"""
Bridge to embedded (or external) WeChat acquisition stack.

Does not re-implement SQLCipher crypto — dispatches to scripts/acquire/*.
Adding a new acquire operation = ACQUIRE_OPS 表加一行 + 可选 CLI 别名。
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from paths import (
    ACQUIRE_TOOLS,
    acquire_dir,
    acquire_tool,
    digger_python,
    digger_root,
    private_vault_dir,
    vault_cli_path,
)


# op → (tool_key, default_argv_prefix builder receives remaining args)
# 一行扩展一类采集动作
ACQUIRE_OPS = {
    "list-dbs": ("keys", ["--list-dbs"]),
    "keys-match": ("keys", ["--match-only", "--targets", "all", "--reuse-log"]),
    "keys-capture": ("keys", ["--targets", "all"]),  # may need --duration
    "decrypt-full": ("decrypt", ["--mode", "full"]),
    "decrypt-incremental": ("decrypt", ["--mode", "incremental"]),
    "vault-status": ("vault_cli", ["status"]),
    "vault-sessions": ("vault_cli", ["sessions"]),
    "vault-contacts": ("vault_cli", ["contacts"]),
    "vault-history": ("vault_cli", ["history"]),
    "vault-search": ("vault_cli", ["search"]),
    "vault-moments": ("vault_cli", ["moments"]),
    "vault-favorites": ("vault_cli", ["favorites"]),
    "vault-export": ("vault_cli", ["export"]),
    "vault-digest-source": ("vault_cli", ["digest-source"]),
    "vault-members": ("vault_cli", ["members"]),
    "vault-stats": ("vault_cli", ["stats"]),
    "vault-unread": ("vault_cli", ["unread"]),
    "vault-new-messages": ("vault_cli", ["new-messages"]),
    "vault-sns-feed": ("vault_cli", ["sns-feed"]),
    "vault-sns-search": ("vault_cli", ["sns-search"]),
    "vault-sns-notifications": ("vault_cli", ["sns-notifications"]),
    "vault-biz-articles": ("vault_cli", ["biz-articles"]),
    "export-chat": ("export_chat", []),
}

_VAULT_CLI_LOCK = threading.Lock()
_VAULT_CLI_MOD_CACHE: dict[tuple[str, int], Any] = {}


def _load_vault_cli_module(path: Path):
    resolved = str(path.resolve())
    st = path.stat()
    key = (resolved, st.st_mtime_ns)
    mod = _VAULT_CLI_MOD_CACHE.get(key)
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location("_wd_bundled_vault_cli", resolved)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module spec for {resolved}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _VAULT_CLI_MOD_CACHE.clear()
    _VAULT_CLI_MOD_CACHE[key] = mod
    return mod


def _can_run_vault_cli_inprocess(tool_key: str, path: Path, timeout: Optional[int]) -> bool:
    if tool_key != "vault_cli":
        return False
    if os.environ.get("WECHAT_ACQUIRE_SUBPROCESS") == "1":
        return False
    if digger_python() != sys.executable:
        return False
    if timeout is not None and timeout < 30:
        return False
    try:
        return path.resolve().parent == (digger_root() / "scripts" / "acquire").resolve()
    except OSError:
        return False


def _run_vault_cli_inprocess(path: Path, argv: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    rc = 0
    with _VAULT_CLI_LOCK:
        try:
            mod = _load_vault_cli_module(path)
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                mod.main(list(argv))
        except SystemExit as exc:
            code = exc.code
            if code is None:
                rc = 0
            elif isinstance(code, int):
                rc = code
            else:
                err_buf.write(str(code) + "\n")
                rc = 1
        except Exception as exc:
            err_buf.write(f"{type(exc).__name__}: {exc}\n")
            rc = 1
    cp = subprocess.CompletedProcess(
        args=[sys.executable, str(path), *argv],
        returncode=rc,
        stdout=out_buf.getvalue(),
        stderr=err_buf.getvalue(),
    )
    if check and rc != 0:
        raise subprocess.CalledProcessError(rc, cp.args, output=cp.stdout, stderr=cp.stderr)
    return cp


def inventory() -> dict:
    tools = {}
    for name, filename in ACQUIRE_TOOLS.items():
        path = acquire_tool(name)
        tools[name] = {
            "file": filename,
            "path": str(path) if path else None,
            "ready": path is not None and path.is_file(),
        }
    ad = acquire_dir()
    return {
        "acquire_dir": str(ad) if ad else None,
        "bundled": bool(ad and ad.name == "acquire"),
        "private_vault": str(private_vault_dir()),
        "decrypted_current": str(private_vault_dir() / "decrypted" / "current"),
        "keys_config": str(Path.home() / ".config" / "wechat-keys.json"),
        "vault_config": str(Path.home() / ".config" / "wechat-local-vault.json"),
        "keys_config_exists": (Path.home() / ".config" / "wechat-keys.json").exists(),
        "vault_config_exists": (Path.home() / ".config" / "wechat-local-vault.json").exists(),
        "decrypted_ready": (private_vault_dir() / "decrypted" / "current").exists(),
        "tools": tools,
        "ops": sorted(ACQUIRE_OPS.keys()),
    }


def run_tool(
    tool_key: str,
    argv: list[str],
    *,
    timeout: Optional[int] = None,
    check: bool = False,
) -> subprocess.CompletedProcess:
    path = acquire_tool(tool_key)
    if not path:
        raise FileNotFoundError(
            f"acquire tool '{tool_key}' not found. "
            f"Expected under scripts/acquire/ or WECHAT_ACQUIRE_DIR. "
            f"Run: python3 scripts/wd.py acquire-info"
        )
    if _can_run_vault_cli_inprocess(tool_key, path, timeout):
        return _run_vault_cli_inprocess(path, argv, check=check)
    cmd = [digger_python(), str(path), *argv]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def run_op(op: str, extra_argv: Optional[list[str]] = None, timeout: Optional[int] = None) -> dict:
    if op not in ACQUIRE_OPS:
        return {
            "error": "unknown_op",
            "fact": f"未知采集操作: {op}",
            "action": f"可用: {', '.join(sorted(ACQUIRE_OPS))}",
            "forbidden": "不要编造解密结果。",
        }
    tool_key, prefix = ACQUIRE_OPS[op]
    argv = list(prefix) + list(extra_argv or [])
    try:
        r = run_tool(tool_key, argv, timeout=timeout)
    except FileNotFoundError as e:
        return {
            "error": "tool_missing",
            "fact": str(e),
            "action": "确认 wechat-digger 含 scripts/acquire/，或设置 WECHAT_ACQUIRE_DIR",
            "forbidden": "不要跳过采集假装有数据。",
        }
    except subprocess.TimeoutExpired:
        return {
            "error": "timeout",
            "fact": f"操作 {op} 超时",
            "action": "增大 timeout 或检查微信/磁盘",
            "forbidden": "不要重复并发抓 key。",
        }

    out: dict[str, Any] = {
        "op": op,
        "tool": tool_key,
        "code": r.returncode,
        "stdout": r.stdout,
        "stderr": r.stderr,
    }
    # try parse json stdout for machine consumers
    text = (r.stdout or "").strip()
    if text:
        try:
            out["data"] = json.loads(text)
        except json.JSONDecodeError:
            # leave raw stdout
            pass
    if r.returncode != 0:
        out["error"] = "command_failed"
        out["fact"] = (r.stderr or r.stdout or f"exit {r.returncode}")[:500]
        out["action"] = "查看 stderr；keys 缺失时先 keys list-dbs / keys-match；库过期则 decrypt-incremental"
        out["forbidden"] = "不要在日志里打印完整 key/salt。"
    return out


def run_passthrough(tool_key: str, argv: list[str], timeout: Optional[int] = None) -> int:
    """Stream child stdout/stderr to parent (for long decrypt/key capture)."""
    path = acquire_tool(tool_key)
    if not path:
        print(
            json.dumps(
                {
                    "error": "tool_missing",
                    "fact": f"{tool_key} not found",
                    "action": "check scripts/acquire or WECHAT_ACQUIRE_DIR",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    cmd = [digger_python(), str(path), *argv]
    try:
        r = subprocess.run(cmd, timeout=timeout)
        return r.returncode
    except subprocess.TimeoutExpired:
        print("timeout", file=sys.stderr)
        return 124


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="wechat-digger acquire bridge")
    p.add_argument("op", nargs="?", help="operation name or 'info'")
    p.add_argument("rest", nargs=argparse.REMAINDER, help="args passed to underlying tool")
    p.add_argument("--timeout", type=int, default=None)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    if not args.op or args.op == "info":
        print(json.dumps(inventory(), ensure_ascii=False, indent=2))
        return 0

    # strip leading -- from remainder if argparse left it
    rest = list(args.rest or [])
    if rest and rest[0] == "--":
        rest = rest[1:]

    result = run_op(args.op, rest, timeout=args.timeout)
    if args.json or result.get("data") is not None or result.get("error"):
        # compact for json mode
        printable = {k: v for k, v in result.items() if k not in ("stdout", "stderr") or args.json}
        if args.json:
            printable = result
        print(json.dumps(printable, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(result.get("stdout") or "")
        if result.get("stderr"):
            sys.stderr.write(result["stderr"])
    return 0 if not result.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
