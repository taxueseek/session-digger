#!/usr/bin/env python3
"""Collect prior-art candidates from curated vertical catalogs and local engines, merge them by canonical repository, and optionally decide reuse/adapt/build/invent with labeled evidence."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable


SCRIPT_DIR = Path(__file__).resolve().parent


def _load_peer_module(module_name: str, filename: str, required: tuple[str, ...]) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_DIR / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        raise RuntimeError(f"{filename} is missing required functions: {', '.join(missing)}")
    return module


SKILLSMP = _load_peer_module("skill_search_skillsmp", "search_skillsmp.py", ("fetch",))
GITHUB = _load_peer_module(
    "skill_search_github", "search_github.py", ("fetch_repos", "fetch_official_catalog")
)
CLAWHUB = _load_peer_module("skill_search_clawhub", "search_clawhub.py", ("fetch",))
SEEK_LOCAL = _load_peer_module(
    "skill_seek_local",
    "skill_seek_local.py",
    ("default_skills_dirs", "scan_inventory", "channel_local_seek", "channel_digger"),
)

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SKILLS_SH_RE = re.compile(
    r"(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@(?P<skill>[^\s]+)\s+"
    r"(?P<installs>[0-9]+(?:\.[0-9]+)?[KMB]?)\s+installs",
    re.IGNORECASE,
)
MULTIPLIERS = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def strip_ansi(value: str) -> str:
    return ANSI_RE.sub("", value)


def parse_install_count(value: str) -> int:
    normalized = value.strip().upper()
    suffix = normalized[-1] if normalized and normalized[-1] in MULTIPLIERS else ""
    number = float(normalized[:-1] if suffix else normalized)
    return int(number * MULTIPLIERS.get(suffix, 1))


def parse_skills_sh_output(output: str, query: str) -> list[dict[str, Any]]:
    text = strip_ansi(output)
    candidates: list[dict[str, Any]] = []
    for match in SKILLS_SH_RE.finditer(text):
        repo = match.group("repo").lower()
        skill = match.group("skill")
        display = match.group("installs")
        candidates.append(
            {
                "source": "skills.sh",
                "query": query,
                "owner_repo": repo,
                "skill_name": skill,
                "family_key": f"{repo}:{skill.lower()}",
                "skills_sh_installs": parse_install_count(display),
                "skills_sh_installs_display": display,
                "skills_sh_url": f"https://skills.sh/{repo}/{skill}",
            }
        )
    return candidates


def run_skills_sh(query: str, timeout: float) -> list[dict[str, Any]]:
    try:
        completed = subprocess.run(
            ["npx", "--yes", "skills", "find", query],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("skills.sh unavailable: npx/node not installed") from exc
    if completed.returncode != 0:
        detail = strip_ansi(completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"skills.sh query failed ({completed.returncode}): {detail[:500]}")
    candidates = parse_skills_sh_output(completed.stdout, query)
    if not candidates:
        raise RuntimeError("skills.sh returned no parseable candidates")
    return candidates


def run_skillsmp(query: str, args: argparse.Namespace) -> list[dict[str, Any]]:
    request_args = SimpleNamespace(
        query=query,
        page=1,
        limit=args.skillsmp_limit,
        sort=args.skillsmp_sort,
        category=args.category,
        occupation=args.occupation,
        language=args.language,
        timeout=args.timeout,
        retries=args.retries,
        retry_backoff=args.retry_backoff,
        retry_jitter=args.retry_jitter,
    )
    result = SKILLSMP.fetch(request_args)
    output = []
    for candidate in result["candidates"]:
        item = dict(candidate)
        item["query"] = query
        output.append(item)
    return output


def run_github(query: str, args: argparse.Namespace) -> list[dict[str, Any]]:
    request_args = SimpleNamespace(
        query=query,
        topic=getattr(args, "github_topic", None),
        sort=getattr(args, "github_sort", "stars"),
        page=1,
        limit=getattr(args, "github_limit", 10),
        timeout=getattr(args, "timeout", 30.0),
        retries=getattr(args, "retries", 2),
        retry_backoff=getattr(args, "retry_backoff", 0.5),
        retry_jitter=getattr(args, "retry_jitter", 0.2),
    )
    result = GITHUB.fetch_repos(query, request_args)
    output = []
    for candidate in result["candidates"]:
        item = dict(candidate)
        item["query"] = query
        output.append(item)
    return output


def run_official_catalog(owner_repo: str, args: argparse.Namespace) -> list[dict[str, Any]]:
    request_args = SimpleNamespace(
        timeout=getattr(args, "timeout", 30.0),
        retries=getattr(args, "retries", 2),
        retry_backoff=getattr(args, "retry_backoff", 0.5),
        retry_jitter=getattr(args, "retry_jitter", 0.2),
    )
    result = GITHUB.fetch_official_catalog(owner_repo, request_args)
    output = []
    for candidate in result["candidates"]:
        item = dict(candidate)
        item["query"] = owner_repo
        output.append(item)
    return output


def run_clawhub(query: str, args: argparse.Namespace) -> list[dict[str, Any]]:
    request_args = SimpleNamespace(
        query=query,
        limit=getattr(args, "clawhub_limit", 10),
        timeout=getattr(args, "timeout", 30.0),
        retries=getattr(args, "retries", 2),
        retry_backoff=getattr(args, "retry_backoff", 0.5),
        retry_jitter=getattr(args, "retry_jitter", 0.2),
    )
    result = CLAWHUB.fetch(request_args)
    output = []
    for candidate in result["candidates"]:
        item = dict(candidate)
        item["query"] = query
        output.append(item)
    return output


def merge_candidates(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    families: dict[str, dict[str, Any]] = {}
    for record in records:
        key = str(record["family_key"])
        family = families.setdefault(
            key,
            {
                "family_key": key,
                "queries": [],
                "skills_sh": None,
                "skillsmp": None,
                "github": None,
                "github_official": None,
                "clawhub": None,
                "catalogs": [],
                "requires_source_review": True,
            },
        )
        query = str(record.get("query", ""))
        if query and query not in family["queries"]:
            family["queries"].append(query)
        source = record.get("source")
        if source == "skills.sh":
            current = family.get("skills_sh")
            if current is None or record.get("skills_sh_installs", 0) > current.get("skills_sh_installs", 0):
                family["skills_sh"] = record
        elif source == "skillsmp":
            current = family.get("skillsmp")
            if current is None or (record.get("repo_stars") or 0) > (current.get("repo_stars") or 0):
                family["skillsmp"] = record
        elif source == "github":
            current = family.get("github")
            if current is None or (record.get("repo_stars") or 0) > (current.get("repo_stars") or 0):
                family["github"] = record
        elif source == "github-official":
            current = family.get("github_official")
            if current is None:
                family["github_official"] = record
        elif source == "clawhub":
            current = family.get("clawhub")
            if current is None or (record.get("clawhub_downloads") or 0) > (current.get("clawhub_downloads") or 0):
                family["clawhub"] = record
        if source and source not in family["catalogs"]:
            family["catalogs"].append(source)

    def rank(item: dict[str, Any]) -> tuple[int, int, int, int, str]:
        skills_sh = item.get("skills_sh") or {}
        skillsmp = item.get("skillsmp") or {}
        github = item.get("github") or {}
        clawhub = item.get("clawhub") or {}
        return (
            -int(skills_sh.get("skills_sh_installs") or 0),
            -int(skillsmp.get("repo_stars") or 0),
            -int(github.get("repo_stars") or 0),
            -int(clawhub.get("clawhub_downloads") or 0),
            str(item["family_key"]),
        )

    output = list(families.values())
    for item in output:
        item["queries"].sort()
        item["catalogs"].sort()
    return sorted(output, key=rank)


def flatten_candidates(families: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten merged families into flat records for the decision stage.

    Each family keeps its per-catalog records nested; the decision stage consumes
    flat records with combined metric fields. Metrics stay separate by source — no
    unified score is invented, so ``score`` is deliberately None here.
    """
    flat: list[dict[str, Any]] = []
    for family in families:
        skills_sh = family.get("skills_sh") or {}
        skillsmp = family.get("skillsmp") or {}
        github = family.get("github") or {}
        github_official = family.get("github_official") or {}
        clawhub = family.get("clawhub") or {}
        best = skills_sh or skillsmp or github or github_official or clawhub or {}
        title = (
            clawhub.get("title")
            or (f"{skills_sh.get('owner_repo')}:{skills_sh.get('skill_name')}" if skills_sh else "")
            or best.get("name")
            or family.get("family_key", "")
        )
        flat.append(
            {
                "family_key": family.get("family_key", ""),
                "title": title,
                "name": best.get("name") or family.get("family_key", ""),
                "url": (
                    skills_sh.get("skills_sh_url")
                    or clawhub.get("url")
                    or github.get("github_url")
                    or github_official.get("github_url")
                    or skillsmp.get("skillsmp_url")
                    or ""
                ),
                "github_url": (
                    github.get("github_url")
                    or github_official.get("github_url")
                    or skillsmp.get("github_url")
                    or ""
                ),
                "catalogs": family.get("catalogs") or [],
                "skills_sh_installs": skills_sh.get("skills_sh_installs"),
                "skills_sh_installs_display": skills_sh.get("skills_sh_installs_display"),
                "repo_stars": (
                    skillsmp.get("repo_stars")
                    or github.get("repo_stars")
                    or github_official.get("repo_stars")
                    or 0
                ),
                "clawhub_downloads": clawhub.get("clawhub_downloads"),
                "score": None,
                "skillish": True,
            }
        )
    return flat


def research(
    args: argparse.Namespace,
    skills_sh_runner: Callable[[str, float], list[dict[str, Any]]] = run_skills_sh,
    skillsmp_runner: Callable[[str, argparse.Namespace], list[dict[str, Any]]] = run_skillsmp,
    github_runner: Callable[[str, argparse.Namespace], list[dict[str, Any]]] = run_github,
    official_catalog_runner: Callable[[str, argparse.Namespace], list[dict[str, Any]]] = run_official_catalog,
    clawhub_runner: Callable[[str, argparse.Namespace], list[dict[str, Any]]] = run_clawhub,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    query_runs: list[dict[str, Any]] = []
    missing_evidence: list[str] = []
    for query in args.queries:
        run: dict[str, Any] = {"query": query, "skills_sh": "not_run", "skillsmp": "not_run", "github": "not_run", "clawhub": "not_run"}
        if not getattr(args, "skip_skills_sh", False):
            try:
                found = skills_sh_runner(query, args.timeout)
                records.extend(found)
                run["skills_sh"] = "ok"
                run["skills_sh_candidates"] = len(found)
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                run["skills_sh"] = "error"
                run["skills_sh_error"] = str(exc)
                missing_evidence.append(f"skills.sh `{query}`: {exc}")
        if not getattr(args, "skip_skillsmp", False):
            try:
                found = skillsmp_runner(query, args)
                records.extend(found)
                run["skillsmp"] = "ok"
                run["skillsmp_candidates"] = len(found)
            except RuntimeError as exc:
                run["skillsmp"] = "error"
                run["skillsmp_error"] = str(exc)
                missing_evidence.append(f"SkillsMP `{query}`: {exc}")
        if not getattr(args, "skip_github", False):
            try:
                found = github_runner(query, args)
                records.extend(found)
                run["github"] = "ok"
                run["github_candidates"] = len(found)
            except RuntimeError as exc:
                run["github"] = "error"
                run["github_error"] = str(exc)
                missing_evidence.append(f"github `{query}`: {exc}")
        if not getattr(args, "skip_clawhub", False):
            try:
                found = clawhub_runner(query, args)
                records.extend(found)
                run["clawhub"] = "ok"
                run["clawhub_candidates"] = len(found)
            except RuntimeError as exc:
                run["clawhub"] = "error"
                run["clawhub_error"] = str(exc)
                missing_evidence.append(f"clawhub `{query}`: {exc}")
        query_runs.append(run)

    official_catalog_run: dict[str, Any] = {"catalog": getattr(args, "official_catalog", None), "status": "not_run"}
    if getattr(args, "official_catalog", None):
        try:
            found = official_catalog_runner(args.official_catalog, args)
            records.extend(found)
            official_catalog_run = {
                "catalog": args.official_catalog,
                "status": "ok",
                "candidates": len(found),
            }
        except RuntimeError as exc:
            official_catalog_run = {"catalog": args.official_catalog, "status": "error", "error": str(exc)}
            missing_evidence.append(f"official-catalog `{args.official_catalog}`: {exc}")

    families = merge_candidates(records)
    skipped_any = (
        getattr(args, "skip_skills_sh", False)
        or getattr(args, "skip_skillsmp", False)
        or getattr(args, "skip_github", False)
        or getattr(args, "skip_clawhub", False)
    )
    complete = not missing_evidence and not skipped_any
    ok = bool(families) and (complete or not args.strict)

    local_block = None
    if getattr(args, "local", False):
        local_block = run_local_engines(args)
    usage_block = None
    if getattr(args, "digger", False):
        usage_block = run_usage_engine(args)

    decision_block = None
    if getattr(args, "decide", False):
        decision_block = run_decision_stage(
            args,
            families,
            local_block,
            usage_block,
            query_runs,
            official_catalog_run,
        )

    return {
        "ok": ok,
        "complete": complete,
        "researched_at": date.today().isoformat(),
        "queries": args.queries,
        "official_catalog_run": official_catalog_run,
        "local": local_block,
        "usage": usage_block,
        "decision": decision_block,
        "metric_semantics": {
            "skills_sh_installs": "ecosystem install telemetry; not ratings or correctness",
            "skillsmp_repo_stars": "GitHub repository stars; not installs, ratings, or skill-specific quality",
            "github_repo_stars": "GitHub repository stars; popularity signal, not skill quality",
            "github_pushed_at": "repository maintenance recency; verify source SKILL.md before adoption",
            "github_official_catalog": "first-party curated catalog; presence means curated/published, not popularity",
            "clawhub_downloads": "ClawHub download telemetry; not correctness or skill quality",
            "local_inventory": "installed SKILL.md presence; local reuse signal, not external evidence",
            "local_seek_content": "content hits inside installed skills; suggestions only, open before adapt",
            "usage": "session-digger structured opportunity/gap counts; raw sessions never included",
            "cross_catalog_score": "not calculated; metrics remain separate",
        },
        "query_runs": query_runs,
        "candidate_family_count": len(families),
        "candidates": families,
        "missing_evidence": missing_evidence,
        "next_steps": [
            "shortlist by job relevance and role coverage, not combined popularity",
            "check local inventory for a reusable or extendable installed skill",
            "open canonical GitHub source and inspect SKILL.md before adoption",
            "verify license, maintenance, permissions, security, duplication, and rating evidence",
            "write keep/adapt/reject/invent synthesis before authoring",
        ],
    }


def run_local_engines(args: argparse.Namespace) -> dict[str, Any]:
    """local_meta + local_body: deterministic, read-only, no network."""
    skills_dirs = SEEK_LOCAL.default_skills_dirs(getattr(args, "skills_dir", None))
    primary = args.queries[0]
    inventory = SEEK_LOCAL.scan_inventory(
        skills_dirs,
        primary,
        getattr(args, "proposed_name", None),
        max_hits=getattr(args, "max_local", 12),
    )
    content = SEEK_LOCAL.channel_local_seek(
        primary,
        skills_dirs,
        max_hits=getattr(args, "max_content", 15),
        timeout=getattr(args, "local_seek_timeout", 30.0),
    )
    return {"inventory": inventory, "content": content}


def run_usage_engine(args: argparse.Namespace) -> dict[str, Any]:
    """usage: session-digger opportunity/gap signals, structured only."""
    skills_dirs = SEEK_LOCAL.default_skills_dirs(getattr(args, "skills_dir", None))
    return SEEK_LOCAL.channel_digger(
        args.queries[0],
        min_sessions=getattr(args, "digger_min_sessions", 8),
        min_occurrences=getattr(args, "digger_min_occurrences", 5),
        timeout=getattr(args, "digger_timeout", 120.0),
        skills_dirs=skills_dirs,
        max_items=6,
    )


# ---------------------------------------------------------------------------
# Decision stage: reuse / adapt / build / invent + labeled evidence
# ---------------------------------------------------------------------------

def decide(
    query: str,
    proposed_name: str | None,
    local_inv: dict[str, Any],
    local_content: dict[str, Any],
    remote: dict[str, Any],
    usage: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Return (decision, synthesis_list, evidence)."""
    inv_hits = local_inv.get("hits") or []
    content_hits = local_content.get("hits") or []
    remote_hits = remote.get("hits") or []
    opp = usage.get("opportunity_hits") or []
    gaps = usage.get("gap_hits") or []

    synthesis: list[dict[str, Any]] = []
    missing: list[str] = []

    top_local = inv_hits[0] if inv_hits else None
    strong_local = bool(
        top_local
        and (
            top_local.get("exact_name")
            or float(top_local.get("score") or 0) >= 0.72
        )
    )
    medium_local = bool(
        top_local and float(top_local.get("score") or 0) >= 0.45
    )

    # catalog hits are already skill-vertical; multi-catalog or high installs = strong
    skillish_remote = [h for h in remote_hits if h.get("skillish") is not False]

    def _catalog_strength(h: dict[str, Any]) -> float:
        installs = float(h.get("skills_sh_installs") or 0)
        stars = float(h.get("repo_stars") or 0)
        downloads = float(h.get("clawhub_downloads") or 0)
        multi = 1.0 + 0.12 * max(0, len(h.get("catalogs") or []) - 1)
        base = float(h.get("score") or 0)
        if installs >= 5000 or stars >= 500 or downloads >= 10000:
            base = max(base, 0.75)
        elif installs >= 500 or stars >= 50 or downloads >= 1000:
            base = max(base, 0.55)
        return base * multi

    ranked_remote = sorted(skillish_remote or remote_hits, key=_catalog_strength, reverse=True)
    strong_remote = bool(
        ranked_remote
        and (
            _catalog_strength(ranked_remote[0]) >= 0.55
            or len(ranked_remote) >= 3
            or (ranked_remote[0].get("skills_sh_installs") or 0) >= 1000
            or (ranked_remote[0].get("clawhub_downloads") or 0) >= 2000
        )
    )
    any_remote = bool(remote_hits)

    # usage: if covered by matching skill with high friction still → adapt local
    covered_usage = [
        o
        for o in opp
        if str(o.get("coverage") or "").lower() == "covered" and o.get("matching_skill")
    ]
    gap_for_new = [
        g
        for g in gaps
        if not g.get("matched_skill")
        or str(g.get("matched_skill")).lower() in ("none", "null", "")
    ]

    # Channel health: not_run is a scope choice (channel not requested), not an
    # evidence gap; explicit missing/error channels are recorded as missing evidence.
    if local_inv.get("status") not in ("ok", "not_run"):
        missing.append("local_inventory")
    if local_content.get("status") in ("missing", "error"):
        missing.append("local_seek")
    # empty local-seek is fine (zero content hits ≠ missing channel)
    if remote.get("status") in ("missing", "error"):
        missing.append("vertical_catalog")
    elif remote.get("status") == "degraded":
        for m in remote.get("missing") or []:
            missing.append(str(m))
    if usage.get("status") in ("missing",):
        missing.append("session_digger")

    action = "build"
    confidence = "medium"
    rationale_parts: list[str] = []
    target = None
    secondary = None

    if strong_local:
        action = "reuse"
        confidence = "high"
        target = top_local.get("path")
        rationale_parts.append(
            f"本地已有高匹配 skill「{top_local.get('name')}」(score={top_local.get('score')})"
        )
        synthesis.append(
            {
                "source": top_local.get("name"),
                "kind": "local",
                "verdict": "keep",
                "notes": "直接复用；创建前先验证是否真正覆盖意图",
            }
        )
        if medium_local and len(inv_hits) > 1:
            for h in inv_hits[1:3]:
                synthesis.append(
                    {
                        "source": h.get("name"),
                        "kind": "local",
                        "verdict": "reject" if float(h.get("score") or 0) < 0.5 else "adapt",
                        "notes": f"次匹配 score={h.get('score')}",
                    }
                )
    elif medium_local:
        action = "adapt"
        confidence = "medium"
        target = top_local.get("path")
        rationale_parts.append(
            f"本地部分匹配「{top_local.get('name')}」(score={top_local.get('score')})，优先改造而非新建"
        )
        synthesis.append(
            {
                "source": top_local.get("name"),
                "kind": "local",
                "verdict": "adapt",
                "notes": "扩展 description / 流程 / 脚本以覆盖新意图",
            }
        )
    elif covered_usage and not strong_local:
        # digger says theme already covered
        ms = covered_usage[0].get("matching_skill")
        action = "reuse"
        confidence = "medium"
        target = str(ms)
        rationale_parts.append(
            f"对话分析显示主题已被「{ms}」覆盖 (coverage=covered)"
        )
        synthesis.append(
            {
                "source": str(ms),
                "kind": "usage",
                "verdict": "keep",
                "notes": "usage 通道提示已有覆盖；请打开该 skill 核对",
            }
        )
    elif strong_remote and not medium_local:
        action = "adapt"
        confidence = "medium" if remote.get("status") in ("ok", "degraded") else "low"
        top_r = ranked_remote[0] if ranked_remote else remote_hits[0]
        target = top_r.get("url") or top_r.get("github_url") or top_r.get("title")
        secondary = "install_requires_user_confirm"
        inst = top_r.get("skills_sh_installs_display") or top_r.get("skills_sh_installs")
        stars = top_r.get("repo_stars")
        ch_dl = top_r.get("clawhub_downloads")
        metric_bits = []
        if inst is not None:
            metric_bits.append(f"skills.sh installs={inst}")
        if stars is not None:
            metric_bits.append(f"repo_stars={stars}")
        if ch_dl is not None:
            metric_bits.append(f"clawhub downloads={ch_dl}")
        metric_note = ("（" + "；".join(metric_bits) + "；指标≠质量）") if metric_bits else ""
        rationale_parts.append(
            f"垂直目录命中「{top_r.get('title') or top_r.get('name')}」{metric_note}，"
            "建议审源后适配（安装/删除须用户确认）"
        )
        for h in (ranked_remote or remote_hits)[:3]:
            notes = "打开源码审 SKILL.md；勿整包拷贝"
            if h.get("skills_sh_installs"):
                notes += f"；installs={h.get('skills_sh_installs_display') or h.get('skills_sh_installs')}"
            if h.get("repo_stars") is not None:
                notes += f"；stars={h.get('repo_stars')}"
            if h.get("clawhub_downloads") is not None:
                notes += f"；clawhub_dl={h.get('clawhub_downloads')}"
            synthesis.append(
                {
                    "source": h.get("title") or h.get("name") or h.get("url"),
                    "url": h.get("url") or h.get("github_url"),
                    "kind": "catalog",
                    "verdict": "adapt",
                    "notes": notes,
                }
            )
    elif any_remote and not medium_local:
        action = "build"
        confidence = "low"
        rationale_parts.append(
            "垂直目录有弱相关候选，不足以直接复用；可作参考后原创构建"
        )
        for h in (ranked_remote or remote_hits)[:2]:
            synthesis.append(
                {
                    "source": h.get("title") or h.get("url"),
                    "url": h.get("url") or h.get("github_url"),
                    "kind": "catalog",
                    "verdict": "reject",
                    "notes": "弱相关或低信号，仅作背景",
                }
            )
        synthesis.append(
            {
                "source": "new",
                "kind": "invent",
                "verdict": "invent",
                "notes": "无足够 prior-art，走创建路径",
            }
        )
    else:
        action = "build"
        confidence = "medium" if not missing else "low"
        rationale_parts.append("本地与远端均无足够匹配，建议新建")
        synthesis.append(
            {
                "source": "new",
                "kind": "invent",
                "verdict": "invent",
                "notes": "原创构建；仍过 Three Gates",
            }
        )

    # Content hits reinforce local
    if content_hits and action == "build":
        hints = {h.get("skill_hint") for h in content_hits if h.get("skill_hint")}
        hints.discard(None)
        if hints:
            rationale_parts.append(
                f"正文检索命中 skill 目录：{', '.join(sorted(list(hints))[:5])}"
            )
            if action == "build" and len(hints) == 1:
                action = "adapt"
                target = list(hints)[0]
                confidence = "low"
                synthesis.append(
                    {
                        "source": target,
                        "kind": "local-seek",
                        "verdict": "adapt",
                        "notes": "正文命中，建议打开核对后再决定 build",
                    }
                )

    if gap_for_new and action in ("reuse", "adapt"):
        rationale_parts.append(
            f"usage gap 仍有 {len(gap_for_new)} 条相关缺口信号，改造时一并考虑"
        )

    # Evidence status
    if missing and not (strong_local or medium_local):
        ev_status = "missing_evidence"
    elif strong_local or (medium_local and local_content.get("status") == "ok"):
        ev_status = "validated"
    elif action in ("build", "adapt") and (any_remote or opp or gaps):
        ev_status = "hypothesis"
    elif action == "reuse":
        ev_status = "validated"
    else:
        ev_status = "hypothesis"

    if remote.get("status") == "empty":
        missing.append("remote_empty")
    if usage.get("status") == "empty":
        # empty usage is not fatal
        pass

    decision = {
        "action": action,
        "confidence": confidence,
        "rationale": "；".join(rationale_parts) if rationale_parts else "无",
        # Public keys: `target` is the stable handoff field; `recommended_target` kept as alias.
        "target": target,
        "recommended_target": target,
        "flags": [secondary] if secondary else [],
        "query": query,
        "proposed_name": proposed_name,
    }
    evidence = {
        "status": ev_status,
        "missing": missing,
        "notes": (
            "digger counts and catalog installs/stars are decision signals only; "
            "never merged into a single quality score or written into skill prose as proof"
        ),
    }
    return decision, synthesis, evidence


def build_handoff(
    decision: dict[str, Any],
    evidence: dict[str, Any],
    synthesis: list[dict[str, Any]],
) -> dict[str, str]:
    syn_brief = ", ".join(
        f"{s.get('source')}:{s.get('verdict')}" for s in synthesis[:5]
    )
    return {
        "SEEK_ACTION": str(decision.get("action") or ""),
        "SEEK_TARGET": str(decision.get("recommended_target") or ""),
        "SEEK_EVIDENCE": str(evidence.get("status") or ""),
        "SEEK_CONFIDENCE": str(decision.get("confidence") or ""),
        "SEEK_SUMMARY": str(decision.get("rationale") or "")[:500],
        "SEEK_SYNTHESIS": syn_brief[:500],
        "SEEK_FLAGS": ",".join(decision.get("flags") or []),
    }


def run_decision_stage(
    args: argparse.Namespace,
    families: list[dict[str, Any]],
    local_block: dict[str, Any] | None,
    usage_block: dict[str, Any] | None,
    query_runs: list[dict[str, Any]],
    official_catalog_run: dict[str, Any],
) -> dict[str, Any]:
    """Turn merged candidates into a decision, synthesis, evidence, and handoff.

    Channels not requested (no ``--local``/``--digger``) pass through as
    ``not_run`` and are treated as scope choices, not evidence gaps.
    """
    local_inv = (local_block or {}).get("inventory") or {"status": "not_run", "hits": []}
    local_content = (local_block or {}).get("content") or {"status": "not_run", "hits": []}
    usage = usage_block or {"status": "not_run", "opportunity_hits": [], "gap_hits": []}

    failed = [
        ch
        for run in query_runs
        for ch in ("skills_sh", "skillsmp", "github", "clawhub")
        if run.get(ch) == "error"
    ]
    if official_catalog_run.get("status") == "error":
        failed.append("official_catalog")
    remote_status = "degraded" if failed else ("ok" if families else "empty")
    remote = {
        "hits": flatten_candidates(families),
        "status": remote_status,
        "missing": failed,
    }

    decision, synthesis, evidence = decide(
        query=args.queries[0],
        proposed_name=getattr(args, "proposed_name", None),
        local_inv=local_inv,
        local_content=local_content,
        remote=remote,
        usage=usage,
    )
    handoff = build_handoff(decision, evidence, synthesis)
    return {
        "decision": decision,
        "synthesis": synthesis,
        "evidence": evidence,
        "handoff": handoff,
    }


def summary_view(result: dict[str, Any]) -> dict[str, Any]:
    local = result.get("local")
    usage = result.get("usage")
    decision = (result.get("decision") or {}).get("decision")
    evidence = (result.get("decision") or {}).get("evidence")
    handoff = (result.get("decision") or {}).get("handoff")
    return {
        "ok": result["ok"],
        "complete": result["complete"],
        "researched_at": result["researched_at"],
        "queries": result["queries"],
        "official_catalog_run": result["official_catalog_run"],
        "candidate_family_count": result["candidate_family_count"],
        "query_runs": result["query_runs"],
        "local": {
            "status": local.get("inventory", {}).get("status") if local else None,
            "inventory_hits": len(local.get("inventory", {}).get("hits") or []) if local else 0,
            "content_status": local.get("content", {}).get("status") if local else None,
        },
        "usage": {
            "status": usage.get("status") if usage else None,
            "opportunity_hits": len(usage.get("opportunity_hits") or []) if usage else 0,
            "gap_hits": len(usage.get("gap_hits") or []) if usage else 0,
        },
        "decision": {
            "action": decision.get("action"),
            "confidence": decision.get("confidence"),
            "target": decision.get("target"),
            "evidence": evidence.get("status"),
            "rationale": decision.get("rationale"),
            "handoff": handoff,
        }
        if decision and evidence and handoff
        else None,
        "missing_evidence": result["missing_evidence"],
        "full_output": "written to --output" if result.get("_has_output") else "use --output to preserve candidates",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Research and normalize prior-art skills across curated catalogs and local engines.")
    parser.add_argument("queries", nargs="+", help="One to four intent-shaped search queries.")
    parser.add_argument("--skillsmp-limit", type=int, default=10, choices=range(1, 51), metavar="1-50")
    parser.add_argument("--skillsmp-sort", choices=("stars", "recent"), default="stars")
    parser.add_argument("--github-topic", help="Restrict GitHub repository search to a topic (e.g. claude-skills).")
    parser.add_argument("--github-limit", type=int, default=10, choices=range(1, 101), metavar="1-100")
    parser.add_argument("--github-sort", choices=("stars", "updated"), default="stars")
    parser.add_argument("--clawhub-limit", type=int, default=10, choices=range(1, 51), metavar="1-50")
    parser.add_argument("--official-catalog", metavar="OWNER/REPO[/PATH]", help="Also list skills of a first-party catalog repo (e.g. anthropics/skills/skills).")
    parser.add_argument("--category")
    parser.add_argument("--occupation")
    parser.add_argument("--language")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=2, choices=range(0, 6), metavar="0-5")
    parser.add_argument("--retry-backoff", type=float, default=0.5)
    parser.add_argument("--retry-jitter", type=float, default=0.2)
    parser.add_argument("--skip-skills-sh", action="store_true")
    parser.add_argument("--skip-skillsmp", action="store_true")
    parser.add_argument("--skip-github", action="store_true")
    parser.add_argument("--skip-clawhub", action="store_true")
    parser.add_argument("--local", action="store_true", help="Also scan installed SKILL.md inventory + local-seek content (read-only).")
    parser.add_argument("--digger", action="store_true", help="Also query session-digger opportunity/gap JSON (structured counts only).")
    parser.add_argument("--name", dest="proposed_name", help="Proposed skill name (exact match boost for local inventory).")
    parser.add_argument("--skills-dir", action="append", default=[], help="Extra / override skills directory for local engines (repeatable).")
    parser.add_argument("--max-local", type=int, default=12)
    parser.add_argument("--max-content", type=int, default=15)
    parser.add_argument("--local-seek-timeout", type=float, default=30.0)
    parser.add_argument("--digger-timeout", type=float, default=120.0)
    parser.add_argument("--digger-min-sessions", type=int, default=8)
    parser.add_argument("--digger-min-occurrences", type=int, default=5)
    parser.add_argument("--strict", action="store_true", help="Fail unless every enabled catalog succeeds for every query.")
    parser.add_argument("--decide", action="store_true", help="Also run the decision stage: reuse/adapt/build/invent + evidence status + creation handoff fields.")
    parser.add_argument("--summary", action="store_true", help="Print only run status; --output still receives full JSON.")
    parser.add_argument("--output", help="Optional JSON output path.")
    args = parser.parse_args()
    if len(args.queries) > 4:
        parser.error("use at most four intent-shaped queries per research run")
    if args.skip_skills_sh and args.skip_skillsmp and args.skip_github and args.skip_clawhub:
        parser.error("cannot skip all four catalogs")
    if args.retry_backoff < 0 or args.retry_jitter < 0:
        parser.error("retry delay values must be non-negative")
    return args


def main() -> None:
    args = parse_args()
    result = research(args)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result["_has_output"] = True
    rendered = summary_view(result) if args.summary else result
    print(json.dumps(rendered, ensure_ascii=False, indent=2))
    if not result["ok"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
