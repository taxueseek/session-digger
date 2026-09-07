"""评分引擎 — 量化各环境健康度。

规则：
- 扣分制：critical -25, warning -10, info -5，下限 0
- 六环境维度独立评分：install, config, auth, network, extensions, cross_env
- 第七维度 storage 为「主机级」维度，由 storage_check 单独评分、并入全局分
- 权重：install 13%, config 22%, auth 18%, network 14%, extensions 13%,
  cross_env 10%, storage 10%（环境部分合计 90%，主机级存储占 10%）
- 全局分 = 环境加权平均 × 0.9 + storage_score × 0.1
"""

from typing import Optional

# 维度权重（含主机级 storage）
DIMENSION_WEIGHTS = {
    "install": 0.13,
    "config": 0.22,
    "auth": 0.18,
    "network": 0.14,
    "extensions": 0.13,
    "cross_env": 0.10,
    "storage": 0.10,
}

# 环境维度（不含主机级 storage）
ENV_DIMENSIONS = [d for d in DIMENSION_WEIGHTS if d != "storage"]

ALL_DIMENSIONS = list(DIMENSION_WEIGHTS.keys())

# 扣分规则
SEVERITY_PENALTY = {
    "critical": 25,
    "warning": 10,
    "info": 5,
    "ok": 0,
}


def score_dimension(issues: list, dimension: str) -> int:
    """计算单个维度的分数。"""
    score = 100
    for issue in issues:
        if issue.get("dimension") != dimension:
            continue
        sev = issue.get("severity", "info")
        penalty = SEVERITY_PENALTY.get(sev, 0)
        score -= penalty
    return max(0, score)


def score_environment(env_result: dict) -> dict:
    """计算单个环境的所有维度分数和总分（仅环境维度）。"""
    issues = env_result.get("issues", [])

    dimension_scores = {}
    for dim in ENV_DIMENSIONS:
        dimension_scores[dim] = score_dimension(issues, dim)

    # 加权总分（环境维度合计权重 0.9，换算回 0-100）
    total = 0.0
    for dim in ENV_DIMENSIONS:
        total += dimension_scores[dim] * DIMENSION_WEIGHTS[dim]
    total /= 0.9  # 归一化，使环境分独立于 storage 权重

    final_score = round(total)
    final_score = max(0, min(100, final_score))

    # 状态判定
    if final_score >= 85:
        status = "ok"
    elif final_score >= 60:
        status = "warn"
    else:
        status = "fail"

    return {
        "score": final_score,
        "status": status,
        "dimension_scores": dimension_scores,
    }


def score_global(env_results: list, storage_score: Optional[int] = None) -> dict:
    """计算全局分数。

    环境维度：各环境等权平均。
    主机级 storage：单独评分，占全局分 10%。
    全局分 = env_avg × 0.9 + storage_score × 0.1（storage_score 缺省时退化为纯环境分）。
    """
    if not env_results and storage_score is None:
        radar = {d: 0 for d in ALL_DIMENSIONS}
        return {
            "global_score": 0,
            "global_status": "fail",
            "dimension_radar": radar,
        }

    total_score = 0.0
    radar_sums = {d: 0.0 for d in ENV_DIMENSIONS}
    count = 0

    for env in env_results:
        scoring = env.get("_scoring", {})
        if not scoring:
            continue
        total_score += scoring.get("score", 0)
        count += 1
        for dim in ENV_DIMENSIONS:
            radar_sums[dim] += scoring.get("dimension_scores", {}).get(dim, 0)

    # 环境平均分（无环境时按 0 计）
    env_avg = (total_score / count) if count else 0

    # 主机级 storage 分
    if storage_score is None:
        storage_score = 100  # 未检查时视为健康，不拖累全局分
    radar = {d: round(radar_sums[d] / count) if count else 0 for d in ENV_DIMENSIONS}
    radar["storage"] = storage_score

    if count == 0:
        # 只有存储维度（--env storage）：全局分退化为存储分，环境维度不参与
        status = "ok" if storage_score >= 85 else ("warn" if storage_score >= 60 else "fail")
        return {
            "global_score": storage_score,
            "global_status": status,
            "dimension_radar": radar,
        }

    # 全局分 = 环境 90% + 存储 10%
    global_score = round(env_avg * 0.9 + storage_score * 0.1)
    global_score = max(0, min(100, global_score))

    if global_score >= 85:
        global_status = "ok"
    elif global_score >= 60:
        global_status = "warn"
    else:
        global_status = "fail"

    return {
        "global_score": global_score,
        "global_status": global_status,
        "dimension_radar": radar,
    }


def compute_remediation_priority(env_results: list, extra_issues: Optional[list] = None) -> list:
    """生成优先修复清单（按 severity 和维度权重排序）。

    extra_issues: 主机级问题（如 storage），env 记为 "host"。
    """
    all_issues = []
    for env in env_results:
        env_name = env.get("env", "unknown")
        for issue in env.get("issues", []):
            sev = issue.get("severity", "info")
            if sev == "ok":
                continue
            all_issues.append({
                "env": env_name,
                "severity": sev,
                "dimension": issue.get("dimension", "config"),
                "summary": issue.get("summary", ""),
                "remediation": issue.get("remediation", ""),
                "check_id": issue.get("check_id", ""),
            })

    for issue in extra_issues or []:
        sev = issue.get("severity", "info")
        if sev == "ok":
            continue
        all_issues.append({
            "env": "host",
            "severity": sev,
            "dimension": issue.get("dimension", "storage"),
            "summary": issue.get("summary", ""),
            "remediation": issue.get("remediation", ""),
            "check_id": issue.get("check_id", ""),
        })

    # 排序：critical > warning > info，然后按维度权重
    sev_order = {"critical": 0, "warning": 1, "info": 2}

    def sort_key(item):
        sev_rank = sev_order.get(item["severity"], 99)
        dim_weight = DIMENSION_WEIGHTS.get(item["dimension"], 0)
        return (sev_rank, -dim_weight)

    all_issues.sort(key=sort_key)
    return all_issues[:10]  # 最多返回 10 条
