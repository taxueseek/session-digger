"""输出格式化 — json / table / minimal 三种格式。"""

import json
from typing import Optional


def format_json(report: dict) -> str:
    """完整 JSON 输出。"""
    return json.dumps(report, ensure_ascii=False, indent=2)


def format_minimal(report: dict) -> str:
    """一行摘要。"""
    global_score = report.get("global_score", 0)
    global_status = report.get("global_status", "unknown")
    envs = report.get("environments", [])
    installed = sum(1 for e in envs if e.get("installed"))
    total = len(envs)
    storage_score = report.get("storage", {}).get("score")
    storage_part = f" storage={storage_score}" if storage_score is not None else ""
    return f"global_score={global_score} status={global_status} envs={installed}/{total}{storage_part}"


def _display_width(s: str) -> int:
    """计算字符串的显示宽度（CJK 字符算 2）。"""
    w = 0
    for c in s:
        o = ord(c)
        # CJK 统一表意符号、全角字符等
        if o > 0x2E00:
            w += 2
        else:
            w += 1
    return w


def format_table(report: dict) -> str:
    """终端表格报告（带 Unicode 框线、进度条）。"""
    lines = []
    width = 58

    def hline(char="═"):
        return "╠" + char * width + "╣"

    def vline(text="", fill=" "):
        display_w = _display_width(text)
        padding = width - display_w
        if padding < 0:
            padding = 0
        return "║" + text + fill * padding + "║"

    # 标题
    lines.append("╔" + "═" * width + "╗")
    title = "env-master 统一环境自检报告"
    lines.append(vline(title))
    lines.append(hline())

    # 全局分数
    score = report.get("global_score", 0)
    status = report.get("global_status", "unknown").upper()
    bar_filled = score // 10
    bar_empty = 10 - bar_filled
    bar = "█" * bar_filled + "░" * bar_empty
    score_line = f"  全局健康分: {score}/100  [{bar}]  {status}"
    lines.append(vline(score_line))

    # 环境覆盖
    envs = report.get("environments", [])
    env_parts = []
    for e in envs:
        name = e.get("env", "?")
        if not e.get("installed"):
            env_parts.append(f"{name} ✗")
        elif e.get("overall_status") == "ok":
            env_parts.append(f"{name} ✓")
        else:
            env_parts.append(f"{name} ⚠")
    env_line = "  环境覆盖: " + "  ".join(env_parts)
    lines.append(vline(env_line))
    lines.append(hline())

    # 维度雷达
    radar = report.get("dimension_radar", {})
    dim_labels = {
        "install": "D1 安装",
        "config": "D2 配置",
        "auth": "D3 认证",
        "network": "D4 网络",
        "extensions": "D5 扩展",
        "cross_env": "D6 协调",
        "storage": "D7 存储",
    }
    has_envs = bool(envs)
    min_score = min(radar.values()) if radar else 0
    for dim_key, dim_label in dim_labels.items():
        if not has_envs and dim_key != "storage":
            continue  # 未检查环境时（--env storage），只显示存储维度
        s = radar.get(dim_key, 0)
        bar_f = s // 10
        bar_e = 10 - bar_f
        bar_str = "█" * bar_f + "░" * bar_e
        arrow = " ← 短板" if s == min_score and s < 80 else ""
        dim_line = f"  {dim_label}  {bar_str}  {s}{arrow}"
        lines.append(vline(dim_line))
    if not has_envs:
        lines.append(vline("  （环境维度未检查：--env storage 仅查存储）"))

    lines.append(hline("─"))

    # Issues 汇总（环境 + 主机级存储）
    all_issues = []
    for e in envs:
        for issue in e.get("issues", []):
            if issue.get("severity") == "ok":
                continue
            all_issues.append({
                "env": e.get("env", ""),
                **issue,
            })
    for issue in report.get("storage", {}).get("issues", []):
        if issue.get("severity") == "ok":
            continue
        all_issues.append({
            "env": "host",
            **issue,
        })

    criticals = [i for i in all_issues if i["severity"] == "critical"]
    warnings = [i for i in all_issues if i["severity"] == "warning"]
    infos = [i for i in all_issues if i["severity"] == "info"]

    lines.append(vline(f"  CRITICAL ({len(criticals)})"))
    lines.append(vline("  " + "─" * 50))
    for i in criticals[:5]:
        summary = i.get("summary", "")[:80]
        lines.append(vline(f"  ✗ [{i.get('dimension', '?')}] {summary}"))

    lines.append(vline(f"  WARNING ({len(warnings)})"))
    lines.append(vline("  " + "─" * 50))
    for i in warnings[:5]:
        summary = i.get("summary", "")[:80]
        lines.append(vline(f"  ⚠ [{i.get('dimension', '?')}] {summary}"))

    if infos:
        lines.append(vline(f"  INFO ({len(infos)})"))
        lines.append(vline("  " + "─" * 50))
        for i in infos[:3]:
            summary = i.get("summary", "")[:80]
            lines.append(vline(f"  ℹ [{i.get('dimension', '?')}] {summary}"))

    lines.append(hline("─"))

    # 优先修复
    remediation = report.get("remediation_priority", [])
    lines.append(vline("  优先修复:"))
    for idx, item in enumerate(remediation[:5], 1):
        rem = item.get("remediation", "")[:70]
        lines.append(vline(f"  {idx}. {rem}"))

    # 可清理项清单（storage 维度）
    storage = report.get("storage", {})
    cleanable = storage.get("cleanable_items", [])
    if cleanable:
        lines.append(hline("─"))
        total = storage.get("metrics", {}).get("total_cleanable_human", "?")
        lines.append(vline(f"  可清理项 ({len(cleanable)} 项, 共 {total}):"))
        risk_label = {"safe": "可直接清", "recompile": "需重编", "user_data": "需确认"}
        for item in cleanable[:6]:
            risk = risk_label.get(item.get("risk", "?"), "?")
            path_disp = item["path"]
            if len(path_disp) > 52:
                path_disp = path_disp[:49] + "..."
            lines.append(vline(
                f"  {item['size_human']:>9} [{risk}] {path_disp}"
            ))

    lines.append("╚" + "═" * width + "╝")
    return "\n".join(lines)


FORMATTERS = {
    "json": format_json,
    "table": format_table,
    "minimal": format_minimal,
}


def format_output(report: dict, fmt: str) -> str:
    """根据格式选择输出。"""
    fn = FORMATTERS.get(fmt, format_json)
    return fn(report)
