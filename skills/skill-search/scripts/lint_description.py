#!/usr/bin/env python3
"""Lint a skill's description field for common failures.

Usage: python3 lint_description.py <skill-folder>

Detects:
  - Description Trap: description recaps HOW (workflow steps) instead of WHEN
  - Missing trigger phrases (needs 5+)
  - No exclusion clause ("NOT for ...")
  - Too short / too long
  - Placeholder leakage

Prints findings and a score. Exits 0 if no hard errors.
"""
import re
# CLI uses sys.argv (argparse-equivalent interface)
import sys
from pathlib import Path

TRAP_PATTERNS = [
    (r"(write|run|start).{0,20}(test|step).{0,20}(first|then|before)", "sequential how-steps"),
    (r"step\s?\d", "numbered steps in description"),
    (r"\bfirst\b.{0,40}\bthen\b", "first...then workflow recap"),
    (r"\balways (use|do)\b", "imperative rule (belongs in body)"),
    (r"\bnever (use|do)\b", "imperative rule (belongs in body)"),
    (r"->|→|=>", "arrow-flow notation (workflow recap)"),
]
TRIGGER_HINTS = [
    "use when", "triggers", "use for", "fires on", "when the user",
    "when ", "适用于", "触发词", "当用户",
]


def get_description(text: str) -> str:
    if not text.startswith("---"):
        return ""
    parts = text.split("---", 2)
    if len(parts) < 3:
        return ""
    raw = parts[1]
    m = re.search(r"^description\s*:\s*(.+(?:\n(?![\w-]+:).*)*)", raw, re.M)
    if not m:
        return ""
    val = m.group(1)
    val = val.replace(">", " ").replace("|", " ")
    return re.sub(r"\s+", " ", val).strip()


def main():
    if len(sys.argv) != 2:
        print("Usage: lint_description.py <skill-folder>", file=sys.stderr)
        return 2
    skill_md = Path(sys.argv[1]) / "SKILL.md"
    if not skill_md.exists():
        print(f"ERROR: {skill_md} not found", file=sys.stderr)
        return 2
    desc = get_description(skill_md.read_text(encoding="utf-8"))
    if not desc:
        print("ERROR: no description field found", file=sys.stderr)
        return 2

    findings = []  # (severity, message)
    score = 100

    # length
    if len(desc) < 40:
        findings.append(("ERROR", f"too short ({len(desc)} chars) — needs triggers"))
        score -= 40
    if len(desc) > 600:
        findings.append(("WARN", f"long ({len(desc)} chars) — risks token bloat"))

    # Description Trap
    for pat, label in TRAP_PATTERNS:
        if re.search(pat, desc, re.I):
            findings.append(("ERROR", f"Description Trap: {label} — move HOW to body, keep WHEN only"))
            score -= 35
            break

    # trigger phrase count (rough: count comma-separated cues near "use when")
    triggers = re.findall(r"[,;]\s*([A-Za-z一-龥][\w 一-龥]{2,})", desc)
    trigger_count = len(triggers)
    has_trigger_word = any(h in desc.lower() for h in TRIGGER_HINTS)
    if not has_trigger_word:
        findings.append(("ERROR", "no trigger phrase (add 'Use when ...')"))
        score -= 30
    elif trigger_count < 3:
        findings.append(("WARN", f"only ~{trigger_count} trigger cues — aim for 5+"))

    # exclusion
    if not re.search(r"\bnot for\b|do not use|don't use|不用于|不要用于", desc, re.I):
        findings.append(("WARN", "no exclusion clause ('NOT for ...') — risks false triggers"))
        score -= 10

    # placeholder
    if re.search(r"\[[A-Za-z一-龥][^\]]*\]|TODO|TBD|待定", desc, re.I):
        findings.append(("ERROR", "placeholder/TODO in description"))
        score -= 25

    print(f"# description lint — {Path(sys.argv[1]).name}")
    print(f"description: {desc[:200]}{'…' if len(desc) > 200 else ''}")
    print(f"trigger cues: ~{trigger_count}")
    print(f"score: {max(0, score)}/100")
    print()
    if findings:
        for sev, msg in findings:
            mark = "✗" if sev == "ERROR" else "⚠"
            print(f"  {mark} [{sev}] {msg}")
    else:
        print("  ✓ clean")
    return 1 if any(s == "ERROR" for s, _ in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
