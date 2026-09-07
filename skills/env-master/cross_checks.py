"""跨环境检查 — 整合现有盲区脚本的逻辑。

复用 env-doctor 的四个脚本核心逻辑：
- check_network()       — API 可达性（参考 probe-network.sh）
- check_path_conflicts() — PATH 冲突（参考 cross-path-audit.sh）
- check_skill_drift()   — Skill 漂移（参考 drift-detector.py）
- check_known_patterns() — 已知问题模式匹配（参考 history-match.py + known-issues.json）
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import Optional

HOME = Path.home()
SCRIPT_DIR = Path(__file__).parent.resolve()

# 默认已知问题模式（内嵌精简版，避免外部依赖）
DEFAULT_KNOWN_PATTERNS = [
    {
        "id": "ENV-PATH-HARDCODE",
        "severity": "critical",
        "category": "config_health",
        "title": "配置文件中存在硬编码本机路径",
        "description": "skill 或 hook 中直接写入本机家目录绝对路径导致跨机器不可用。",
        "suggestion": "替换为 $HOME、环境变量、或 session-digger 的 Path resolution 模板。",
    },
    {
        "id": "ENV-HOOK-TIMEOUT",
        "severity": "warning",
        "category": "hook_health",
        "title": "Hook 脚本执行超时",
        "description": "全匹配 matcher '*' 同时挂载多个事件，累积延迟显著。",
        "suggestion": "缩小 matcher 范围，或确保 hook 脚本轻量。",
    },
    {
        "id": "ENV-ORPHANED-AUTH",
        "severity": "warning",
        "category": "config_health",
        "title": "认证文件过期或冗余",
        "description": "多环境并存时，auth.json / config.toml 可能包含过期 token、备份文件。",
        "suggestion": "清理 .bak 备份文件，移除不用的 provider 配置。",
    },
    {
        "id": "ENV-API-PROXY-CONFLICT",
        "severity": "critical",
        "category": "network_health",
        "title": "API 代理配置冲突",
        "description": "多个环境走不同链路，可能出现一个通一个不通的情况。",
        "suggestion": "统一检查所有环境的 base_url / proxy 可达性。",
    },
    {
        "id": "ENV-SKILL-DRIFT",
        "severity": "info",
        "category": "skill_health",
        "title": "Skill 安装漂移",
        "description": "同一个 skill 在多个目录可能安装了不同版本。",
        "suggestion": "选择其中一个作为真源，其他位置通过符号链接统一。",
    },
]


def check_network(timeout: int = 5) -> dict:
    """API 可达性探测（内联 probe-network.sh 核心逻辑）。"""
    URL_PATTERN = re.compile(
        r'(?:https?://)'
        r'(?:'
        r'(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+'
        r'[a-zA-Z]{2,}'
        r'|'
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}'
        r')'
        r'(?::\d{2,5})?'
        r'(?:/[^\s"\']*)?'
    )

    URL_KEY_HINTS = [
        'base_url', 'api_url', 'endpoint', 'api_base',
        'ANTHROPIC_BASE_URL', 'OPENAI_BASE_URL',
    ]

    # 发现配置文件中的 URL
    config_files = [
        HOME / ".claude/settings.json",
        HOME / ".codex/config.toml",
        HOME / ".grok/config.toml",
        HOME / ".kimi-code/config.toml",
        HOME / ".mimocode/config.toml",
    ]

    all_urls = OrderedDict()

    for path in config_files:
        if not path.exists():
            continue
        try:
            content = path.read_text(errors='replace')
        except Exception:
            continue
        for line in content.split('\n'):
            line = line.strip()
            if line.startswith('#'):
                continue
            for hint in URL_KEY_HINTS:
                m = re.search(
                    r'(?:["\']?' + re.escape(hint) + r'["\']?)\s*[=:]\s*["\']([^"\']+)["\']',
                    line, re.IGNORECASE
                )
                if m:
                    url = m.group(1)
                    if URL_PATTERN.match(url):
                        key = f"{path.name}:{hint}"
                        if url not in all_urls.values():
                            all_urls[key] = url

    # 环境变量
    for key, val in os.environ.items():
        if any(h.lower() in key.lower() for h in URL_KEY_HINTS):
            if URL_PATTERN.match(val) and val not in all_urls.values():
                all_urls[f"env:{key}"] = val

    # 探测
    results = []
    for source_key, url in all_urls.items():
        start = time.time()
        try:
            req = urllib.request.Request(url, method="HEAD")
            req.add_header("User-Agent", "env-master/1.0")
            resp = urllib.request.urlopen(req, timeout=timeout)
            latency = round(time.time() - start, 3)
            results.append({
                "url": url, "http_code": str(resp.status),
                "latency_s": latency, "status": "ok", "source": source_key,
            })
        except urllib.error.HTTPError as e:
            latency = round(time.time() - start, 3)
            results.append({
                "url": url, "http_code": str(e.code),
                "latency_s": latency,
                "status": "ok" if e.code < 500 else "degraded",
                "source": source_key,
            })
        except Exception as e:
            latency = round(time.time() - start, 3)
            results.append({
                "url": url, "http_code": "000",
                "latency_s": latency, "status": "unreachable",
                "error": str(e)[:100], "source": source_key,
            })

    return {
        "check": "network",
        "url_count": len(results),
        "results": results,
        "issues": _network_issues(results),
    }


def _network_issues(results: list) -> list:
    """从网络探测结果生成 issues。"""
    issues = []
    for r in results:
        if r["status"] == "unreachable":
            issues.append({
                "severity": "critical",
                "dimension": "network",
                "summary": f"API 不可达: {r['url']} ({r.get('error', 'unknown')})",
                "remediation": "检查网络连接或代理配置",
                "check_id": "network_unreachable",
            })
        elif r["latency_s"] > 3.0:
            issues.append({
                "severity": "warning",
                "dimension": "network",
                "summary": f"API 延迟偏高: {r['url']} ({r['latency_s']}s)",
                "remediation": "检查代理配置或更换网络",
                "check_id": "network_high_latency",
            })
    return issues


def check_path_conflicts() -> dict:
    """PATH 冲突检测（内联 cross-path-audit.sh 核心逻辑）。"""
    path_dirs = os.environ.get("PATH", "").split(":")

    TARGETS = [
        "claude", "codex", "grok", "kimi", "kimi-code",
        "cursor", "windsurf", "aider", "cline", "continue",
        "opencode", "mimo", "mimocode", "deepseek",
    ]

    all_found = {}
    conflicts = {}

    for target in TARGETS:
        found = []
        for d in path_dirs:
            p = Path(d) / target
            if p.exists() and os.access(p, os.X_OK):
                found.append(str(p))
        if found:
            all_found[target] = found[0]
            if len(found) > 1:
                conflicts[target] = found

    issues = []
    for cmd, paths in conflicts.items():
        issues.append({
            "severity": "warning",
            "dimension": "cross_env",
            "summary": f"{cmd} 在 PATH 中出现 {len(paths)} 次: {', '.join(paths)}",
            "remediation": f"只保留一个 {cmd} 路径，避免命令路由不确定性",
            "check_id": f"path_conflict_{cmd}",
        })

    return {
        "check": "path_conflicts",
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
        "all_found": all_found,
        "issues": issues,
    }


def check_skill_drift(threshold: int = 5) -> dict:
    """Skill 漂移检测（内联 drift-detector.py 核心逻辑）。"""
    candidates = [
        ("claude", HOME / ".claude/skills"),
        ("agents", HOME / ".agents/skills"),
        ("codex", HOME / ".codex/skills"),
        ("grok", HOME / ".grok/skills"),
        ("kimi", HOME / ".kimi-code/skills"),
    ]

    roots = {}
    for name, path in candidates:
        if path.exists() and path.is_dir():
            roots[name] = path

    if len(roots) < 2:
        return {
            "check": "skill_drift",
            "status": "skip",
            "reason": "少于 2 个 skill 根目录",
            "issues": [],
        }

    # 收集所有 skills
    all_skills = {}
    for env_name, root in roots.items():
        for item in sorted(root.iterdir()):
            if item.is_dir():
                skill_md = item / "SKILL.md"
                if skill_md.exists():
                    all_skills.setdefault(item.name, {})[env_name] = skill_md

    drifts = []
    for skill_name, locations in sorted(all_skills.items()):
        if len(locations) < 2:
            continue
        hashes = {}
        sizes = {}
        for env, path in locations.items():
            stat = path.stat()
            h = hashlib.md5()
            h.update(f"{stat.st_size}:{stat.st_mtime}".encode())
            try:
                with open(path, "rb") as f:
                    h.update(f.read(4096))
            except Exception:
                pass
            hashes[env] = h.hexdigest()[:12]
            sizes[env] = stat.st_size

        unique_hashes = set(hashes.values())
        if len(unique_hashes) > 1:
            size_vals = list(sizes.values())
            max_diff = max(abs(a - b) / max(b, 1) * 100 for a in size_vals for b in size_vals)
            if max_diff >= threshold:
                drifts.append({
                    "skill": skill_name,
                    "locations": {env: str(p) for env, p in locations.items()},
                    "sizes": sizes,
                    "max_diff_pct": round(max_diff, 1),
                })

    issues = []
    for d in drifts:
        issues.append({
            "severity": "info",
            "dimension": "cross_env",
            "summary": f"Skill '{d['skill']}' 存在安装漂移 (最大差异 {d['max_diff_pct']}%)",
            "remediation": "选择真源目录，其他位置通过符号链接统一",
            "check_id": f"skill_drift_{d['skill']}",
        })

    return {
        "check": "skill_drift",
        "status": "drift" if drifts else "clean",
        "drift_count": len(drifts),
        "drifts": drifts,
        "issues": issues,
    }


def check_known_patterns(envs_checked: list) -> dict:
    """已知问题模式匹配（内联 history-match.py + known-issues.json 核心逻辑）。"""
    # 尝试加载外部 known-issues.json
    known_issues_path = SCRIPT_DIR / "known-issues.json"
    patterns = DEFAULT_KNOWN_PATTERNS
    if known_issues_path.exists():
        try:
            with open(known_issues_path) as f:
                data = json.load(f)
                patterns = data.get("issue_patterns", patterns)
        except Exception:
            pass

    matched = []
    for p in patterns:
        # 匹配：pattern 与当前检查的环境相关
        text = (p.get("id", "") + p.get("title", "") + p.get("description", "")).lower()
        is_cross = p.get("category", "") in ("config_health", "network_health",
                                              "skill_health", "env_health")
        if is_cross:
            matched.append(p)
            continue
        for env in envs_checked:
            if env.lower() in text:
                matched.append(p)
                break

    issues = []
    for p in matched:
        issues.append({
            "severity": p.get("severity", "info"),
            "dimension": _pattern_dimension(p.get("category", "")),
            "summary": f"[{p.get('id', '')}] {p.get('title', '')}",
            "remediation": p.get("suggestion", ""),
            "check_id": f"known_{p.get('id', 'unknown')}",
        })

    return {
        "check": "known_patterns",
        "matched_count": len(matched),
        "patterns": matched,
        "issues": issues,
    }


def _pattern_dimension(category: str) -> str:
    """从 pattern category 映射到维度。"""
    mapping = {
        "config_health": "config",
        "network_health": "network",
        "skill_health": "extensions",
        "env_health": "cross_env",
        "tool_reliability": "config",
        "hook_health": "extensions",
        "session_health": "config",
    }
    return mapping.get(category, "cross_env")


def run_all_cross_checks(envs_checked: list, no_network: bool = False) -> dict:
    """运行所有跨环境检查。"""
    results = {
        "path": check_path_conflicts(),
        "drift": check_skill_drift(),
        "patterns": check_known_patterns(envs_checked),
    }
    if not no_network:
        results["network"] = check_network()
    else:
        results["network"] = {"check": "network", "status": "skipped", "issues": []}

    # 汇总所有 issues
    all_issues = []
    for check_name, check_result in results.items():
        for issue in check_result.get("issues", []):
            issue["source"] = check_name
            all_issues.append(issue)

    results["all_issues"] = all_issues
    return results
