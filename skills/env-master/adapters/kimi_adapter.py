"""Kimi Code 适配器 — 复用 native-diag.py 的 diag_kimi 逻辑。"""

import shutil
import subprocess
from typing import Optional

from .base import BaseAdapter, _mk_issue, HOME


class KimiAdapter(BaseAdapter):
    env_name = "kimi"
    label = "Kimi Code"

    def discover(self) -> dict:
        result = {
            "installed": False,
            "version": None,
            "path": None,
            "config_dir": None,
        }
        kimi_path = shutil.which("kimi")
        if kimi_path:
            result["path"] = kimi_path
            result["installed"] = True
        kimi_dir = HOME / ".kimi-code"
        if kimi_dir.exists():
            result["config_dir"] = str(kimi_dir)
            result["installed"] = True
        if kimi_path:
            try:
                proc = subprocess.run(["kimi", "--version"], capture_output=True,
                                      text=True, timeout=10)
                if proc.returncode == 0:
                    result["version"] = proc.stdout.strip()
            except Exception:
                pass
        return result

    def run_native(self) -> dict:
        """调用 kimi doctor config 和 kimi doctor tui。"""
        native_cmd = "kimi doctor config | kimi doctor tui"
        issues = []
        metrics = {}

        if not shutil.which("kimi"):
            issues.append(_mk_issue(
                severity="warning",
                summary="kimi 不在 PATH 中",
                remediation="安装 Kimi Code CLI",
                dimension="install"
            ))
            return {
                "env": "kimi", "native_cmd": native_cmd,
                "overall_status": "warn", "issues": issues, "metrics": metrics,
            }

        def _run(subcmd: str) -> tuple:
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
                remediation="检查 ~/.kimi-code/config.toml 语法",
                dimension="config"
            ))
        else:
            metrics["config_status"] = "unknown"
            issues.append(_mk_issue(
                severity="warning",
                summary=f"kimi doctor config 输出异常: {out or err}",
                dimension="config"
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
                remediation="检查 ~/.kimi-code/tui.toml 语法",
                dimension="config"
            ))
        else:
            metrics["tui_status"] = "unknown"
            issues.append(_mk_issue(
                severity="warning",
                summary=f"kimi doctor tui 输出异常: {out or err}",
                dimension="config"
            ))

        overall = "fail" if any(i["severity"] == "critical" for i in issues) else \
                  ("warn" if any(i["severity"] == "warning" for i in issues) else "ok")
        return {
            "env": "kimi", "native_cmd": native_cmd,
            "overall_status": overall, "issues": issues, "metrics": metrics,
        }

    def get_blind_spots(self) -> list:
        return ["cross_env_drift", "api_reachability", "path_conflict", "network", "install", "skills", "hooks"]
