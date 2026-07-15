#!/usr/bin/env python3
"""
native-diag.py — 调用各 AI 编码环境的原生诊断命令，输出统一 JSON。

目标：把「原生诊断命令」的输出收拢成统一 schema，便于 session-digger 索引
和 env-doctor 消费。对无法在 CLI 调用的命令（如 Claude /doctor）给 fallback。

用法:
    python3 native-diag.py --env <claude|codex|grok|kimi|mimo|all> [--json]

退出码:
    0 = 全部 ok 或仅 warn
    1 = 存在 fail / critical
    2 = 环境未安装或诊断命令不可用
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

HOME = Path.home()

# ---------------------------------------------------------------------------
# 统一的 issue schema
# ---------------------------------------------------------------------------

def _mk_issue(severity: str, summary: str, remediation: Optional[str] = None,
              check_id: Optional[str] = None) -> dict:
    """构造一条标准化的 issue。"""
    out = {"severity": severity, "summary": summary}
    if remediation:
        out["remediation"] = remediation
    if check_id:
        out["check_id"] = check_id
    return out


def _empty_result(env: str, native_cmd: str, overall: str = "ok",
                  issues: Optional[list] = None, metrics: Optional[dict] = None,
                  notes: Optional[str] = None) -> dict:
    """构造一条环境结果骨架。"""
    out = {
        "env": env,
        "native_cmd": native_cmd,
        "overall_status": overall,
        "issues": issues or [],
        "metrics": metrics or {},
    }
    if notes:
        out["notes"] = notes
    return out


# ---------------------------------------------------------------------------
# 各环境诊断逻辑
# ---------------------------------------------------------------------------

def diag_claude() -> dict:
    """
    Claude Code /doctor 是会话内斜杠命令，CLI 无法直接调用。
    这里只输出 fallback 提示，并做一项静态探测：检查 ~/.claude 目录是否存在。
    """
    native_cmd = "/doctor (会话内斜杠命令)"
    issues = []
    metrics = {}

    claude_dir = HOME / ".claude"
    if claude_dir.exists():
        metrics["config_dir_exists"] = True
        settings = claude_dir / "settings.json"
        metrics["settings_exists"] = settings.exists()
    else:
        metrics["config_dir_exists"] = False
        issues.append(_mk_issue(
            severity="warning",
            summary="~/.claude 目录不存在，Claude Code 可能未配置或未使用此配置目录",
            remediation="在 Claude Code 会话内运行 /doctor 获取完整诊断"
        ))

    issues.append(_mk_issue(
        severity="ok",
        summary="Claude Code /doctor 必须在会话内调用，CLI 无法调度",
        remediation="在 Claude Code 会话中输入 /doctor, 然后手动把输出传给 session-digger 索引"
    ))

    return _empty_result(
        env="claude",
        native_cmd=native_cmd,
        overall="warn",
        issues=issues,
        metrics=metrics,
        notes="fallback: /doctor 是斜杠命令, 仅静态探测 config 目录"
    )


def diag_codex() -> dict:
    """调用 codex doctor --json，提取所有非 ok 的 check 项。"""
    native_cmd = "codex doctor --json"
    issues = []
    metrics = {}

    if not shutil.which("codex"):
        issues.append(_mk_issue(
            severity="warning",
            summary="codex 不在 PATH 中",
            remediation="安装或重装 Codex CLI"
        ))
        return _empty_result(env="codex", native_cmd=native_cmd,
                             overall="warn", issues=issues)

    try:
        proc = subprocess.run(
            ["codex", "doctor", "--json"],
            capture_output=True, text=True, timeout=60
        )
        output = proc.stdout.strip()
        if not output:
            issues.append(_mk_issue(
                severity="warning",
                summary="codex doctor --json 无输出",
                remediation="检查 stderr: " + (proc.stderr.strip() or "(empty)")
            ))
            return _empty_result(env="codex", native_cmd=native_cmd,
                                 overall="warn", issues=issues)

        data = json.loads(output)
    except subprocess.TimeoutExpired:
        issues.append(_mk_issue(severity="warning", summary="codex doctor --json 超时 (60s)"))
        return _empty_result(env="codex", native_cmd=native_cmd,
                             overall="warn", issues=issues)
    except json.JSONDecodeError as e:
        issues.append(_mk_issue(
            severity="warning",
            summary=f"codex doctor --json 输出不是合法 JSON: {e}",
            remediation="检查 codex 版本是否支持 --json flag"
        ))
        return _empty_result(env="codex", native_cmd=native_cmd,
                             overall="warn", issues=issues)

    checks = data.get("checks", {})
    metrics["total_checks"] = len(checks)
    metrics["schema_version"] = data.get("schemaVersion")
    metrics["codex_version"] = data.get("codexVersion")

    fail_count = 0
    warn_count = 0
    ok_count = 0

    for cid, c in checks.items():
        status = c.get("status", "unknown")
        summary = c.get("summary", "")
        remediation = c.get("remediation")
        if status == "ok":
            ok_count += 1
            continue
        elif status == "fail":
            fail_count += 1
            sev = "critical"
        elif status == "warning":
            warn_count += 1
            sev = "warning"
        else:
            warn_count += 1
            sev = "warning"

        issues.append(_mk_issue(
            severity=sev,
            summary=summary,
            remediation=remediation,
            check_id=cid
        ))

    metrics["fail_count"] = fail_count
    metrics["warn_count"] = warn_count
    metrics["ok_count"] = ok_count

    overall = "fail" if fail_count > 0 else ("warn" if warn_count > 0 else "ok")
    return _empty_result(env="codex", native_cmd=native_cmd,
                         overall=overall, issues=issues, metrics=metrics)


def diag_grok() -> dict:
    """调用 grok inspect --json，提取 skills/hooks/MCP 等 metrics。"""
    native_cmd = "grok inspect --json"
    issues = []
    metrics = {}

    if not shutil.which("grok"):
        issues.append(_mk_issue(
            severity="warning",
            summary="grok 不在 PATH 中",
            remediation="安装 Grok CLI"
        ))
        return _empty_result(env="grok", native_cmd=native_cmd,
                             overall="warn", issues=issues)

    try:
        proc = subprocess.run(
            ["grok", "inspect", "--json"],
            capture_output=True, text=True, timeout=60
        )
        output = proc.stdout.strip()
        if not output:
            issues.append(_mk_issue(
                severity="warning",
                summary="grok inspect --json 无输出",
                remediation="检查 stderr: " + (proc.stderr.strip() or "(empty)")
            ))
            return _empty_result(env="grok", native_cmd=native_cmd,
                                 overall="warn", issues=issues)
        data = json.loads(output)
    except subprocess.TimeoutExpired:
        issues.append(_mk_issue(severity="warning", summary="grok inspect --json 超时 (60s)"))
        return _empty_result(env="grok", native_cmd=native_cmd,
                             overall="warn", issues=issues)
    except json.JSONDecodeError as e:
        issues.append(_mk_issue(
            severity="warning",
            summary=f"grok inspect --json 输出不是合法 JSON: {e}",
            remediation="某些 grok 版本可能不支持 --json，尝试 grok inspect (tree)"
        ))
        return _empty_result(env="grok", native_cmd=native_cmd,
                             overall="warn", issues=issues)

    metrics["grok_version"] = data.get("grokVersion")

    skills = data.get("skills", [])
    hooks = data.get("hooks", [])
    agents = data.get("agents", [])
    mcp_servers = data.get("mcpServers", [])
    plugins = data.get("plugins", [])

    metrics["skills_count"] = len(skills) if isinstance(skills, list) else 0
    metrics["hooks_count"] = len(hooks) if isinstance(hooks, list) else 0
    metrics["agents_count"] = len(agents) if isinstance(agents, list) else 0
    metrics["mcp_servers_count"] = len(mcp_servers) if isinstance(mcp_servers, list) else 0
    metrics["plugins_count"] = len(plugins) if isinstance(plugins, list) else 0

    # 异常判定：hooks 过多可能拖慢执行
    if metrics["hooks_count"] > 20:
        issues.append(_mk_issue(
            severity="warning",
            summary=f"hooks 注册数偏多 ({metrics['hooks_count']} 个)，可能影响性能",
            remediation="清理不需要的 hook 或合并同类 hook"
        ))

    # MCP servers 数为 0 只是提示
    if metrics["mcp_servers_count"] == 0:
        issues.append(_mk_issue(
            severity="ok",
            summary="未配置 MCP servers",
            remediation="按需配置 MCP 扩展能力"
        ))

    overall = "warn" if any(i["severity"] == "warning" for i in issues) else \
              ("critical" if any(i["severity"] == "critical" for i in issues) else "ok")

    return _empty_result(env="grok", native_cmd=native_cmd,
                         overall=overall, issues=issues, metrics=metrics)


def diag_kimi() -> dict:
    """调用 kimi doctor config 和 kimi doctor tui。"""
    native_cmd = "kimi doctor config | kimi doctor tui"
    issues = []
    metrics = {}

    if not shutil.which("kimi"):
        issues.append(_mk_issue(
            severity="warning",
            summary="kimi 不在 PATH 中",
            remediation="安装 Kimi Code CLI"
        ))
        return _empty_result(env="kimi", native_cmd=native_cmd,
                             overall="warn", issues=issues)

    def _run(subcmd: str) -> tuple[int, str, str]:
        p = subprocess.run(["kimi", "doctor", subcmd], capture_output=True,
                           text=True, timeout=30)
        return p.returncode, p.stdout.strip(), p.stderr.strip()

    # config
    rc, out, err = _run("config")
    metrics["config_returncode"] = rc
    if rc == 0 and "OK" in out:
        metrics["config_status"] = "ok"
    elif "FAIL" in out or rc != 0:
        metrics["config_status"] = "fail"
        issues.append(_mk_issue(
            severity="critical",
            summary=f"kimi doctor config 失败: {out or err}",
            remediation="检查 ~/.kimi-code/config.toml 语法"
        ))
    else:
        metrics["config_status"] = "unknown"
        issues.append(_mk_issue(
            severity="warning",
            summary=f"kimi doctor config 输出异常: {out or err}"
        ))

    # tui
    rc, out, err = _run("tui")
    metrics["tui_returncode"] = rc
    if rc == 0 and "OK" in out:
        metrics["tui_status"] = "ok"
    elif "FAIL" in out or rc != 0:
        metrics["tui_status"] = "fail"
        issues.append(_mk_issue(
            severity="critical",
            summary=f"kimi doctor tui 失败: {out or err}",
            remediation="检查 ~/.kimi-code/tui.toml 语法"
        ))
    else:
        metrics["tui_status"] = "unknown"
        issues.append(_mk_issue(
            severity="warning",
            summary=f"kimi doctor tui 输出异常: {out or err}"
        ))

    overall = "fail" if any(i["severity"] == "critical" for i in issues) else \
              ("warn" if any(i["severity"] == "warning" for i in issues) else "ok")
    return _empty_result(env="kimi", native_cmd=native_cmd,
                         overall=overall, issues=issues, metrics=metrics)


def diag_mimo() -> dict:
    """
    mimo 没有单命令汇总诊断，组合调用多个 mimo debug 子命令。
    用 mimo debug config + mimo debug paths 获取结构与路径信息。
    """
    native_cmd = "mimo debug config + mimo debug paths"
    issues = []
    metrics = {}

    if not shutil.which("mimo"):
        issues.append(_mk_issue(
            severity="warning",
            summary="mimo 不在 PATH 中",
            remediation="安装 MiMo Code CLI"
        ))
        return _empty_result(env="mimo", native_cmd=native_cmd,
                             overall="warn", issues=issues)

    def _run(args: list[str]) -> tuple[int, str, str]:
        p = subprocess.run(["mimo"] + args, capture_output=True,
                           text=True, timeout=30)
        return p.returncode, p.stdout.strip(), p.stderr.strip()

    # mimo debug config
    rc, out, err = _run(["debug", "config"])
    metrics["debug_config_returncode"] = rc
    if rc == 0 and out:
        metrics["debug_config_ok"] = True
        # 尝试解析简单 key=value 输出
        try:
            for line in out.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    k = k.strip().lower().replace(" ", "_")
                    if "version" in k:
                        metrics[f"mimo_{k}"] = v.strip()
        except Exception:
            pass
    else:
        metrics["debug_config_ok"] = False
        issues.append(_mk_issue(
            severity="warning",
            summary=f"mimo debug config 失败: {err or out}"
        ))

    # mimo debug paths
    rc, out, err = _run(["debug", "paths"])
    metrics["debug_paths_returncode"] = rc
    if rc == 0 and out:
        metrics["debug_paths_ok"] = True
    else:
        metrics["debug_paths_ok"] = False
        issues.append(_mk_issue(
            severity="warning",
            summary=f"mimo debug paths 失败: {err or out}"
        ))

    overall = "fail" if any(i["severity"] == "critical" for i in issues) else \
              ("warn" if any(i["severity"] == "warning" for i in issues) else "ok")
    return _empty_result(env="mimo", native_cmd=native_cmd,
                         overall=overall, issues=issues, metrics=metrics,
                         notes="mimo 无单命令汇总诊断，组合 mimo debug 多个子命令做自适应发现")


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

def run_env(env: str) -> dict:
    """根据 env 名字调用对应的诊断函数。"""
    router = {
        "claude": diag_claude,
        "codex": diag_codex,
        "grok": diag_grok,
        "kimi": diag_kimi,
        "mimo": diag_mimo,
    }
    fn = router.get(env)
    if not fn:
        return _empty_result(
            env=env, native_cmd="(none)", overall="warn",
            issues=[_mk_issue(severity="warning",
                              summary=f"未知环境: {env}，可选: {list(router.keys())}")]
        )
    try:
        return fn()
    except Exception as e:
        return _empty_result(
            env=env, native_cmd="(none)", overall="warn",
            issues=[_mk_issue(severity="warning",
                              summary=f"诊断执行异常: {type(e).__name__}: {e}")]
        )


def main():
    parser = argparse.ArgumentParser(
        description="调用 AI 编码环境的原生诊断命令，统一输出 JSON"
    )
    parser.add_argument(
        "--env",
        choices=["claude", "codex", "grok", "kimi", "mimo", "all"],
        default="all",
        help="要诊断的环境 (默认: all)"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="纯 JSON 输出（默认即 JSON，保留此参数以兼容 env-doctor 调用习惯）"
    )
    args = parser.parse_args()

    envs = ["claude", "codex", "grok", "kimi", "mimo"] if args.env == "all" else [args.env]

    results = [run_env(e) for e in envs]

    # 汇总退出码
    has_fail = any(
        any(i["severity"] == "critical" for i in r["issues"]) for r in results
    )
    has_warn = any(
        any(i["severity"] in ("warning", "warn") for i in r["issues"]) for r in results
    )
    if len(results) == 1:
        print(json.dumps(results[0], ensure_ascii=False, indent=2))
    else:
        print(json.dumps({
            "overall_status": "fail" if has_fail else ("warn" if has_warn else "ok"),
            "environments": results,
            "summary": {
                "total": len(results),
                "fail": sum(1 for r in results if r["overall_status"] == "fail"),
                "warn": sum(1 for r in results if r["overall_status"] == "warn"),
                "ok": sum(1 for r in results if r["overall_status"] == "ok"),
            }
        }, ensure_ascii=False, indent=2))

    sys.exit(1 if has_fail else 0)


if __name__ == "__main__":
    main()
