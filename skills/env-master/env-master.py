#!/usr/bin/env python3
"""
env-master.py — 统一 AI 编码环境自检系统入口。

整合 native-diag + env-doctor，提供：
- 统一入口：一个命令检查所有环境
- 量化评分：0-100 健康分 + 六维度雷达
- 多格式输出：json / table / minimal
- 跨环境盲区：PATH 冲突、skill 漂移、API 可达性、已知模式

用法:
    python3 env-master.py --env <name|all|storage> [--format json|table|minimal]
    python3 env-master.py --env all --dimensions install,config,auth
    python3 env-master.py --env storage                # 只查存储健康（D7）
    python3 env-master.py --env all --no-cross
    python3 env-master.py --env all --history
    python3 env-master.py --env claude --fix

退出码:
    0 = 全部 ok（或仅 info）
    1 = 存在 warning
    2 = 存在 critical / fail
    3 = 执行异常
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

# 确保能导入同级模块
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from adapters import get_adapter, ALL_ENVS
from scoring import score_environment, score_global, compute_remediation_priority
from formatter import format_output
from cross_checks import run_all_cross_checks
from storage_check import run as run_storage_check

HISTORY_FILE = SCRIPT_DIR / ".env-master-history.json"


def parse_args():
    parser = argparse.ArgumentParser(
        description="env-master — 统一 AI 编码环境自检系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 env-master.py --env all --format table
  python3 env-master.py --env claude,codex --format json
  python3 env-master.py --env all --no-cross --format minimal
  python3 env-master.py --env all --history
        """,
    )
    parser.add_argument(
        "--env",
        default="all",
        help="要检查的环境 (名称逗号分隔，或 all，默认: all)"
    )
    parser.add_argument(
        "--format", "-f",
        choices=["json", "table", "minimal"],
        default="table",
        help="输出格式 (默认: table)"
    )
    parser.add_argument(
        "--dimensions",
        default="install,config,auth,network,extensions,cross_env,storage",
        help="只检查指定维度 (逗号分隔，默认全部)"
    )
    parser.add_argument(
        "--no-cross", action="store_true",
        help="跳过跨环境检查（快速模式）"
    )
    parser.add_argument(
        "--history", action="store_true",
        help="与上次结果对比"
    )
    parser.add_argument(
        "--fix", action="store_true",
        help="自动修复可修复的问题（需确认）"
    )
    parser.add_argument(
        "--timeout", type=int, default=60,
        help="单个环境诊断超时秒数 (默认: 60)"
    )
    parser.add_argument(
        "--parallel", type=int, default=4,
        help="并行诊断的最大线程数 (默认: 4)"
    )
    return parser.parse_args()


def resolve_envs(env_arg: str) -> list:
    """解析 --env 参数。"""
    if env_arg.strip().lower() == "all":
        return list(ALL_ENVS)
    return [e.strip() for e in env_arg.split(",") if e.strip()]


def run_single_env(env_name: str, timeout: int = 60) -> dict:
    """运行单个环境的完整检查。"""
    adapter = get_adapter(env_name)
    if adapter is None:
        return {
            "env": env_name,
            "label": env_name,
            "installed": False,
            "version": None,
            "path": None,
            "config_dir": None,
            "native_cmd": "(none)",
            "overall_status": "warn",
            "issues": [{
                "severity": "warning",
                "summary": f"未知环境: {env_name}",
                "dimension": "install",
            }],
            "metrics": {},
            "notes": f"未注册的环境: {env_name}",
            "blind_spots": [],
        }

    try:
        return adapter.run_full_check()
    except Exception as e:
        return {
            "env": env_name,
            "label": adapter.label,
            "installed": False,
            "version": None,
            "path": None,
            "config_dir": None,
            "native_cmd": "(error)",
            "overall_status": "fail",
            "issues": [{
                "severity": "warning",
                "summary": f"诊断执行异常: {type(e).__name__}: {e}",
                "dimension": "install",
            }],
            "metrics": {},
            "notes": str(e),
            "blind_spots": [],
        }


def filter_issues_by_dimensions(issues: list, dimensions: list) -> list:
    """按维度过滤 issues。"""
    if not dimensions:
        return issues
    return [i for i in issues if i.get("dimension") in dimensions]


def load_history() -> Optional[dict]:
    """加载上次运行结果。"""
    if not HISTORY_FILE.exists():
        return None
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def save_history(report: dict):
    """保存本次结果到历史文件。"""
    try:
        # 只保存关键字段，避免历史文件过大
        snapshot = {
            "timestamp": report.get("timestamp"),
            "global_score": report.get("global_score"),
            "global_status": report.get("global_status"),
            "envs": {
                e.get("env"): {
                    "score": e.get("_scoring", {}).get("score"),
                    "status": e.get("overall_status"),
                }
                for e in report.get("environments", [])
            },
        }
        with open(HISTORY_FILE, "w") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def compute_trend(current: dict, previous: dict) -> Optional[dict]:
    """计算与上次结果的对比趋势。"""
    if not previous:
        return None
    prev_score = previous.get("global_score")
    curr_score = current.get("global_score")
    if prev_score is None or curr_score is None:
        return None
    delta = curr_score - prev_score
    return {
        "previous_score": prev_score,
        "current_score": curr_score,
        "delta": delta,
        "direction": "improved" if delta > 0 else ("degraded" if delta < 0 else "stable"),
        "previous_timestamp": previous.get("timestamp"),
    }


def main():
    args = parse_args()

    # 解析环境列表
    env_list = resolve_envs(args.env)
    if not env_list:
        print(json.dumps({"error": "未指定有效环境"}, ensure_ascii=False), file=sys.stderr)
        sys.exit(3)

    # storage 是主机级维度（D7），不是环境 —— 从环境列表剔除，单独运行
    want_storage = args.env.strip().lower() == "all" or "storage" in env_list
    env_list = [e for e in env_list if e != "storage"]

    # 解析维度
    dimensions = [d.strip() for d in args.dimensions.split(",") if d.strip()]

    # 并行调度各环境适配器
    env_results = []
    with ThreadPoolExecutor(max_workers=args.parallel) as executor:
        future_map = {
            executor.submit(run_single_env, env_name, args.timeout): env_name
            for env_name in env_list
        }
        for future in as_completed(future_map):
            env_name = future_map[future]
            try:
                result = future.result()
                env_results.append(result)
            except Exception as e:
                env_results.append({
                    "env": env_name,
                    "label": env_name,
                    "installed": False,
                    "version": None,
                    "path": None,
                    "config_dir": None,
                    "native_cmd": "(error)",
                    "overall_status": "fail",
                    "issues": [{
                        "severity": "warning",
                        "summary": f"执行异常: {e}",
                        "dimension": "install",
                    }],
                    "metrics": {},
                    "notes": str(e),
                    "blind_spots": [],
                })

    # 按原始顺序排序
    env_order = {name: idx for idx, name in enumerate(env_list)}
    env_results.sort(key=lambda x: env_order.get(x.get("env", ""), 999))

    # 按维度过滤 issues
    if dimensions:
        for env in env_results:
            env["issues"] = filter_issues_by_dimensions(env.get("issues", []), dimensions)

    # 跨环境检查
    installed_envs = [e["env"] for e in env_results if e.get("installed")]
    cross_results = run_all_cross_checks(
        envs_checked=installed_envs if installed_envs else env_list,
        no_network=args.no_cross,
    )

    # 将跨环境 issues 注入到对应环境
    cross_issues = cross_results.get("all_issues", [])
    for issue in cross_issues:
        if dimensions and issue.get("dimension") not in dimensions:
            continue
        # 找到对应环境，或注入到第一个已安装环境
        target_env = issue.get("env")
        placed = False
        for env in env_results:
            if target_env and env.get("env") == target_env:
                env.setdefault("issues", []).append(issue)
                placed = True
                break
        if not placed:
            # 注入到第一个环境
            if env_results:
                env_results[0].setdefault("issues", []).append(issue)

    # 评分
    for env in env_results:
        scoring = score_environment(env)
        env["_scoring"] = scoring
        env["score"] = scoring["score"]
        env["dimension_scores"] = scoring["dimension_scores"]

    # 主机级存储检查（D7）
    storage_scoring = None
    if want_storage and (not dimensions or "storage" in dimensions):
        storage_scoring = run_storage_check()

    # 全局评分（环境 90% + 存储 10%）
    global_scoring = score_global(
        env_results,
        storage_score=storage_scoring["score"] if storage_scoring else None,
    )

    # 优先修复清单（含主机级存储问题，排序逻辑在 scoring 层单一实现）
    remediation = compute_remediation_priority(
        env_results,
        extra_issues=storage_scoring["issues"] if storage_scoring else None,
    )

    # 历史对比
    previous = load_history() if args.history else None
    trend = compute_trend(
        {"global_score": global_scoring["global_score"]},
        previous,
    )

    # 构造最终报告
    report = {
        "schema_version": "1.0",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "global_score": global_scoring["global_score"],
        "global_status": global_scoring["global_status"],
        "dimension_radar": global_scoring["dimension_radar"],
        "environments": [
            {
                "env": e.get("env"),
                "label": e.get("label"),
                "installed": e.get("installed", False),
                "version": e.get("version"),
                "path": e.get("path"),
                "config_dir": e.get("config_dir"),
                "score": e.get("score", 0),
                "status": e.get("_scoring", {}).get("status", "ok"),
                "dimension_scores": e.get("dimension_scores", {}),
                "issues": e.get("issues", []),
                "metrics": e.get("metrics", {}),
            }
            for e in env_results
        ],
        "cross_env_findings": [
            {
                "check": k,
                "status": v.get("status", "unknown"),
                "issue_count": len(v.get("issues", [])),
            }
            for k, v in cross_results.items()
            if k != "all_issues"
        ],
        "remediation_priority": remediation,
    }

    if storage_scoring:
        report["storage"] = {
            "score": storage_scoring["score"],
            "issues": storage_scoring["issues"],
            "cleanable_items": storage_scoring["cleanable_items"],
            "metrics": storage_scoring["metrics"],
        }

    if trend:
        report["trend"] = trend

    # 保存历史
    save_history(report)

    # 输出
    output = format_output(report, args.format)
    print(output)

    # 退出码
    storage_issues = storage_scoring["issues"] if storage_scoring else []
    has_critical = any(
        i.get("severity") == "critical"
        for e in env_results
        for i in e.get("issues", [])
    ) or any(i.get("severity") == "critical" for i in storage_issues)
    has_warning = any(
        i.get("severity") == "warning"
        for e in env_results
        for i in e.get("issues", [])
    ) or any(i.get("severity") == "warning" for i in storage_issues)

    if has_critical:
        sys.exit(2)
    elif has_warning:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
