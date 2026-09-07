#!/usr/bin/env python3
"""Export a compact Skill IR document from a skill package."""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None


SCHEMA_VERSION = "2.0.0-lite"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(read_text(path))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists() or yaml is None:
        return {}
    payload = yaml.safe_load(read_text(path)) or {}
    return payload if isinstance(payload, dict) else {}


def parse_frontmatter_and_body(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    lines = text.splitlines()
    try:
        end = lines[1:].index("---") + 1
    except ValueError:
        return {}, text
    frontmatter_text = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :]).lstrip()
    if yaml is not None:
        payload = yaml.safe_load(frontmatter_text) or {}
        return payload if isinstance(payload, dict) else {}, body
    data: dict[str, Any] = {}
    for line in frontmatter_text.splitlines():
        if ":" not in line or line.startswith(" "):
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip("'\"|")
    return data, body


def parse_sections(body: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {"_preamble": []}
    current = "_preamble"
    for line in body.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
            continue
        sections.setdefault(current, []).append(line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def list_items(text: str, limit: int = 20) -> list[str]:
    items: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s*(?:[-*]|\d+\.)\s+(.*)$", line)
        if match:
            value = match.group(1).strip()
            if value:
                items.append(value)
        if len(items) >= limit:
            break
    return items


def files(root: Path, folder: str, suffixes: tuple[str, ...] | None = None) -> list[str]:
    target = root / folder
    if not target.exists():
        return []
    output: list[str] = []
    for path in sorted(target.rglob("*")):
        if not path.is_file():
            continue
        if suffixes and path.suffix not in suffixes:
            continue
        output.append(str(path.relative_to(root)))
    return output


def trigger_samples(root: Path, bucket: str) -> list[str]:
    cases = load_json(root / "evals" / "trigger_cases.json")
    output: list[str] = []
    for raw in cases.get(bucket, []):
        if isinstance(raw, str):
            output.append(raw)
        elif isinstance(raw, dict) and raw.get("text"):
            output.append(str(raw["text"]))
    return output


def build_ir(root: Path) -> dict[str, Any]:
    root = root.resolve()
    frontmatter, body = parse_frontmatter_and_body(read_text(root / "SKILL.md"))
    sections = parse_sections(body)
    interface = load_yaml(root / "agents" / "interface.yaml")
    manifest = load_json(root / "manifest.json")

    compatibility = interface.get("compatibility", {}) if isinstance(interface, dict) else {}
    gates = interface.get("gates", {}) if isinstance(interface, dict) else {}
    intent = manifest.get("intent", {}) if isinstance(manifest.get("intent"), dict) else {}

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": date.today().isoformat(),
        "package": {
            "name": frontmatter.get("name") or manifest.get("name"),
            "version": manifest.get("version"),
            "owner": manifest.get("owner"),
            "maturity_tier": manifest.get("maturity_tier"),
            "lifecycle_stage": manifest.get("lifecycle_stage"),
            "upstream": manifest.get("upstream"),
            "upstream_sync": manifest.get("upstream_sync", {}),
        },
        "intent": {
            "description": frontmatter.get("description", ""),
            "job_to_be_done": intent.get("job_to_be_done") or frontmatter.get("description", ""),
            "target_users": intent.get("target_users", ["skill operator"]),
            "inputs": intent.get("inputs", []),
            "outputs": intent.get("outputs", []),
            "exclusions": intent.get("exclusions", []),
            "defaults": manifest.get("defaults", {}),
        },
        "triggers": {
            "should_trigger": trigger_samples(root, "should_trigger"),
            "should_not_trigger": trigger_samples(root, "should_not_trigger"),
            "near_neighbor": trigger_samples(root, "near_neighbor"),
        },
        "workflow": {
            "router_rules": list_items(sections.get("Router Rules", "")),
            "compact_workflow": list_items(sections.get("Compact Workflow", "")),
            "gate_ladder": list_items(sections.get("Gate Ladder", "")),
            "output_contract": list_items(sections.get("Output Contract", "")),
        },
        "resources": {
            "references": files(root, "references", (".md", ".json", ".yaml", ".yml")),
            "scripts": files(root, "scripts", (".py",)),
            "evals": files(root, "evals"),
            "reports": files(root, "reports"),
        },
        "portability": {
            "canonical_format": compatibility.get("canonical_format"),
            "adapter_targets": compatibility.get("adapter_targets", []),
            "activation": compatibility.get("activation", {}),
            "execution": compatibility.get("execution", {}),
            "trust": compatibility.get("trust", {}),
            "permissions": compatibility.get("permissions", {}),
            "degradation": compatibility.get("degradation", {}),
        },
        "gates": gates,
        "evidence_boundary": {
            "generated_reports_are_evidence": True,
            "planned_work_is_evidence": False,
            "missing_external_or_human_evidence_label": "missing evidence",
            "public_claim_policy": "claim only what local validation, install proof, human review, or provider-backed evidence actually supports",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Export compact Skill IR for an agent skill.")
    parser.add_argument("skill_dir", nargs="?", default=".", help="Skill directory.")
    parser.add_argument("--output", "-o", help="Write JSON to this path.")
    args = parser.parse_args()

    root = Path(args.skill_dir)
    payload = build_ir(root)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        if not output.is_absolute():
            output = root / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
