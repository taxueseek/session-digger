#!/usr/bin/env python3
"""Run a lightweight trigger-boundary smoke eval for the skill description."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None


DEFAULT_CONCEPTS: dict[str, list[str]] = {
    "skill": ["skill", "agent skill", "技能", "agent 能力", "能力包"],
    "source_material": ["workflow", "workflows", "prompt", "prompts", "transcript", "docs", "runbook", "notes", "SOP", "流程", "工作流", "提示词", "笔记", "对话记录", "材料", "脚本"],
    "authoring_action": ["create", "synthesize", "turn", "convert", "refactor", "evaluate", "package", "govern", "publish", "upgrade", "improve", "migrate", "install", "加工", "自动化", "创建", "整理", "封装", "沉淀", "优化", "升级", "迁移", "安装", "补", "发布", "打包"],
    "search": ["search", "research", "prior art", "prior-art", "discover", "现成的", "有没有", "检索", "搜索", "同类", "已有", "生态"],
    "prior_art": ["skills catalog", "skill search", "skills.sh", "popular skill", "related skills", "下载量", "安装量", "好评", "口碑", "相关 skill", "借鉴", "取长补短"],
    "eval_release": ["eval", "trigger", "output", "Skill IR", "release gate", "评估", "触发", "边界", "门禁", "治理", "发布"],
}


def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def phrase_present(text: str, phrase: str) -> bool:
    phrase = normalize(phrase)
    if not phrase:
        return False
    if re.search(r"[\u4e00-\u9fff]", phrase):
        return phrase in text
    return f" {phrase} " in f" {text} "


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def parse_description(skill_md: Path) -> str:
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return text
    lines = text.splitlines()
    try:
        end = lines[1:].index("---") + 1
    except ValueError:
        return text
    frontmatter_text = "\n".join(lines[1:end])
    if yaml is not None:
        payload = yaml.safe_load(frontmatter_text) or {}
        if isinstance(payload, dict):
            return str(payload.get("description", ""))
    match = re.search(r"description:\s*\|?\s*(.*)", frontmatter_text)
    return match.group(1).strip() if match else text


def concept_hits(text: str, concepts: dict[str, list[str]]) -> list[str]:
    normalized = normalize(text)
    hits = []
    for name, phrases in concepts.items():
        if any(phrase_present(normalized, phrase) for phrase in phrases):
            hits.append(name)
    return hits


def negative_hit(text: str, patterns: list[str]) -> str | None:
    normalized = normalize(text)
    for pattern in patterns:
        if phrase_present(normalized, pattern):
            return pattern
    return None


def case_items(cases: dict[str, Any], bucket: str) -> list[dict[str, Any]]:
    output = []
    for raw in cases.get(bucket, []):
        if isinstance(raw, str):
            output.append({"text": raw, "family": "default"})
        elif isinstance(raw, dict):
            item = dict(raw)
            item.setdefault("family", "default")
            output.append(item)
    return output


def evaluate(root: Path, cases_path: Path) -> dict[str, Any]:
    cases = load_json(cases_path)
    concepts = cases.get("positive_concepts") or DEFAULT_CONCEPTS
    threshold = float(cases.get("recommended_threshold", 0.34))
    negative_patterns = list(cases.get("negative_patterns", []))
    description = parse_description(root / "SKILL.md")
    description_hits = set(concept_hits(description, concepts))
    required_description_concepts = set(cases.get("description_required_concepts", ["skill", "source_material", "authoring_action", "search"]))
    missing_description = sorted(required_description_concepts - description_hits)

    buckets: dict[str, list[dict[str, Any]]] = {"should_trigger": [], "should_not_trigger": [], "near_neighbor": []}
    failures: list[dict[str, Any]] = []
    totals = {"total": 0, "passed": 0, "false_positive": 0, "false_negative": 0}

    denominator = max(3, min(5, len(description_hits) or len(concepts)))
    for bucket in buckets:
        expected = bucket == "should_trigger"
        for item in case_items(cases, bucket):
            prompt = str(item.get("text", ""))
            hits = set(concept_hits(prompt, concepts))
            matched = sorted(hits & description_hits)
            neg = negative_hit(prompt, negative_patterns)
            score = min(1.0, len(matched) / denominator)
            predicted = score >= threshold and neg is None
            passed = predicted == expected
            record = {
                "prompt": prompt,
                "family": item.get("family", "default"),
                "expected_trigger": expected,
                "predicted_trigger": predicted,
                "passed": passed,
                "score": round(score, 3),
                "matched_concepts": matched,
                "negative_pattern": neg,
            }
            buckets[bucket].append(record)
            totals["total"] += 1
            if passed:
                totals["passed"] += 1
            else:
                kind = "false_negative" if expected else "false_positive"
                totals[kind] += 1
                failures.append({"bucket": bucket, "kind": kind, **record})

    ok = not missing_description and not failures
    return {
        "ok": ok,
        "threshold": threshold,
        "description_concepts": sorted(description_hits),
        "missing_description_concepts": missing_description,
        "summary": {
            **totals,
            "pass_rate": round(totals["passed"] / totals["total"], 3) if totals["total"] else 0,
        },
        "failures": failures,
        "results": buckets,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate an agent skill trigger description against smoke cases.")
    parser.add_argument("skill_dir", nargs="?", default=".", help="Skill directory.")
    parser.add_argument("--cases", default="evals/trigger_cases.json", help="Trigger case JSON path.")
    parser.add_argument("--output", "-o", help="Write JSON report to this path.")
    args = parser.parse_args()

    root = Path(args.skill_dir).resolve()
    cases_path = Path(args.cases)
    if not cases_path.is_absolute():
        cases_path = root / cases_path
    result = evaluate(root, cases_path)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        if not output.is_absolute():
            output = root / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    if not result["ok"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
