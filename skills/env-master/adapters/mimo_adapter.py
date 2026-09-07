"""MiMo Code 适配器 — 复用 native-diag.py 的 diag_mimo 逻辑。"""

import shutil
import subprocess
from typing import Optional

from .base import BaseAdapter, _mk_issue, HOME


class MimoAdapter(BaseAdapter):
    env_name = "mimo"
    label = "MiMo Code"

    def discover(self) -> dict:
        result = {
            "installed": False,
            "version": None,
            "path": None,
            "config_dir": None,
        }
        mimo_path = shutil.which("mimo") or shutil.which("mimocode")
        if mimo_path:
            result["path"] = mimo_path
            result["installed"] = True
        mimo_dir = HOME / ".mimocode"
        if mimo_dir.exists():
            result["config_dir"] = str(mimo_dir)
            result["installed"] = True
        if mimo_path:
            try:
                proc = subprocess.run([mimo_path, "--version"], capture_output=True,
                                      text=True, timeout=10)
                if proc.returncode == 0:
                    result["version"] = proc.stdout.strip()
            except Exception:
                pass
        return result

    def run_native(self) -> dict:
        """组合调用 mimo debug config + mimo debug paths。"""
        native_cmd = "mimo debug config + mimo debug paths"
        issues = []
        metrics = {}

        mimo_path = shutil.which("mimo") or shutil.which("mimocode")
        if not mimo_path:
            issues.append(_mk_issue(
                severity="warning",
                summary="mimo 不在 PATH 中",
                remediation="安装 MiMo Code CLI",
                dimension="install"
            ))
            return {
                "env": "mimo", "native_cmd": native_cmd,
                "overall_status": "warn", "issues": issues, "metrics": metrics,
            }

        def _run(args: list) -> tuple:
            p = subprocess.run([mimo_path] + args, capture_output=True,
                               text=True, timeout=30)
            return p.returncode, p.stdout.strip(), p.stderr.strip()

        # mimo debug config
        rc, out, err = _run(["debug", "config"])
        metrics["debug_config_returncode"] = rc
        if rc == 0 and out:
            metrics["debug_config_ok"] = True
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
                summary=f"mimo debug config 失败: {err or out}",
                dimension="config"
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
                summary=f"mimo debug paths 失败: {err or out}",
                dimension="config"
            ))

        overall = "fail" if any(i["severity"] == "critical" for i in issues) else \
                  ("warn" if any(i["severity"] == "warning" for i in issues) else "ok")
        return {
            "env": "mimo", "native_cmd": native_cmd,
            "overall_status": overall, "issues": issues, "metrics": metrics,
            "notes": "mimo 无单命令汇总诊断，组合 mimo debug 多个子命令做自适应发现",
        }

    def get_blind_spots(self) -> list:
        return ["cross_env_drift", "api_reachability", "path_conflict", "network", "auth"]
