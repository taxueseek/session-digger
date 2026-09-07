#!/usr/bin/env python3
"""Local-only prior-art engines: installed SKILL.md inventory, local-seek content search, session-digger usage signals.

Deterministic local collection — no network, no installs, no raw session dumps.
Consumed by research_prior_art.py; also runnable standalone for debugging.

Engines:
  local_meta  — scan installed SKILL.md inventories for name/description matches
  local_body  — content search inside skills roots via the local-seek script
  usage       — session-digger opportunity/gap JSON (structured counts only)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


DEFAULT_SKILLS_DIRS = [
    Path.home() / ".agents" / "skills",
    Path.home() / ".claude" / "skills",
]

_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_+-]{1,}|[0-9]+")
_STOP_WORDS = {
    "the", "and", "for", "with", "from", "this", "that", "skill", "skills",
    "use", "when", "user", "help", "需要", "使用", "帮我", "一个", "这个",
    "如何", "怎么", "进行", "相关", "功能",
}


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def tokenize(text: str) -> List[str]:
    if not text:
        return []
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def token_set(text: str) -> set:
    return {t for t in tokenize(text) if t not in _STOP_WORDS and len(t) > 1}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / float(len(a | b))


def overlap_count(a: set, b: set) -> int:
    return len(a & b)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def default_skills_dirs(extra: Optional[Sequence[str]] = None) -> List[Path]:
    dirs: List[Path] = []
    seen: set = set()
    for d in list(DEFAULT_SKILLS_DIRS) + [Path(p) for p in (extra or [])]:
        try:
            resolved = d.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        dirs.append(resolved)
    return dirs


def resolve_local_seek() -> Optional[Path]:
    env = __import__("os").environ.get("LOCAL_SEEK_SCRIPT")
    if env and Path(env).is_file():
        return Path(env)
    for base in (
        Path.home() / ".agents" / "skills" / "local-seek",
        Path.home() / ".claude" / "skills" / "local-seek",
    ):
        script = base / "scripts" / "seek.py"
        if script.is_file():
            return script
    return None


def resolve_digger_script(name: str) -> Optional[Path]:
    for base in (
        Path.home() / ".agents" / "skills" / "session-digger",
        Path.home() / ".claude" / "skills" / "session-digger",
    ):
        script = base / "scripts" / name
        if script.is_file():
            return script
    return None


# ---------------------------------------------------------------------------
# Command helpers
# ---------------------------------------------------------------------------

def run_cmd(argv: List[str], *, timeout: float, cwd: Optional[Path] = None) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return 124, out, "timeout"
    except OSError as exc:
        return 127, "", str(exc)


def parse_json_stdout(stdout: str) -> Any:
    text = (stdout or "").strip()
    if not text:
        raise ValueError("empty stdout")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for index, ch in enumerate(text):
        if ch in "{[":
            try:
                return json.loads(text[index:])
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON object in stdout")


# ---------------------------------------------------------------------------
# SKILL.md parsing (lightweight frontmatter, no PyYAML dependency)
# ---------------------------------------------------------------------------

def parse_skill_md(skill_path: Path) -> Tuple[str, str, str]:
    """Return (name, description, full_content) for a skill directory."""
    content = (skill_path / "SKILL.md").read_text(encoding="utf-8")
    if not content.startswith("---"):
        raise ValueError("SKILL.md missing frontmatter (no opening ---)")
    parts = content.split("---", 2)
    if len(parts) < 3:
        raise ValueError("SKILL.md frontmatter not closed with ---")
    frontmatter = parts[1]

    def scalar(key: str) -> str:
        match = re.search(rf"^{key}:\s*(.*)$", frontmatter, re.MULTILINE)
        if not match:
            return ""
        first = match.group(1).strip()
        if first in ("|", ">"):
            # Block scalar: collect following indented lines until a non-indented key.
            collected: List[str] = []
            for line in frontmatter[match.end():].splitlines():
                if line.strip() == "":
                    continue
                if line[:1] in (" ", "\t"):
                    collected.append(line.strip())
                else:
                    break
            return " ".join(collected)
        return first.strip().strip("'\"")

    name = scalar("name")
    description = scalar("description")
    return name, description, content


# ---------------------------------------------------------------------------
# Engine: local_meta — installed SKILL.md inventory
# ---------------------------------------------------------------------------

def scan_inventory(
    skills_dirs: List[Path],
    query: str,
    proposed_name: Optional[str] = None,
    *,
    max_hits: int = 12,
) -> Dict[str, Any]:
    qset = token_set(query)
    name_key = (proposed_name or "").strip().lower()
    hits: List[Dict[str, Any]] = []
    scanned = 0
    errors = 0

    for root in skills_dirs:
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            skill_md = child / "SKILL.md"
            if not skill_md.is_file():
                continue
            scanned += 1
            try:
                name, description, _ = parse_skill_md(child)
            except (ValueError, OSError):
                errors += 1
                continue
            name = name or child.name
            nset = token_set(name + " " + description)
            name_l = name.lower()
            dir_l = child.name.lower()

            exact = False
            if name_key and name_key in (name_l, dir_l):
                exact = True
            score = 0.0
            if exact:
                score = 1.0
            else:
                score = max(
                    jaccard(qset, nset),
                    jaccard(qset, token_set(name)),
                    0.85 if name_key and (name_key in name_l or name_key in dir_l) else 0.0,
                )
                ql = query.lower()
                if name_l and name_l in ql:
                    score = max(score, 0.75)
                if dir_l and dir_l.replace("-", " ") in ql:
                    score = max(score, 0.7)
                if any(t in name_l for t in qset if len(t) >= 4):
                    score = max(score, 0.55)

            if score < 0.28 and not exact:
                continue

            hits.append(
                {
                    "name": name,
                    "path": str(child),
                    "dir_name": child.name,
                    "description": (description or "")[:280],
                    "score": round(score, 4),
                    "exact_name": exact,
                    "source": "inventory",
                }
            )

    hits.sort(key=lambda h: (-h["score"], h["name"]))
    deduped: List[Dict[str, Any]] = []
    seen_names: set = set()
    for h in hits:
        key = (h.get("name") or h.get("dir_name") or "").lower()
        if not key or key in seen_names:
            continue
        seen_names.add(key)
        deduped.append(h)
    hits = deduped[:max_hits]
    return {
        "status": "ok",
        "scanned": scanned,
        "errors": errors,
        "skills_dirs": [str(d) for d in skills_dirs],
        "hits": hits,
    }


# ---------------------------------------------------------------------------
# Engine: local_body — local-seek content search
# ---------------------------------------------------------------------------

def channel_local_seek(
    query: str,
    skills_dirs: List[Path],
    *,
    max_hits: int = 15,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    script = resolve_local_seek()
    if not script:
        return {
            "status": "missing",
            "error": "local-seek seek.py not found under ~/.agents/skills/local-seek",
            "hits": [],
        }

    path = skills_dirs[0] if skills_dirs else Path.home() / ".agents" / "skills"
    argv = [
        sys.executable,
        str(script),
        query,
        "--path",
        str(path),
        "--type",
        "md",
        "--json",
        "--max",
        str(max_hits),
    ]
    code, out, err = run_cmd(argv, timeout=timeout)
    no_match_hint = ("未找到匹配" in (err or "")) or ("未找到匹配" in (out or ""))
    data: Any = None
    if (out or "").strip():
        try:
            data = parse_json_stdout(out)
        except ValueError:
            data = None

    if data is None:
        if code != 0 and (no_match_hint or code == 1):
            return {"status": "empty", "engine": "rg", "count": 0, "path": str(path), "hits": [], "exit_code": code}
        return {
            "status": "error",
            "error": (err or out or "local-seek failed")[:500],
            "exit_code": code,
            "hits": [],
        }

    results = data.get("results") if isinstance(data, dict) else None
    hits: List[Dict[str, Any]] = []
    if isinstance(results, list):
        for r in results:
            if not isinstance(r, dict):
                continue
            p = str(r.get("path") or "")
            skill_name = None
            try:
                parts = Path(p).parts
                if "skills" in parts:
                    i = parts.index("skills")
                    if i + 1 < len(parts):
                        skill_name = parts[i + 1]
            except Exception:
                skill_name = None
            hits.append(
                {
                    "path": p,
                    "line": r.get("line"),
                    "snippet": (r.get("snippet") or "")[:240],
                    "skill_hint": skill_name,
                    "source": "local-seek",
                }
            )

    return {
        "status": "ok" if hits else "empty",
        "engine": data.get("engine") if isinstance(data, dict) else "rg",
        "count": len(hits),
        "path": str(path),
        "hits": hits,
    }


# ---------------------------------------------------------------------------
# Engine: usage — session-digger opportunity/gap (structured only)
# ---------------------------------------------------------------------------

def _filter_relevant(
    items: List[Dict[str, Any]],
    query: str,
    *,
    text_keys: Sequence[str],
    max_items: int,
) -> List[Dict[str, Any]]:
    qset = token_set(query)
    if not qset:
        return items[:max_items]
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        blob = " ".join(str(item.get(k) or "") for k in text_keys)
        s = jaccard(qset, token_set(blob))
        s = max(s, overlap_count(qset, token_set(blob)) * 0.12)
        if s >= 0.12:
            scored.append((s, item))
    scored.sort(key=lambda x: -x[0])
    out: List[Dict[str, Any]] = []
    for s, item in scored[:max_items]:
        row = dict(item)
        row["_relevance"] = round(s, 4)
        if "evidence_sessions" in row and isinstance(row["evidence_sessions"], list):
            row["evidence_sessions_sample"] = row["evidence_sessions"][:3]
            row["evidence_sessions_count"] = len(row["evidence_sessions"])
            del row["evidence_sessions"]
        if "top_examples" in row and isinstance(row["top_examples"], list):
            row["top_examples"] = row["top_examples"][:2]
        out.append(row)
    return out


def channel_digger(
    query: str,
    *,
    min_sessions: int = 8,
    min_occurrences: int = 5,
    timeout: float = 120.0,
    skills_dirs: List[Path],
    max_items: int = 6,
) -> Dict[str, Any]:
    opp_script = resolve_digger_script("skill-opportunity-finder.py")
    gap_script = resolve_digger_script("skill-gap-finder.py")
    if not opp_script and not gap_script:
        return {
            "status": "missing",
            "error": "session-digger scripts not found",
            "opportunity_hits": [],
            "gap_hits": [],
        }

    warnings: List[str] = []
    opportunity_hits: List[Dict[str, Any]] = []
    gap_hits: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {}

    skills_dir_args: List[str] = []
    for d in skills_dirs[:2]:
        skills_dir_args.extend(["--skills-dir", str(d)])

    if opp_script:
        argv = [
            sys.executable,
            str(opp_script),
            "analyze",
            "--json-only",
            "--min-sessions",
            str(min_sessions),
            "--top",
            "2",
        ] + skills_dir_args
        code, out, err = run_cmd(argv, timeout=timeout)
        if code != 0:
            warnings.append(f"opportunity_error:{(err or out)[:200]}")
        else:
            try:
                data = parse_json_stdout(out)
                if isinstance(data, dict):
                    meta["opportunity"] = {
                        "sessions_analyzed": data.get("sessions_analyzed"),
                        "skills_scanned": data.get("skills_scanned"),
                        "proposals_total": len(data.get("proposals") or []),
                    }
                    raw = data.get("proposals") or []
                    if isinstance(raw, list):
                        opportunity_hits = _filter_relevant(
                            raw,
                            query,
                            text_keys=("theme", "matching_skill", "coverage", "priority"),
                            max_items=max_items,
                        )
            except ValueError as exc:
                warnings.append(f"opportunity_json:{exc}")

    if gap_script:
        argv = [
            sys.executable,
            str(gap_script),
            "analyze",
            "--min-occurrences",
            str(min_occurrences),
        ] + skills_dir_args
        code, out, err = run_cmd(argv, timeout=timeout)
        if code != 0:
            warnings.append(f"gap_error:{(err or out)[:200]}")
        else:
            try:
                data = parse_json_stdout(out)
                if isinstance(data, dict):
                    meta["gap"] = {
                        "sessions_analyzed": data.get("sessions_analyzed"),
                        "patterns_found": data.get("patterns_found"),
                        "skills_scanned": data.get("skills_scanned"),
                        "proposals_total": len(data.get("proposals") or []),
                    }
                    raw = data.get("proposals") or []
                    if isinstance(raw, list):
                        gap_hits = _filter_relevant(
                            raw,
                            query,
                            text_keys=("problem", "matched_skill", "suggested_skill_md_addition", "note"),
                            max_items=max_items,
                        )
            except ValueError as exc:
                warnings.append(f"gap_json:{exc}")

    status = "ok"
    if not opportunity_hits and not gap_hits:
        status = "empty" if not warnings else "degraded"

    return {
        "status": status,
        "note": "structured digger outputs only; raw sessions never included",
        "meta": meta,
        "warnings": warnings,
        "opportunity_hits": opportunity_hits,
        "gap_hits": gap_hits,
    }


# ---------------------------------------------------------------------------
# CLI (standalone debugging)
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local-only skill prior-art engines.")
    parser.add_argument("query", help="Primary intent / capability query.")
    parser.add_argument("--name", dest="proposed_name", help="Proposed skill name (exact match boost).")
    parser.add_argument("--skills-dir", action="append", default=[], help="Extra skills directory (repeatable).")
    parser.add_argument("--no-meta", action="store_true", help="Skip local_meta inventory engine.")
    parser.add_argument("--no-seek", action="store_true", help="Skip local_body content engine.")
    parser.add_argument("--no-digger", action="store_true", help="Skip usage engine.")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output.")
    parser.add_argument("--max-local", type=int, default=12)
    parser.add_argument("--max-content", type=int, default=15)
    parser.add_argument("--timeout-local", type=float, default=30.0)
    parser.add_argument("--timeout-digger", type=float, default=120.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    skills_dirs = default_skills_dirs(args.skills_dir)
    out: Dict[str, Any] = {"query": args.query, "skills_dirs": [str(d) for d in skills_dirs]}
    if not args.no_meta:
        out["local_meta"] = scan_inventory(skills_dirs, args.query, args.proposed_name, max_hits=args.max_local)
    if not args.no_seek:
        out["local_body"] = channel_local_seek(args.query, skills_dirs, max_hits=args.max_content, timeout=args.timeout_local)
    if not args.no_digger:
        out["usage"] = channel_digger(
            args.query,
            timeout=args.timeout_digger,
            skills_dirs=skills_dirs,
        )
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
