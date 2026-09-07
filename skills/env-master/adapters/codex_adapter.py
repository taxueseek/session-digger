"""Codex 适配器 — 复用 native-diag.py 的 diag_codex 逻辑。"""

import json
import shutil
import subprocess
from typing import Optional

from .base import BaseAdapter, _mk_issue, HOME


class CodexAdapter(BaseAdapter):
    env_name = "codex"
    label = "Codex"

    def discover(self) -> dict:
        result = {
            "installed": False,
            "version": None,
            "path": None,
            "config_dir": None,
        }
        codex_path = shutil.which("codex")
        if codex_path:
            result["path"] = codex_path
            result["installed"] = True
        codex_dir = HOME / ".codex"
        if codex_dir.exists():
            result["config_dir"] = str(codex_dir)
            result["installed"] = True
        if codex_path:
            try:
                proc = subprocess.run(["codex", "--version"], capture_output=True,
                                      text=True, timeout=10)
                if proc.returncode == 0:
                    result["version"] = proc.stdout.strip()
            except Exception:
                pass
        return result

    def run_native(self) -> dict:
        """调用 codex doctor --json，提取所有非 ok 的 check 项。"""
        native_cmd = "codex doctor --json"
        issues = []
        metrics = {}

        if not shutil.which("codex"):
            issues.append(_mk_issue(
                severity="warning",
                summary="codex 不在 PATH 中",
                remediation="安装或重装 Codex CLI",
                dimension="install"
            ))
            return {
                "env": "codex", "native_cmd": native_cmd,
                "overall_status": "warn", "issues": issues, "metrics": metrics,
            }

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
                    remediation="检查 stderr: " + (proc.stderr.strip() or "(empty)"),
                    dimension="config"
                ))
                return {
                    "env": "codex", "native_cmd": native_cmd,
                    "overall_status": "warn", "issues": issues, "metrics": metrics,
                }
            data = json.loads(output)
        except subprocess.TimeoutExpired:
            issues.append(_mk_issue(
                severity="warning", summary="codex doctor --json 超时 (60s)",
                dimension="config"
            ))
            return {
                "env": "codex", "native_cmd": native_cmd,
                "overall_status": "warn", "issues": issues, "metrics": metrics,
            }
        except json.JSONDecodeError as e:
            issues.append(_mk_issue(
                severity="warning",
                summary=f"codex doctor --json 输出不是合法 JSON: {e}",
                remediation="检查 codex 版本是否支持 --json flag",
                dimension="config"
            ))
            return {
                "env": "codex", "native_cmd": native_cmd,
                "overall_status": "warn", "issues": issues, "metrics": metrics,
            }

        checks = data.get("checks", {})
        metrics["total_checks"] = len(checks)
        metrics["schema_version"] = data.get("schemaVersion")
        metrics["codex_version"] = data.get("codexVersion")

        fail_count = warn_count = ok_count = 0

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
            else:
                warn_count += 1
                sev = "warning"

            issues.append(_mk_issue(
                severity=sev,
                summary=summary,
                remediation=remediation,
                check_id=cid,
                dimension=_classify_codex_check(cid),
            ))

        metrics["fail_count"] = fail_count
        metrics["warn_count"] = warn_count
        metrics["ok_count"] = ok_count

        overall = "fail" if fail_count > 0 else ("warn" if warn_count > 0 else "ok")
        return {
            "env": "codex", "native_cmd": native_cmd,
            "overall_status": overall, "issues": issues, "metrics": metrics,
        }

    def get_blind_spots(self) -> list:
        return ["cross_env_drift", "path_conflict", "skill_version_diff"]


def _classify_codex_check(check_id: str) -> str:
    """根据 codex doctor check_id 分类维度。"""
    cid = check_id.lower()
    if any(k in cid for k in ["install", "path", "bin"]):
        return "install"
    if any(k in cid for k in ["config", "setting", "toml"]):
        return "config"
    if any(k in cid for k in ["auth", "token", "credential", "oauth", "api_key"]):
        return "auth"
    if any(k in cid for k in ["network", "connect", "websocket", "api", "provider", "reach"]):
        return "network"
    if any(k in cid for k in ["mcp", "skill", "extension", "plugin"]):
        return "extensions"
    return "config"
