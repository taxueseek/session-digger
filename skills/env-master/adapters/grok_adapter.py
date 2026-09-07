"""Grok 适配器 — 复用 native-diag.py 的 diag_grok 逻辑。"""

import json
import shutil
import subprocess
from typing import Optional

from .base import BaseAdapter, _mk_issue, HOME


class GrokAdapter(BaseAdapter):
    env_name = "grok"
    label = "Grok Build"

    def discover(self) -> dict:
        result = {
            "installed": False,
            "version": None,
            "path": None,
            "config_dir": None,
        }
        grok_path = shutil.which("grok")
        if grok_path:
            result["path"] = grok_path
            result["installed"] = True
        grok_dir = HOME / ".grok"
        if grok_dir.exists():
            result["config_dir"] = str(grok_dir)
            result["installed"] = True
        if grok_path:
            try:
                proc = subprocess.run(["grok", "--version"], capture_output=True,
                                      text=True, timeout=10)
                if proc.returncode == 0:
                    result["version"] = proc.stdout.strip()
            except Exception:
                pass
        return result

    def run_native(self) -> dict:
        """调用 grok inspect --json，提取 skills/hooks/MCP 等 metrics。"""
        native_cmd = "grok inspect --json"
        issues = []
        metrics = {}

        if not shutil.which("grok"):
            issues.append(_mk_issue(
                severity="warning",
                summary="grok 不在 PATH 中",
                remediation="安装 Grok CLI",
                dimension="install"
            ))
            return {
                "env": "grok", "native_cmd": native_cmd,
                "overall_status": "warn", "issues": issues, "metrics": metrics,
            }

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
                    remediation="检查 stderr: " + (proc.stderr.strip() or "(empty)"),
                    dimension="config"
                ))
                return {
                    "env": "grok", "native_cmd": native_cmd,
                    "overall_status": "warn", "issues": issues, "metrics": metrics,
                }
            data = json.loads(output)
        except subprocess.TimeoutExpired:
            issues.append(_mk_issue(
                severity="warning", summary="grok inspect --json 超时 (60s)",
                dimension="config"
            ))
            return {
                "env": "grok", "native_cmd": native_cmd,
                "overall_status": "warn", "issues": issues, "metrics": metrics,
            }
        except json.JSONDecodeError as e:
            issues.append(_mk_issue(
                severity="warning",
                summary=f"grok inspect --json 输出不是合法 JSON: {e}",
                remediation="某些 grok 版本可能不支持 --json",
                dimension="config"
            )
            )
            return {
                "env": "grok", "native_cmd": native_cmd,
                "overall_status": "warn", "issues": issues, "metrics": metrics,
            }

        metrics["grok_version"] = data.get("grokVersion")

        for key in ("skills", "hooks", "agents", "mcpServers", "plugins"):
            val = data.get(key, [])
            metrics[f"{key}_count"] = len(val) if isinstance(val, list) else 0

        hooks_count = metrics.get("hooks_count", 0)
        if hooks_count > 20:
            issues.append(_mk_issue(
                severity="warning",
                summary=f"hooks 注册数偏多 ({hooks_count} 个)，可能影响性能",
                remediation="清理不需要的 hook 或合并同类 hook",
                dimension="extensions"
            ))

        if metrics.get("mcpServers_count", 0) == 0:
            issues.append(_mk_issue(
                severity="ok",
                summary="未配置 MCP servers",
                remediation="按需配置 MCP 扩展能力",
                dimension="extensions"
            ))

        overall = "warn" if any(i["severity"] == "warning" for i in issues) else \
                  ("critical" if any(i["severity"] == "critical" for i in issues) else "ok")

        return {
            "env": "grok", "native_cmd": native_cmd,
            "overall_status": overall, "issues": issues, "metrics": metrics,
        }

    def get_blind_spots(self) -> list:
        return ["cross_env_drift", "api_reachability", "path_conflict", "network"]
