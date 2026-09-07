"""存储健康检查 — 主机级维度 (D7)。

定位：env-master 的第七维度「存储健康」。与其他六维不同，存储是主机级属性，
不属于某个 AI 环境，因此单独评分、并入全局分。

设计原则：
1. 只收集原始数据（df/du 实测），判断交给规则/模型，不写死路径
2. 探测存在性，不存在则跳过 —— 不硬编码具体机器
3. 只生成可清理项「计划」，绝不执行删除（安全删除由用户在确认后执行）
4. 平台假设：macOS（df -h 输出格式、~/Library/Caches）

输出结构：
- metrics: 原始数据（磁盘、缓存大户、trash 状态、可清理项汇总）
- cleanable_items: 可清理项清单（path/size/category/risk/action）
- issues: 健康问题（severity + summary + remediation），供评分引擎消费
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

from scoring import score_dimension  # 复用扣分规则，避免重复实现

# 可配置扫描根（可用环境变量覆盖）
CACHE_ROOT = Path(os.environ.get("ENV_MASTER_CACHE_ROOT", "~/Library/Caches")).expanduser()
WORKSPACE_ROOTS = [
    Path(p).expanduser()
    for p in os.environ.get(
        "ENV_MASTER_WORKSPACE_ROOTS",
        "~/Documents",  # 本机可用 ENV_MASTER_WORKSPACE_ROOTS 覆盖
    ).split(",")
    if p.strip()
]
DATA_HOME = Path(os.environ.get("ENV_MASTER_DATA_HOME", "~/.kimix")).expanduser()

TRASH_AGE_DAYS = 30            # 超过 30 天未清理的 trash 视为可清理
DISK_WARN_PCT = 20             # 剩余 < 20% 警告
DISK_CRIT_PCT = 10             # 剩余 < 10% 严重
ITEM_WARN_BYTES = 5 * 1024**3  # 单项 > 5GB 警告
ITEM_MIN_BYTES = 500 * 1024**2  # 单项低于此体积不列入可清理


def _run(cmd: list) -> str:
    """执行命令，返回 stdout（失败返回空串）。"""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return proc.stdout or ""
    except Exception:
        return ""


def _human(n: int) -> str:
    """bytes → 人类可读。"""
    if n <= 0:
        return "0B"
    for unit in ["B", "K", "M", "G", "T"]:
        if n < 1024 or unit == "T":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024


def _dir_size(path: Path) -> int:
    """目录总大小（字节），不存在返回 0。"""
    out = _run(["du", "-sk", str(path)])
    m = re.match(r"^(\d+)", out)
    return int(m.group(1)) * 1024 if m else 0


def _dir_top_sizes(path: Path, limit: int = 12) -> list:
    """目录内子项大小 TOP（字节），按大小降序。"""
    if not path.is_dir():
        return []
    out = _run(["du", "-sk", *[str(p) for p in path.iterdir()]])
    items = []
    for line in out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        try:
            size = int(parts[0]) * 1024
        except ValueError:
            continue
        items.append({"name": parts[1].strip(), "size_bytes": size})
    items.sort(key=lambda x: x["size_bytes"], reverse=True)
    return items[:limit]


def _item(path: Path, size: int, category: str, risk: str, action: str,
          rebuildable: bool, detail: str = "") -> dict:
    """构造可清理项。"""
    return {
        "path": str(path),
        "size_bytes": size,
        "size_human": _human(size),
        "category": category,
        "risk": risk,
        "action": action,
        "rebuildable": rebuildable,
        "detail": detail,
    }


def collect_disk() -> dict:
    """磁盘总体情况（优先 Data 卷）。"""
    out = _run(["df", "-h", "/System/Volumes/Data", "/"])
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 9 and parts[0].startswith("/dev/"):
            rows.append({
                "device": parts[0],
                "size": parts[1],
                "used": parts[2],
                "avail": parts[3],
                "used_pct": int(parts[4].rstrip("%")),
                "mounted": parts[8] if len(parts) > 8 else "",
            })
    # 优先用户数据卷，避免取到只读系统卷
    for row in rows:
        if row["mounted"] == "/System/Volumes/Data":
            return row
    return rows[0] if rows else {}


def collect_trash() -> dict:
    """trash 目录超期文件检查（> 30 天），位置从扫描根派生。"""
    trash_dirs = [DATA_HOME / ".trash"] + [r / ".trash" for r in WORKSPACE_ROOTS]
    findings = []
    cutoff = time.time() - TRASH_AGE_DAYS * 86400
    for td in trash_dirs:
        if not td.is_dir():
            continue
        stale = 0
        for child in td.iterdir():
            try:
                if child.stat().st_mtime < cutoff:
                    stale += 1
            except OSError:
                pass
        findings.append({
            "path": str(td),
            "size_bytes": _dir_size(td),
            "size_human": "",
            "stale_count": stale,
        })
    for f in findings:
        f["size_human"] = _human(f["size_bytes"])
    return {"trash_dirs": findings}


def _scan_builds() -> list:
    """编译产物：工作区下 */target，> ITEM_MIN_BYTES。"""
    items = []
    for root in WORKSPACE_ROOTS:
        if not root.is_dir():
            continue
        for target in root.glob("*/target"):
            if not target.is_dir():
                continue
            size = _dir_size(target)
            if size <= ITEM_MIN_BYTES:
                continue
            detail = ""
            dbg = target / "debug"
            rel = target / "release-dist"
            if dbg.is_dir():
                detail = f" debug={_human(_dir_size(dbg))}"
            if rel.is_dir():
                detail += f" release-dist={_human(_dir_size(rel))}"
            items.append(_item(
                target, size, "build", "recompile",
                "cargo clean（或仅删 release-dist，保留 debug 增量缓存）",
                True, detail.strip(),
            ))
    return items


def _scan_caches() -> list:
    """应用缓存大户：~/Library/Caches 下 > ITEM_MIN_BYTES。"""
    return [
        _item(CACHE_ROOT / item["name"], item["size_bytes"], "cache", "safe",
              "rm -rf（应用将自动重建）", True)
        for item in _dir_top_sizes(CACHE_ROOT)
        if item["size_bytes"] > ITEM_MIN_BYTES
    ]


_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _version_key(path: Path):
    """目录名中的版本号 → 可比较 key（无版本号回退 mtime）。"""
    m = _VERSION_RE.search(path.name)
    if m:
        return tuple(int(x) for x in m.groups())
    return (0, 0, int(path.stat().st_mtime))


def _scan_data_home() -> list:
    """会话/下载数据：~/.kimix 下的 memtrace、旧版安装包、sessions。"""
    items = []

    # memtrace：调试残留
    mt = DATA_HOME / "memtrace"
    if mt.is_dir():
        items.append(_item(mt, _dir_size(mt), "session", "safe",
                           "rm -rf（调试残留）", True))

    # downloads：保留最新版本，其余标记
    dl = DATA_HOME / "downloads"
    if dl.is_dir():
        versions = sorted(
            [p for p in dl.iterdir() if p.is_dir()],
            key=_version_key,
        )
        for old in versions[:-1]:  # 保留最新
            items.append(_item(old, _dir_size(old), "download", "safe",
                               "rm -rf（保留最新版本即可）", True))

    # sessions：过大提示归档
    ss = DATA_HOME / "sessions"
    if ss.is_dir():
        size = _dir_size(ss)
        if size > 500 * 1024**2:
            items.append(_item(ss, size, "session", "user_data",
                               "归档历史会话（勿直接删，session_search 依赖）",
                               False, "含会话索引，删除会影响历史检索"))
    return items


def _scan_trash() -> list:
    """trash 超期（> 30 天未清理）。"""
    return [
        _item(Path(td["path"]), td["size_bytes"], "trash", "safe",
              "清理超过 30 天的文件（项目规则允许）", False,
              f"{td['stale_count']} 个文件超期")
        for td in collect_trash().get("trash_dirs", [])
        if td["stale_count"] > 0
    ]


def scan_cleanable() -> list:
    """聚合所有可清理项，按风险低→高、体积大→小排序。"""
    items = _scan_builds() + _scan_caches() + _scan_data_home() + _scan_trash()
    risk_order = {"safe": 0, "recompile": 1, "user_data": 2}
    items.sort(key=lambda x: (risk_order.get(x["risk"], 9), -x["size_bytes"]))
    return items


def analyze(disk: dict, trash: dict, cleanable: list) -> list:
    """根据收集数据生成健康问题（issues）。"""
    issues = []

    # 磁盘剩余
    if disk.get("used_pct") is not None:
        free_pct = 100 - disk["used_pct"]
        if free_pct < DISK_CRIT_PCT:
            issues.append({
                "severity": "critical",
                "dimension": "storage",
                "summary": f"磁盘剩余仅 {free_pct}%（{disk.get('avail', '?')}）",
                "remediation": "立即清理可清理项，避免写入失败",
                "check_id": "disk_free_critical",
            })
        elif free_pct < DISK_WARN_PCT:
            issues.append({
                "severity": "warning",
                "dimension": "storage",
                "summary": f"磁盘剩余 {free_pct}%（{disk.get('avail', '?')}）低于 20%",
                "remediation": "清理缓存/编译产物，或归档会话数据",
                "check_id": "disk_free_warning",
            })

    # 可清理项体量
    for item in cleanable:
        sev = "warning" if item["size_bytes"] > ITEM_WARN_BYTES else "info"
        detail = f" {item['detail']}" if item.get("detail") else ""
        issues.append({
            "severity": sev,
            "dimension": "storage",
            "summary": f"可清理: {item['size_human']} [{item['category']}] {item['path']}{detail}",
            "remediation": item["action"],
            "check_id": f"cleanable_{item['category']}",
        })

    # trash 超期
    for td in trash.get("trash_dirs", []):
        if td["stale_count"] > 0:
            issues.append({
                "severity": "info",
                "dimension": "storage",
                "summary": f"trash 有 {td['stale_count']} 个超期文件（{td['size_human']}）",
                "remediation": "清理超过 30 天的 trash 文件",
                "check_id": "trash_stale",
            })

    return issues


def run() -> dict:
    """完整存储检查：收集 → 分析 → 评分。"""
    disk = collect_disk()
    trash = collect_trash()
    cleanable = scan_cleanable()
    issues = analyze(disk, trash, cleanable)

    total_bytes = sum(i["size_bytes"] for i in cleanable)
    metrics = {
        "disk": disk,
        "trash": trash,
        "total_cleanable_bytes": total_bytes,
        "total_cleanable_human": _human(total_bytes),
        "cleanable_count": len(cleanable),
    }

    return {
        "score": score_dimension(issues, "storage"),
        "issues": issues,
        "cleanable_items": cleanable,
        "metrics": metrics,
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
