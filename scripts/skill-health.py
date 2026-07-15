#!/usr/bin/env python3
"""
skill-health.py — session-digger self-check for "skill improves skill".

Layer-3 companion to skill-gap-finder:
  - gap-finder mines session pain points
  - skill-health audits the skill *asset* itself (routing, hardcoding, install drift)

Never prints absolute home paths with usernames. Never auto-edits SKILL.md.

Usage:
  python3 scripts/skill-health.py
  python3 scripts/skill-health.py --root "$SESSION_DIGGER_ROOT"
"""

from _common import read_json, write_json, read_text, json_out
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


# Split so this file does not match its own detector source.
_PERSONAL_MARKERS = (
    "Documents" + "/GPT/session-digger",
    "/Users/",  # followed by username segment checked below
    "/home/",
)


def _resolve_root(explicit: str | None) -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    for env in ("SESSION_DIGGER_ROOT", "CLAUDE_PLUGIN_ROOT", "HERDR_PLUGIN_ROOT"):
        v = os.environ.get(env)
        if v:
            candidates.append(Path(v))
    candidates.append(Path(__file__).resolve().parent.parent)
    for c in (
        Path.home() / ".agents/skills/session-digger",
        Path.home() / ".claude/plugins/session-digger",
        Path.home() / ".claude/skills/session-digger",
        Path.home() / ".grok/skills/session-digger",
    ):
        candidates.append(c)
    for c in candidates:
        if (c / "scripts" / "sd-recall.py").is_file():
            return c.resolve()
    raise SystemExit("Cannot resolve session-digger root. Set SESSION_DIGGER_ROOT.")


def _git_sha(root: Path) -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return out or None
    except Exception:
        return None


def _skill_version(root: Path) -> str | None:
    skill = root / "SKILL.md"
    if not skill.is_file():
        return None
    m = re.search(r"^version:\s*(\S+)", read_text(skill), re.M)
    return m.group(1) if m else None


def _command_names(root: Path) -> list[str]:
    names = []
    for p in sorted((root / "commands").glob("*.md")):
        names.append(p.stem)
    return names


def _routes_in_skill(root: Path) -> set[str]:
    text = (root / "SKILL.md").read_text(encoding="utf-8") if (root / "SKILL.md").is_file() else ""
    # match `/foo` or bare command names in table cells
    found = set(re.findall(r"`(/[a-z0-9-]+)`", text))
    found |= set(re.findall(r"\| `/([a-z0-9-]+)", text))
    # normalize without leading slash for comparison convenience
    return {f[1:] if f.startswith("/") else f for f in found} | {
        f[1:] for f in found if f.startswith("/")
    }


def _scan_hardcodes(root: Path) -> list[dict]:
    hits = []
    for rel in ("commands", "agents", "skills", "scripts", "SKILL.md", "README.md", "CLAUDE.md"):
        base = root / rel
        paths = [base] if base.is_file() else list(base.rglob("*")) if base.is_dir() else []
        for p in paths:
            if not p.is_file():
                continue
            if p.suffix not in {".md", ".py", ".sh", ".toml", ".json", ""} and p.name not in {
                "SKILL.md",
                "README.md",
                "CLAUDE.md",
            }:
                if p.suffix not in {".md", ".py", ".sh", ".toml", ".json"}:
                    continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if p.name == "skill-health.py":
                continue
            for i, line in enumerate(text.splitlines(), 1):
                bad = False
                if _PERSONAL_MARKERS[0] in line:
                    bad = True
                elif "/Users/" in line and re.search(r"/Users/[A-Za-z0-9._-]+/", line):
                    # allow generic placeholders
                    if "/Users/<" in line or "/Users/test/" in line or "/Users/joker/" in line or "/Users/alice/" in line:
                        bad = False
                    else:
                        bad = True
                elif "/home/" in line and re.search(r"/home/[A-Za-z0-9._-]+/", line):
                    if "/home/<" in line or "/home/test/" in line:
                        bad = False
                    else:
                        bad = True
                if not bad:
                    continue
                safe = line.strip()
                safe = re.sub(r"/Users/[A-Za-z0-9._-]+", "/Users/<user>", safe)
                safe = re.sub(r"/home/[A-Za-z0-9._-]+", "/home/<user>", safe)
                safe = safe.replace(_PERSONAL_MARKERS[0], "<personal-path>")[:160]
                hits.append(
                    {
                        "file": str(p.relative_to(root)),
                        "line": i,
                        "snippet": safe,
                    }
                )
    return hits


def _combo_coverage(root: Path, commands: list[str]) -> list[str]:
    combo_path = root / "combo_map.json"
    if not combo_path.is_file():
        return commands[:]
    try:
        data = read_json(combo_path)
    except Exception:
        return commands[:]
    keys = set()
    for k in (data.get("combos") or {}):
        keys.add(k[1:] if k.startswith("/") else k)
    missing = [c for c in commands if c not in keys and c.replace("_", "-") not in keys]
    return missing


def _install_drift(root: Path) -> list[dict]:
    """Compare common install copies to this root (sha only, no paths with user if possible)."""
    findings = []
    root_sha = _git_sha(root)
    for label, candidate in (
        ("agents_skills", Path.home() / ".agents/skills/session-digger"),
        ("claude_plugins", Path.home() / ".claude/plugins/session-digger"),
        ("claude_skills", Path.home() / ".claude/skills/session-digger"),
    ):
        if not (candidate / "scripts" / "sd-recall.py").is_file():
            findings.append({"install": label, "status": "absent"})
            continue
        if candidate.resolve() == root.resolve():
            findings.append({"install": label, "status": "same_as_root", "sha": root_sha})
            continue
        sha = _git_sha(candidate)
        ver = _skill_version(candidate)
        status = "ok" if sha and root_sha and sha == root_sha else "drift"
        findings.append(
            {
                "install": label,
                "status": status,
                "sha": sha,
                "version": ver,
                "root_sha": root_sha,
            }
        )
    return findings


def _herdr_checks(root: Path) -> list[dict]:
    """Validate Herdr surface: manifest wiring + script portability/privacy."""
    issues = []
    toml = root / "herdr-plugin.toml"
    if not toml.is_file():
        return [{"ok": False, "issue": "herdr-plugin.toml missing"}]
    text = read_text(toml)

    if "skill-gap-finder.py" in text:
        if "analyze" not in text:
            issues.append({"ok": False, "issue": "herdr sd-skill-gap missing analyze subcommand"})
        else:
            issues.append({"ok": True, "issue": "herdr skill-gap analyze wired"})

    if "skill-health.py" in text:
        issues.append({"ok": True, "issue": "herdr sd-skill-health present"})
    else:
        issues.append({"ok": False, "issue": "herdr missing sd-skill-health action"})

    for rel in (
        "scripts/herdr-search.sh",
        "scripts/herdr-fuzzy-search.sh",
        "scripts/herdr-event.sh",
        "scripts/herdr-stats-pane.py",
    ):
        if not (root / rel).is_file():
            issues.append({"ok": False, "issue": f"missing {rel}"})
            continue
        body = (root / rel).read_text(encoding="utf-8", errors="replace")
        if ".agents/skills/session-digger" in body:
            issues.append({"ok": False, "issue": f"{rel} hardcodes personal path"})
        if "HERDR_PLUGIN_ROOT" not in body and "SESSION_DIGGER_ROOT" not in body:
            issues.append({"ok": False, "issue": f"{rel} missing plugin root env fallback"})
        if rel.endswith("herdr-fuzzy-search.sh"):
            if 'Path.home() / ".claude" / "projects"' in body or "Path.home() / '.claude' / 'projects'" in body:
                if "rglob" in body:
                    issues.append({"ok": False, "issue": f"{rel} still claude-only message lookup"})

    if not issues:
        issues.append({"ok": True, "issue": "herdr surface checks passed"})
    return issues


def main():
    ap = argparse.ArgumentParser(description="session-digger skill asset health check")
    ap.add_argument("--root", default=None, help="session-digger root (or SESSION_DIGGER_ROOT)")
    ap.add_argument("--json", action="store_true", help="JSON only")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 when proposals exist (CI). Default exit 0 so Herdr actions are not marked failed.",
    )
    args = ap.parse_args()
    root = _resolve_root(args.root)

    commands = _command_names(root)
    routes = _routes_in_skill(root)
    # routes set may include bare names; commands are bare
    missing_routes = [c for c in commands if c not in routes and f"/{c}" not in routes]
    # some skill tables use /name — routes stored without slash
    missing_routes = [c for c in commands if c not in routes]

    hardcodes = _scan_hardcodes(root)
    combo_missing = _combo_coverage(root, commands)
    drift = _install_drift(root)
    herdr = _herdr_checks(root)

    proposals = []
    if missing_routes:
        proposals.append(
            {
                "problem": f"Root SKILL.md route table missing {len(missing_routes)} command(s).",
                "evidence_count": len(missing_routes),
                "missing": missing_routes,
                "suggested_skill_md_addition": (
                    "Add routing rows for: " + ", ".join(f"`/{m}`" for m in missing_routes)
                ),
                "matched_skill": {"name": "session-digger", "match_confidence": "high"},
            }
        )
    if hardcodes:
        proposals.append(
            {
                "problem": f"Found {len(hardcodes)} personal/absolute path hardcode(s).",
                "evidence_count": len(hardcodes),
                "samples": hardcodes[:10],
                "suggested_skill_md_addition": (
                    "Replace hardcodes with Path resolution "
                    "(SESSION_DIGGER_ROOT / CLAUDE_PLUGIN_ROOT / common skill installs)."
                ),
                "matched_skill": {"name": "session-digger", "match_confidence": "high"},
            }
        )
    if combo_missing:
        proposals.append(
            {
                "problem": f"combo_map.json missing entries for {len(combo_missing)} command(s).",
                "evidence_count": len(combo_missing),
                "missing": combo_missing,
                "suggested_skill_md_addition": "Add combo keys for: " + ", ".join(combo_missing),
                "matched_skill": {"name": "session-digger", "match_confidence": "high"},
            }
        )
    drifted = [d for d in drift if d.get("status") == "drift"]
    if drifted:
        proposals.append(
            {
                "problem": f"{len(drifted)} install copy(ies) differ from this root SHA.",
                "evidence_count": len(drifted),
                "installs": drifted,
                "suggested_skill_md_addition": (
                    "Sync installs to root or export SESSION_DIGGER_ROOT to the intended copy."
                ),
                "matched_skill": {"name": "session-digger", "match_confidence": "high"},
            }
        )
    for h in herdr:
        if not h.get("ok"):
            proposals.append(
                {
                    "problem": h["issue"],
                    "evidence_count": 1,
                    "suggested_skill_md_addition": (
                        "Fix herdr-plugin.toml action to call "
                        "skill-gap-finder.py analyze --min-occurrences N"
                    ),
                    "matched_skill": {"name": "session-digger", "match_confidence": "high"},
                }
            )

    report = {
        "root_label": "session-digger",
        "version": _skill_version(root),
        "git_sha": _git_sha(root),
        "commands": len(commands),
        "missing_routes": missing_routes,
        "hardcode_hits": len(hardcodes),
        "combo_missing": combo_missing,
        "install_drift": drift,
        "herdr": herdr,
        "proposals": proposals,
        "note": "Proposals only — do not auto-edit. Review then apply.",
    }

    print(json.dumps(report, ensure_ascii=False, indent=2))
    # Default exit 0 (valid report). --strict → exit 1 when proposals remain.
    if args.strict and proposals:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
