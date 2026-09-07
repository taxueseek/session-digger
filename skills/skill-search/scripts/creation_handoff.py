#!/usr/bin/env python3
"""Generate or refresh reports/creation-handoff.md for a skill package.

Scaffold only — fills identity/version from SKILL.md + optional manifest.
Does not invent prior-art claims; leaves study/absorb/advantage sections for the author.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

def read_text(path, *, errors="strict"):
    return Path(path).read_text(encoding="utf-8", errors=errors)


def load_json_optional(path):
    """Soft JSON load: missing/invalid/non-object → empty dict (never raises)."""
    if not Path(path).is_file():
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def parse_frontmatter(text):
    """Parse YAML frontmatter from a SKILL.md-style document.

    Multiline-aware key: value fallback (no PyYAML dependency). Block scalars
    (| and >) are collapsed to single-line values.
    """
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    try:
        end = lines[1:].index("---") + 1
    except ValueError:
        return {}
    frontmatter_text = "\n".join(lines[1:end])
    data = {}
    current_key = ""
    for raw in frontmatter_text.splitlines():
        if not raw.strip():
            continue
        if raw.startswith((" ", "\t")) and current_key:
            data[current_key] = f"{data.get(current_key, '')}\n{raw.strip()}".strip()
            continue
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        current_key = key.strip()
        val = value.strip()
        if val in ("|", ">"):
            data[current_key] = ""
            continue
        data[current_key] = val.strip("'\"")
    return data



_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))



def identity(root: Path) -> dict[str, str]:
    skill = root / "SKILL.md"
    if not skill.is_file():
        raise FileNotFoundError(f"SKILL.md missing under {root}")
    fm = parse_frontmatter(read_text(skill))
    manifest = load_json_optional(root / "manifest.json")
    name = str(fm.get("name") or root.name).strip()
    description = " ".join(str(fm.get("description", "")).split())
    version = str(manifest.get("version") or "0.0.0").strip()
    home = str(Path.home())
    resolved = str(root.resolve())
    if resolved.startswith(home):
        resolved = "$HOME" + resolved[len(home):]
    return {
        "name": name,
        "description": description,
        "version": version,
        "path": resolved,
        "publication_status": str(manifest.get("status") or "local-draft"),
    }


def render_handoff(meta: dict[str, str], *, seek_summary: str = "") -> str:
    today = date.today().isoformat()
    return f"""# Creation Handoff — {meta['name']} v{meta['version']}

Generated: {today}
Local path: `{meta['path']}`
Publication status: {meta['publication_status']}

## 1. Result

- **Skill**: `{meta['name']}` `v{meta['version']}`
- **Job**: {meta['description'] or '_(fill job-to-be-done)_'}
- **Path**: `{meta['path']}`
- **Status**: {meta['publication_status']}

## 2. Reference skills studied

_(Name 2–4 inspected candidates only — not SERP-only hits.)_

| Skill / source | Why shortlisted | Trust signal (dated) | Mechanism learned | Where it landed |
|---|---|---|---|---|
| | | | | |

## 3. Absorbed and rejected

- **keep**:
- **adapt**:
- **reject**:
- **invent**:

## 4. Advantages and highlights

| Label | Claim | Evidence pointer |
|---|---|---|
| design advantage | | |
| validated advantage | | |
| hypothesis | | missing_evidence |

Labels only: `design advantage` | `validated advantage` | `hypothesis`.  
Do not claim “best / world-class / better than X” without fair comparison.

## 5. Verification and limits

- package validation: _(tier + result)_
- trigger results: _(path to reports/trigger-eval.json or missing_evidence)_
- output / runtime / human evidence: _(or missing_evidence)_
- deliberately excluded permissions/actions:

## Seek handoff (if run)

```
{seek_summary or 'SEEK_ACTION= / SEEK_TARGET= / SEEK_EVIDENCE= / SEEK_CONFIDENCE= / SEEK_SUMMARY='}
```

## Compact final-response template

```markdown
已创建：{meta['name']} {meta['version']} — <one-line outcome>

参考学习
- <skill A>: 学习 <mechanism>; 落到 <artifact/section>。
- <skill B>: 学习 <mechanism>; 落到 <artifact/section>。

取舍与原创
- 保留：...
- 舍弃：...
- 原创：...

优势与证据
- [design advantage] ...
- [validated advantage] ...
- [hypothesis] ...（missing_evidence）

验证：<results>
边界：<limitations>
```
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Scaffold reports/creation-handoff.md for a skill.")
    parser.add_argument("skill_dir", nargs="?", default=".", help="Skill directory")
    parser.add_argument(
        "--output",
        "-o",
        default="reports/creation-handoff.md",
        help="Output path relative to skill_dir (default: reports/creation-handoff.md)",
    )
    parser.add_argument("--seek-summary", default="", help="Optional SEEK_* block to embed")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing handoff (default: refuse if file exists and non-empty)",
    )
    parser.add_argument("--stdout", action="store_true", help="Print markdown instead of writing")
    args = parser.parse_args()

    root = Path(args.skill_dir).expanduser().resolve()
    meta = identity(root)
    text = render_handoff(meta, seek_summary=args.seek_summary)
    if args.stdout:
        print(text)
        return

    output = Path(args.output)
    if not output.is_absolute():
        output = root / output
    if output.is_file() and output.stat().st_size > 0 and not args.force:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"{output} exists; pass --force to overwrite",
                    "path": str(output),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(2)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    print(
        json.dumps(
            {"ok": True, "path": str(output), "name": meta["name"], "version": meta["version"]},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
