"""适配器基类 — 定义统一接口。"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

HOME = Path.home()


def _mk_issue(severity: str, summary: str, remediation: Optional[str] = None,
              check_id: Optional[str] = None, dimension: Optional[str] = None) -> dict:
    """构造一条标准化的 issue。"""
    out = {"severity": severity, "summary": summary}
    if remediation:
        out["remediation"] = remediation
    if check_id:
        out["check_id"] = check_id
    if dimension:
        out["dimension"] = dimension
    return out


class BaseAdapter(ABC):
    """所有环境适配器的基类。"""

    env_name: str = "unknown"
    label: str = "Unknown"

    def discover(self) -> dict:
        """探测环境是否安装、版本、路径。子类应覆盖。"""
        return {
            "installed": False,
            "version": None,
            "path": None,
            "config_dir": None,
        }

    def run_native(self) -> dict:
        """调用原生命令，返回统一格式。子类必须覆盖。"""
        raise NotImplementedError

    def get_blind_spots(self) -> list:
        """返回该环境的盲区 ID 列表。子类可覆盖。"""
        return ["cross_env_drift", "api_reachability", "path_conflict"]

    def run_full_check(self) -> dict:
        """执行完整检查：discover + run_native + 维度标注。"""
        discovery = self.discover()
        native = self.run_native()

        # 合并 issues，补充 dimension 字段
        issues = native.get("issues", [])
        for issue in issues:
            if "dimension" not in issue:
                issue["dimension"] = _infer_dimension(issue.get("check_id", ""),
                                                      issue.get("summary", ""))

        return {
            "env": self.env_name,
            "label": self.label,
            "installed": discovery.get("installed", False),
            "version": discovery.get("version"),
            "path": discovery.get("path"),
            "config_dir": discovery.get("config_dir"),
            "native_cmd": native.get("native_cmd", ""),
            "overall_status": native.get("overall_status", "ok"),
            "issues": issues,
            "metrics": native.get("metrics", {}),
            "notes": native.get("notes", ""),
            "blind_spots": self.get_blind_spots(),
        }


def _infer_dimension(check_id: str, summary: str) -> str:
    """从 check_id 和 summary 推断维度。"""
    text = (check_id + " " + summary).lower()
    if any(k in text for k in ["install", "version", "path", "which", "bin", "重复", "duplicate"]):
        return "install"
    if any(k in text for k in ["config", "setting", "toml", "json", "syntax", "权限", "permission"]):
        return "config"
    if any(k in text for k in ["auth", "token", "api_key", "api key", "oauth", "credential", "认证"]):
        return "auth"
    if any(k in text for k in ["network", "api", "url", "latency", "reachable", "proxy", "网络", "可达"]):
        return "network"
    if any(k in text for k in ["skill", "hook", "mcp", "plugin", "extension", "扩展"]):
        return "extensions"
    if any(k in text for k in ["cross", "conflict", "drift", "冲突", "漂移"]):
        return "cross_env"
    return "config"
