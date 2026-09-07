"""Claude Code 适配器 — 复用 native-diag.py 的 diag_claude 逻辑。"""

import shutil
from pathlib import Path
from typing import Optional

from .base import BaseAdapter, _mk_issue, HOME


class ClaudeAdapter(BaseAdapter):
    env_name = "claude"
    label = "Claude Code"

    def discover(self) -> dict:
        result = {
            "installed": False,
            "version": None,
            "path": None,
            "config_dir": None,
        }
        # claude 不在 PATH 中（它是 npm 全局包或 app bundle）
        claude_path = shutil.which("claude")
        if claude_path:
            result["path"] = claude_path
            result["installed"] = True
        # 配置目录探测
        claude_dir = HOME / ".claude"
        if claude_dir.exists():
            result["config_dir"] = str(claude_dir)
            result["installed"] = True
        # 尝试获取版本
        if claude_path:
            try:
                import subprocess
                proc = subprocess.run(["claude", "--version"], capture_output=True,
                                      text=True, timeout=10)
                if proc.returncode == 0:
                    result["version"] = proc.stdout.strip()
            except Exception:
                pass
        return result

    def run_native(self) -> dict:
        """
        Claude Code /doctor 是会话内斜杠命令，CLI 无法直接调用。
        这里做静态探测：检查 ~/.claude 目录结构。
        """
        native_cmd = "/doctor (会话内斜杠命令)"
        issues = []
        metrics = {}

        claude_dir = HOME / ".claude"
        if claude_dir.exists():
            metrics["config_dir_exists"] = True
            settings = claude_dir / "settings.json"
            metrics["settings_exists"] = settings.exists()
            # 检查 skills 目录
            skills_dir = claude_dir / "skills"
            if skills_dir.exists():
                skill_count = len([d for d in skills_dir.iterdir() if d.is_dir()])
                metrics["skills_count"] = skill_count
            # 检查 CLAUDE.md
            claude_md = Path.cwd() / "CLAUDE.md"
            metrics["project_claude_md"] = claude_md.exists()
        else:
            metrics["config_dir_exists"] = False
            issues.append(_mk_issue(
                severity="warning",
                summary="~/.claude 目录不存在，Claude Code 可能未配置",
                remediation="在 Claude Code 会话内运行 /doctor 获取完整诊断",
                dimension="config"
            ))

        issues.append(_mk_issue(
            severity="ok",
            summary="Claude Code /doctor 必须在会话内调用，CLI 无法调度",
            remediation="在 Claude Code 会话中输入 /doctor",
            dimension="install"
        ))

        return {
            "env": "claude",
            "native_cmd": native_cmd,
            "overall_status": "warn" if any(i["severity"] != "ok" for i in issues) else "ok",
            "issues": issues,
            "metrics": metrics,
            "notes": "fallback: /doctor 是斜杠命令, 仅静态探测 config 目录",
        }

    def get_blind_spots(self) -> list:
        return ["cross_env_drift", "api_reachability", "path_conflict", "env_var_redundancy"]
