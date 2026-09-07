"""DeepSeek 适配器 — 推断实现。"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .base import BaseAdapter, _mk_issue, HOME


class DeepSeekAdapter(BaseAdapter):
    env_name = "deepseek"
    label = "DeepSeek"

    def discover(self) -> dict:
        result = {
            "installed": False,
            "version": None,
            "path": None,
            "config_dir": None,
        }
        # 可能的命令名
        for cmd in ("deepseek", "deepseek-cli", "ds"):
            p = shutil.which(cmd)
            if p:
                result["path"] = p
                result["installed"] = True
                break
        # 配置目录
        for d in (HOME / ".deepseek", HOME / ".config/deepseek"):
            if d.exists():
                result["config_dir"] = str(d)
                result["installed"] = True
                break
        # 版本
        if result["path"]:
            try:
                proc = subprocess.run([result["path"], "--version"], capture_output=True,
                                      text=True, timeout=10)
                if proc.returncode == 0:
                    result["version"] = proc.stdout.strip()
            except Exception:
                pass
        return result

    def run_native(self) -> dict:
        """尝试 deepseek --check / --version + 配置文件检查。"""
        issues = []
        metrics = {}

        ds_path = shutil.which("deepseek") or shutil.which("deepseek-cli") or shutil.which("ds")
        native_cmd = ds_path or "deepseek (not found)"

        if not ds_path:
            issues.append(_mk_issue(
                severity="warning",
                summary="deepseek 不在 PATH 中",
                remediation="安装 DeepSeek CLI",
                dimension="install"
            ))
            return {
                "env": "deepseek", "native_cmd": native_cmd,
                "overall_status": "warn", "issues": issues, "metrics": metrics,
            }

        # 尝试 --check
        try:
            proc = subprocess.run([ds_path, "--check"], capture_output=True,
                                  text=True, timeout=30)
            if proc.returncode == 0:
                metrics["check_ok"] = True
                metrics["check_output"] = proc.stdout.strip()[:500]
            else:
                metrics["check_ok"] = False
                # --check 不支持时 fallback
                if "unknown" in proc.stderr.lower() or "flag" in proc.stderr.lower():
                    metrics["check_unsupported"] = True
                else:
                    issues.append(_mk_issue(
                        severity="warning",
                        summary=f"deepseek --check 失败: {proc.stderr.strip()[:200]}",
                        dimension="config"
                    ))
        except subprocess.TimeoutExpired:
            issues.append(_mk_issue(
                severity="warning", summary="deepseek --check 超时",
                dimension="config"
            ))
        except Exception as e:
            issues.append(_mk_issue(
                severity="warning", summary=f"deepseek --check 异常: {e}",
                dimension="config"
            ))

        # 配置文件检查
        config_dir = HOME / ".deepseek"
        if config_dir.exists():
            for cfg_name in ("config.json", "config.toml", "settings.json"):
                cfg = config_dir / cfg_name
                if cfg.exists():
                    metrics["config_file"] = str(cfg)
                    try:
                        content = cfg.read_text()
                        if cfg_name.endswith(".json"):
                            json.loads(content)
                            metrics["config_valid"] = True
                        else:
                            metrics["config_valid"] = True
                    except json.JSONDecodeError as e:
                        metrics["config_valid"] = False
                        issues.append(_mk_issue(
                            severity="critical",
                            summary=f"{cfg_name} JSON 解析失败: {e}",
                            remediation=f"修复 {cfg} 语法",
                            dimension="config"
                        ))

        overall = "fail" if any(i["severity"] == "critical" for i in issues) else \
                  ("warn" if any(i["severity"] == "warning" for i in issues) else "ok")
        return {
            "env": "deepseek", "native_cmd": native_cmd,
            "overall_status": overall, "issues": issues, "metrics": metrics,
            "notes": "deepseek 适配器为推断实现，基于 --check + 配置文件静态检查",
        }

    def get_blind_spots(self) -> list:
        return ["cross_env_drift", "api_reachability", "path_conflict", "network", "auth", "extensions"]
