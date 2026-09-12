#!/usr/bin/env python3
"""
wx-cli bridge — table-driven surface for jackwener/wx-cli 0.3.x.

设计（对齐 session-digger）：
- 不 vendor wx 二进制；PATH / WECHAT_WX_BIN 探测
- WX_SURFACE 一行扩一类命令
- FLAG_VARIANTS 消灭 --json vs --format 版本漂移
- 就绪探测：binary + (daemon 可响应 | probe sessions)，不只看 WeChat 进程
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Optional
import time

_PROBE_CACHE: dict = {"t": 0.0, "v": None}
_PROBE_TTL = 30.0


# 一行扩展一类 wx 命令：name → argv 前缀（不含 json 旗标）
WX_SURFACE: dict[str, dict[str, Any]] = {
    "sessions": {"argv": ["sessions"], "needs_daemon": True},
    "history": {"argv": ["history"], "needs_daemon": True, "positional": True},
    "search": {"argv": ["search"], "needs_daemon": True, "positional": True},
    "contacts": {"argv": ["contacts"], "needs_daemon": True},
    "export": {"argv": ["export"], "needs_daemon": True, "positional": True},
    "unread": {"argv": ["unread"], "needs_daemon": True},
    "members": {"argv": ["members"], "needs_daemon": True, "positional": True},
    "new-messages": {"argv": ["new-messages"], "needs_daemon": True},
    "stats": {"argv": ["stats"], "needs_daemon": True, "positional": True},
    "favorites": {"argv": ["favorites"], "needs_daemon": True},
    "sns-feed": {"argv": ["sns-feed"], "needs_daemon": True},
    "sns-search": {"argv": ["sns-search"], "needs_daemon": True, "positional": True},
    "sns-notifications": {"argv": ["sns-notifications"], "needs_daemon": True},
    "biz-articles": {"argv": ["biz-articles"], "needs_daemon": True},
    "attachments": {"argv": ["attachments"], "needs_daemon": True, "positional": True},
    "extract": {"argv": ["extract"], "needs_daemon": True, "positional": True},
    "init": {"argv": ["init"], "needs_daemon": False},
    "daemon-status": {"argv": ["daemon", "status"], "needs_daemon": False},
    "daemon-stop": {"argv": ["daemon", "stop"], "needs_daemon": False},
    "daemon-logs": {"argv": ["daemon", "logs"], "needs_daemon": False},
}


# 按 digger 逻辑 op 选引擎：一行消灭「源 ready 但 op 不支持」
# engines 按优先级排列
CAPABILITY_MATRIX: dict[str, list[str]] = {
    "search": ["fts", "vault", "wxcli"],
    "history": ["vault", "wxcli", "fts"],
    "sessions": ["vault", "wxcli"],
    "contacts": ["vault", "wxcli"],
    "export": ["vault", "wxcli"],
    "unread": ["vault", "wxcli"],
    "members": ["vault", "wxcli"],
    "new-messages": ["vault", "wxcli"],
    "stats": ["vault", "wxcli"],
    "favorites": ["vault", "wxcli"],
    "moments": ["vault", "wxcli"],  # vault moments | wx sns-feed
    "sns-feed": ["wxcli"],
    "sns-search": ["wxcli"],
    "sns-notifications": ["wxcli"],
    "biz-articles": ["wxcli"],
    "attachments": ["wxcli"],
    "extract": ["wxcli"],
}


def wx_bin() -> Optional[str]:
    env = os.environ.get("WECHAT_WX_BIN")
    if env and os.path.isfile(os.path.expanduser(env)):
        return os.path.expanduser(env)
    return shutil.which("wx") or shutil.which("wx-cli")


def wechat_process_running() -> bool:
    try:
        r = subprocess.run(
            ["pgrep", "-f", "WeChat"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def wx_version(bin_path: Optional[str] = None) -> Optional[str]:
    b = bin_path or wx_bin()
    if not b:
        return None
    try:
        r = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=10)
        text = (r.stdout or r.stderr or "").strip()
        m = re.search(r"(\d+\.\d+\.\d+)", text)
        return m.group(1) if m else (text[:40] or None)
    except Exception:
        return None


def _run(argv: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    # start_new_session so timeout can kill hung wx-daemon children
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        start_new_session=True,
    )


def unwrap_payload(raw: Any) -> Any:
    """兼容 wx 0.3 {results|data|messages|...} wrapper；保留 meta 若存在。"""
    if not isinstance(raw, dict):
        return raw
    if "error" in raw and not any(k in raw for k in ("results", "data", "messages", "items")):
        return raw
    for key in ("results", "messages", "data", "items", "sessions", "contacts", "articles", "favorites"):
        if key in raw and raw[key] is not None:
            return raw[key]
    return raw


def parse_json_output(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return []
    # 0.3.x 会在 JSON 前输出「[wx] 警告：…」行，先剥离再解析
    cleaned = "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("[wx]")
    ).strip()
    for candidate in (cleaned, text):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") or line.startswith("["):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"error": "json_decode", "message": text[:300]}


def run_wx(
    op: str,
    extra: Optional[list[str]] = None,
    *,
    timeout: int = 60,
    as_json: bool = True,
) -> dict[str, Any]:
    """
    执行 WX_SURFACE 中的 op。
    返回 {ok, op, data?, meta?, error?, stdout?, code?}
    """
    b = wx_bin()
    if not b:
        return {
            "ok": False,
            "error": "wx_not_found",
            "fact": "PATH 中未找到 wx / wx-cli",
            "action": "安装 wx-cli 或 export WECHAT_WX_BIN=/path/to/wx",
            "forbidden": "不要编造会话数据。",
        }
    if op not in WX_SURFACE:
        return {
            "ok": False,
            "error": "unknown_op",
            "fact": f"未知 wx op: {op}",
            "action": f"可用: {', '.join(sorted(WX_SURFACE))}",
            "forbidden": "不要调用未注册命令。",
        }
    meta_cmd = WX_SURFACE[op]
    # 需 daemon 的 op：未就绪则快失败，避免 wx 拉起 daemon 挂死
    if meta_cmd.get("needs_daemon", True) and op not in ("init",):
        pr = probe_ready(timeout=min(5, timeout))
        if not pr.get("ready"):
            return {
                "ok": False,
                "error": "wx_not_ready",
                "fact": pr.get("detail") or "wx-daemon 未就绪",
                "action": "sudo wx init（可能需 codesign）后 wx daemon status；聊天分析请用 --source vault",
                "forbidden": "不要编造会话/朋友圈/公众号数据。",
                "wx": {"version": pr.get("version"), "binary": pr.get("binary")},
            }
    argv = [b, *meta_cmd["argv"], *(extra or [])]
    if as_json and "--json" not in argv and op not in ("init", "daemon-status", "daemon-stop", "daemon-logs"):
        # 0.3 统一 --json；禁止 --format json（会报 unexpected argument）
        argv.append("--json")

    try:
        r = _run(argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "timeout",
            "fact": f"wx {op} 超时 ({timeout}s)",
            "action": "检查 wx daemon：wx daemon status；必要时 sudo wx init",
            "forbidden": "不要并发狂刷 init。",
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "error": "wx_not_found",
            "fact": "wx 二进制消失",
            "action": "重新安装 wx-cli",
            "forbidden": "不要编造数据。",
        }

    out: dict[str, Any] = {"ok": r.returncode == 0, "op": op, "code": r.returncode, "bin": b}
    raw_text = r.stdout or ""
    err_text = r.stderr or ""

    if as_json and op not in ("init", "daemon-status", "daemon-stop", "daemon-logs"):
        parsed = parse_json_output(raw_text if raw_text.strip() else err_text)
        if isinstance(parsed, dict) and parsed.get("error") == "json_decode":
            out["ok"] = False
            out["error"] = "wx_bad_output"
            out["fact"] = (err_text or raw_text or "wx returned non-json")[:400]
            out["action"] = "wx daemon status；未就绪则 sudo wx init；聊天分析可 --source vault"
            out["forbidden"] = "不要在失败时伪造结果。"
            return out
        if isinstance(parsed, dict) and parsed.get("error") and "results" not in parsed:
            out["ok"] = False
            out["error"] = "wx_error"
            out["fact"] = str(parsed.get("error") or parsed.get("message") or parsed)[:400]
            out["action"] = "wx daemon status；未 init 则 sudo wx init；或改用 --source vault"
            out["forbidden"] = "不要在失败时伪造结果。"
            out["raw"] = parsed
            return out
        if isinstance(parsed, dict) and "meta" in parsed:
            out["meta"] = parsed.get("meta")
        out["data"] = unwrap_payload(parsed)
    else:
        out["stdout"] = raw_text
        out["stderr"] = err_text
        if r.returncode != 0:
            out["ok"] = False
            out["error"] = "command_failed"
            out["fact"] = (err_text or raw_text)[:400]
            out["action"] = "查看 wx 输出；init 可能需要 sudo/codesign"
            out["forbidden"] = "不要打印密钥。"

    if r.returncode != 0 and out.get("ok"):
        out["ok"] = False
        out["error"] = "command_failed"
        out["fact"] = (err_text or raw_text)[:400]
        out["action"] = "检查参数与 daemon；或改用 vault"
        out["forbidden"] = "不要忽略非零退出码。"
    if not out.get("ok") and not out.get("fact"):
        out["fact"] = (err_text or raw_text or "wx command failed")[:400]
        out["action"] = out.get("action") or "wx daemon status / sudo wx init / --source vault"
        out["forbidden"] = "不要编造数据。"
        out["error"] = out.get("error") or "wx_failed"
    return out


def _finish_probe(report: dict[str, Any]) -> dict[str, Any]:
    report["ready"] = bool(report.get("probe_ok"))
    if report["ready"]:
        report["detail"] = f"wx {report.get('version')} probe ok"
    elif not report.get("binary"):
        report["detail"] = report.get("detail") or "wx-cli not in PATH"
    elif not report.get("wechat_process"):
        report["detail"] = "WeChat process not running; wx needs init/daemon"
    else:
        report["detail"] = report.get("probe_error") or report.get("daemon_status_text") or "daemon not ready (try: sudo wx init)"
    _PROBE_CACHE["t"] = time.time()
    _PROBE_CACHE["v"] = dict(report)
    return report


def probe_ready(timeout: int = 5) -> dict[str, Any]:
    """探测 wx 是否真正可用（非仅 PATH）。daemon 未就绪时不做 sessions 长探测。"""
    now = time.time()
    if _PROBE_CACHE["v"] is not None and now - _PROBE_CACHE["t"] < _PROBE_TTL:
        return dict(_PROBE_CACHE["v"])
    b = wx_bin()
    report: dict[str, Any] = {
        "binary": b,
        "version": wx_version(b) if b else None,
        "wechat_process": wechat_process_running(),
        "daemon_ok": False,
        "probe_ok": False,
        "ready": False,
        "detail": "",
    }
    if not b:
        report["detail"] = "wx-cli not in PATH"
        return _finish_probe(report)

    # daemon status only (fast). sessions probe can hang if daemon auto-start loops.
    try:
        r = _run([b, "daemon", "status"], timeout=min(4, timeout))
        status_text = (r.stdout or r.stderr or "").strip()
        report["daemon_status_text"] = status_text[:200]
        low = status_text.lower()
        report["daemon_ok"] = r.returncode == 0 and (
            "running" in low
            or "就绪" in status_text
            or "运行中" in status_text
            or "active" in low
            or "ok" in low
        )
        if not report["daemon_ok"]:
            report["probe_error"] = status_text[:200] or "daemon not running"
            return _finish_probe(report)
    except subprocess.TimeoutExpired:
        report["probe_error"] = "daemon status timeout"
        return _finish_probe(report)
    except Exception as e:
        report["probe_error"] = str(e)[:120]
        return _finish_probe(report)

    # daemon claims ok → light sessions probe
    try:
        r = _run([b, "sessions", "-n", "1", "--json"], timeout=timeout)
        if r.returncode == 0:
            parsed = parse_json_output(r.stdout or "")
            if isinstance(parsed, dict) and parsed.get("error") and "results" not in parsed:
                report["probe_ok"] = False
                report["probe_error"] = str(parsed.get("error"))[:200]
            else:
                report["probe_ok"] = True
        else:
            report["probe_ok"] = False
            report["probe_error"] = ((r.stderr or r.stdout) or "")[:200]
    except subprocess.TimeoutExpired:
        report["probe_ok"] = False
        report["probe_error"] = "sessions probe timeout"
    except Exception as e:
        report["probe_ok"] = False
        report["probe_error"] = str(e)[:200]

    return _finish_probe(report)


def inventory() -> dict[str, Any]:
    p = probe_ready()
    return {
        "engine": "wxcli",
        "surface": sorted(WX_SURFACE.keys()),
        "capability_ops": sorted(CAPABILITY_MATRIX.keys()),
        **p,
    }


def engines_for(op: str) -> list[str]:
    return list(CAPABILITY_MATRIX.get(op, ["vault", "wxcli"]))


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="wx-cli bridge")
    ap.add_argument("op", nargs="?", default="info")
    ap.add_argument("rest", nargs=argparse.REMAINDER)
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args(argv)
    if args.op in (None, "info", "probe"):
        print(json.dumps(inventory() if args.op != "probe" else probe_ready(), ensure_ascii=False, indent=2))
        return 0
    rest = list(args.rest or [])
    if rest and rest[0] == "--":
        rest = rest[1:]
    result = run_wx(args.op, rest, timeout=args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
