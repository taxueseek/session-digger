"""Cursor 适配器 — 推断实现。"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .base import BaseAdapter, _mk_issue, HOME


class CursorAdapter(BaseAdapter):
    env_name = "cursor"
    label = "Cursor"

    def discover(self) -> dict:
        result = {
            "installed": False,
            "version": None,
            "path": None,
            "config_dir": None,
        }
        # CLI 命令
        cursor_path = shutil.which("cursor")
        if cursor_path:
            result["path"] = cursor_path
            result["installed"] = True
        # 配置目录（Cursor 用 ~/.cursor 和 ~/.config/Cursor）
        for d in (HOME / ".cursor", HOME / ".config/Cursor",
                  HOME / "Library/Application Support/Cursor"):
            if d.exists():
                result["config_dir"] = str(d)
                result["installed"] = True
                break
        # 版本
        if cursor_path:
            try:
                proc = subprocess.run([cursor_path, "--version"], capture_output=True,
                                      text=True, timeout=10)
                if proc.returncode == 0:
                    result["version"] = proc.stdout.strip()
            except Exception:
                pass
        return result

    def run_native(self) -> dict:
        """尝试 cursor --version + 配置目录检查。"""
        issues = []
        metrics = {}

        cursor_path = shutil.which("cursor")
        native_cmd = cursor_path or "cursor (not found)"

        if not cursor_path:
            issues.append(_mk_issue(
                severity="warning",
                summary="cursor 不在 PATH 中",
                remediation="安装 Cursor CLI 或添加 cursor 到 PATH",
                dimension="install"
            ))
            return {
                "env": "cursor", "native_cmd": native_cmd,
                "overall_status": "warn", "issues": issues, "metrics": metrics,
            }

        # 版本
        try:
            proc = subprocess.run([cursor_path, "--version"], capture_output=True,
                                  text=True, timeout=10)
            if proc.returncode == 0:
                metrics["version_output"] = proc.stdout.strip()
        except Exception as e:
            issues.append(_mk_issue(
                severity="warning",
                summary=f"cursor --version 失败: {e}",
                dimension="install"
            ))

        # 配置目录检查
        config_dir = HOME / ".cursor"
        if config_dir.exists():
            # 检查 settings.json
            settings = config_dir / "settings.json"
            if settings.exists():
                metrics["settings_exists"] = True
                try:
                    json.loads(settings.read_text())
                    metrics["settings_valid"] = True
                except json.JSONDecodeError as e:
                    metrics["settings_valid"] = False
                    issues.append(_mk_issue(
                        severity="warning",
                        summary=f"settings.json 解析失败: {e}",
                        dimension="config"
                    ))

        # 检查 CLI 扩展目录
        ext_dir = config_dir / "extensions"
        if ext_dir.exists():
            ext_count = len(list(ext_dir.iterdir()))
            metrics["extensions_count"] = ext_count

        overall = "fail" if any(i["severity"] == "critical" for i in issues) else \
                  ("warn" if any(i["severity"] == "warning" for i in issues) else "ok")
        return {
            "env": "cursor", "native_cmd": native_cmd,
            "overall_status": overall, "issues": issues, "metrics": metrics,
            "notes": "cursor 适配器为推断实现，基于 --version + 配置目录静态检查",
        }

    def get_blind_spots(self) -> list:
        return ["cross_env_drift", "api_reachability", "path_conflict", "network", "auth", "extensions"]
