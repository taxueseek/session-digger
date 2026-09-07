"""Aider 适配器 — 推断实现。"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .base import BaseAdapter, _mk_issue, HOME


class AiderAdapter(BaseAdapter):
    env_name = "aider"
    label = "Aider"

    def discover(self) -> dict:
        result = {
            "installed": False,
            "version": None,
            "path": None,
            "config_dir": None,
        }
        aider_path = shutil.which("aider")
        if aider_path:
            result["path"] = aider_path
            result["installed"] = True
        # Aider 配置在 ~/.aider.conf.yml
        aider_conf = HOME / ".aider.conf.yml"
        if aider_conf.exists():
            result["config_dir"] = str(HOME)
            result["installed"] = True
        if aider_path:
            try:
                proc = subprocess.run([aider_path, "--version"], capture_output=True,
                                      text=True, timeout=15)
                if proc.returncode == 0:
                    result["version"] = proc.stdout.strip().split("\n")[0]
            except Exception:
                pass
        return result

    def run_native(self) -> dict:
        """调用 aider --version + 配置文件检查。"""
        issues = []
        metrics = {}

        aider_path = shutil.which("aider")
        native_cmd = aider_path or "aider (not found)"

        if not aider_path:
            issues.append(_mk_issue(
                severity="warning",
                summary="aider 不在 PATH 中",
                remediation="pip install aider-chat 或 pipx install aider-chat",
                dimension="install"
            ))
            return {
                "env": "aider", "native_cmd": native_cmd,
                "overall_status": "warn", "issues": issues, "metrics": metrics,
            }

        # 版本
        try:
            proc = subprocess.run([aider_path, "--version"], capture_output=True,
                                  text=True, timeout=15)
            if proc.returncode == 0:
                metrics["version_output"] = proc.stdout.strip()
        except Exception as e:
            issues.append(_mk_issue(
                severity="warning",
                summary=f"aider --version 失败: {e}",
                dimension="install"
            ))

        # 配置文件检查
        aider_conf = HOME / ".aider.conf.yml"
        if aider_conf.exists():
            metrics["config_file"] = str(aider_conf)
            try:
                content = aider_conf.read_text()
                metrics["config_size"] = len(content)
                # 简单语法检查：YAML 顶层 key 数量
                key_count = sum(1 for line in content.splitlines()
                                if line and not line.startswith("#")
                                and not line.startswith(" ") and ":" in line)
                metrics["config_keys"] = key_count
            except Exception as e:
                issues.append(_mk_issue(
                    severity="warning",
                    summary=f"读取 aider 配置失败: {e}",
                    dimension="config"
                ))

        # 检查 API key 环境变量
        for env_key in ("AIDER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            val = __import__("os").environ.get(env_key, "")
            if val:
                metrics[f"has_{env_key.lower()}"] = True
            else:
                metrics[f"has_{env_key.lower()}"] = False

        # 如果没有找到任何 API key
        if not any(metrics.get(f"has_{k.lower()}") for k in
                   ("AIDER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")):
            issues.append(_mk_issue(
                severity="warning",
                summary="未检测到 Aider 所需的 API key 环境变量",
                remediation="设置 OPENAI_API_KEY 或 ANTHROPIC_API_KEY",
                dimension="auth"
            ))

        overall = "fail" if any(i["severity"] == "critical" for i in issues) else \
                  ("warn" if any(i["severity"] == "warning" for i in issues) else "ok")
        return {
            "env": "aider", "native_cmd": native_cmd,
            "overall_status": overall, "issues": issues, "metrics": metrics,
            "notes": "aider 适配器为推断实现，基于 --version + 配置文件静态检查",
        }

    def get_blind_spots(self) -> list:
        return ["cross_env_drift", "api_reachability", "path_conflict", "network", "extensions"]
