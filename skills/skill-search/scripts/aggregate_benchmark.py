#!/usr/bin/env python3
"""Aggregate per-case grading JSON files into a benchmark summary.

Usage: python3 aggregate_benchmark.py <eval-iter-dir>

Produces:
  <eval-iter-dir>/benchmark.json  — machine-readable benchmark
  <eval-iter-dir>/benchmark.md    — human-readable table

Reads grading.json files from either of two layouts:
  - flat:    <eval-iter-dir>/eval-*/grading.json
  - paired:  <eval-iter-dir>/eval-*/with_skill/grading.json  (+ without_skill/)

A grading.json may carry a simple schema (expectations[].passed: bool) or the
rich schema from agents/grader.md (expectations[].with_skill / without_skill).
Both are handled.

The output benchmark.json uses a stable schema: run_summary
with mean/stddev/min/max per configuration, a delta block, and a notes list.
"""
def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    p = Path(path)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def read_text(path, *, errors="strict"):
    return Path(path).read_text(encoding="utf-8", errors=errors)


import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path


def _stats(values):
    """mean/stddev/min/max for a list of numbers; empty -> zeros."""
    if not values:
        return {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0}
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1) if n > 1 else 0.0
    return {
        "mean": round(mean, 4),
        "stddev": round(math.sqrt(var), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def _signed_delta(a, b):
    """'+0.50' / '-0.10' formatted string for the delta block."""
    return f"{a - b:+.2f}"


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        return read_json(path)
    except Exception as e:
        print(f"WARN: could not parse {path}: {e}", file=sys.stderr)
        return None


def _read_meta(case_dir: Path):
    p = case_dir / "eval_metadata.json"
    if p.exists():
        try:
            return read_json(p)
        except Exception:
            pass
    return {}


def _sides_from_grading(grading):
    """Split a grading.json into (with_passed, with_total, wo_passed, wo_total).

    Handles both the rich per-expectation schema (with_skill/without_skill keys
    inside each expectation) and the simple schema (passed: bool, plus a
    summary block). Missing sides come back as 0/0.
    """
    if not grading:
        return 0, 0, 0, 0
    exps = grading.get("expectations", []) or []
    rich = any("with_skill" in e or "without_skill" in e for e in exps)
    if rich:
        wp = sum(1 for e in exps if str(e.get("with_skill", "")).lower() == "pass")
        wt = sum(1 for e in exps if "with_skill" in e)
        wop = sum(1 for e in exps if str(e.get("without_skill", "")).lower() == "pass")
        wot = sum(1 for e in exps if "without_skill" in e)
        return wp, wt, wop, wot
    passed = sum(1 for e in exps if e.get("passed"))
    total = len(exps)
    summ = grading.get("summary", {}) or {}
    if summ:
        passed = summ.get("passed", passed)
        total = summ.get("total", total)
    return passed, total, 0, 0


def _timing(grading):
    """(duration_seconds, total_tokens) from a grading's timing block, else 0."""
    if not grading:
        return 0.0, 0
    t = grading.get("timing", {}) or {}
    dur = t.get("total_duration_seconds") or t.get("executor_duration_seconds") or 0.0
    tok = t.get("total_tokens", 0) or 0
    return dur, tok


def load_grades(root: Path):
    """Collect grading.json from flat and paired layouts into case summaries."""
    cases = []
    for case_dir in sorted(root.glob("eval-*")):
        if not case_dir.is_dir():
            continue
        meta = _read_meta(case_dir)
        g_with = _read_json(case_dir / "with_skill" / "grading.json")
        g_without = _read_json(case_dir / "without_skill" / "grading.json")
        flat = _read_json(case_dir / "grading.json") if g_with is None and g_without is None else None

        if flat is not None:
            wp, wt, wop, wot = _sides_from_grading(flat)
        else:
            wp, wt, _wop, _wot = _sides_from_grading(g_with)
            _wp2, _wt2, wop, wot = _sides_from_grading(g_without)

        with_rate = (wp / wt) if wt else 0.0
        without_rate = (wop / wot) if wot else 0.0

        discriminating, nondiscriminating = 0, 0
        source = flat or g_with
        for e in (source or {}).get("expectations", []):
            ws = str(e.get("with_skill", "")).lower()
            wo = str(e.get("without_skill", "")).lower()
            if ws == "pass" and wo == "fail":
                discriminating += 1
            elif ws == "pass" and wo == "pass":
                nondiscriminating += 1
        broken = 1 if (wp == 0 and wop == 0) else 0

        tw, tkw = _timing(g_with or flat)
        two, tkwo = _timing(g_without)

        cases.append({
            "id": meta.get("eval_id", case_dir.name),
            "name": meta.get("eval_name", case_dir.name),
            "with_rate": with_rate,
            "without_rate": without_rate,
            "with_pass": wp, "with_total": wt,
            "without_pass": wop, "without_total": wot,
            "discriminating": discriminating,
            "nondiscriminating": nondiscriminating,
            "broken": broken,
            "timing_with": tw, "tokens_with": tkw,
            "timing_without": two, "tokens_without": tkwo,
            "eval_feedback": (source or {}).get("eval_feedback", {}).get("overall"),
            "claims_unverified": sum(
                1 for c in (source or {}).get("claims", []) if not c.get("verified")
            ),
        })
    return cases


def _build_notes(cases):
    """Freeform observations the aggregate stats hide."""
    notes = []
    total = len(cases)
    disc = sum(c["discriminating"] for c in cases)
    nondisc = sum(c["nondiscriminating"] for c in cases)
    broken = sum(c["broken"] for c in cases)
    if total:
        if nondisc / total > 0.5:
            notes.append(
                f"{nondisc}/{total} cases are non-discriminating (both pass) — "
                f"the skill may violate Gate 1 on these."
            )
        if broken:
            notes.append(
                f"{broken}/{total} cases broken (both fail) — functional defect "
                f"or bad test prompts."
            )
        if disc:
            notes.append(
                f"{disc}/{total} cases have at least one discriminating expectation "
                f"(with-skill passes, baseline fails)."
            )
    for c in cases:
        if c["eval_feedback"]:
            notes.append(f"[{c['name']}] grader eval critique: {c['eval_feedback']}")
        if c["claims_unverified"]:
            notes.append(
                f"[{c['name']}] {c['claims_unverified']} unverified claim(s) in the output."
            )
    return notes


def build_benchmark(root: Path, cases):
    """Build the viewer-compatible benchmark.json structure."""
    ws_stats = _stats([c["with_rate"] for c in cases])
    wo_stats = _stats([c["without_rate"] for c in cases])
    wt_stats = _stats([c["timing_with"] for c in cases])
    wot_stats = _stats([c["timing_without"] for c in cases])
    wk_stats = _stats([c["tokens_with"] for c in cases])
    wok_stats = _stats([c["tokens_without"] for c in cases])

    run_summary = {
        "with_skill": {
            "pass_rate": ws_stats,
            "time_seconds": wt_stats,
            "tokens": wk_stats,
        },
        "without_skill": {
            "pass_rate": wo_stats,
            "time_seconds": wot_stats,
            "tokens": wok_stats,
        },
        "delta": {
            "pass_rate": _signed_delta(ws_stats["mean"], wo_stats["mean"]),
            "time_seconds": _signed_delta(wt_stats["mean"], wot_stats["mean"]),
            "tokens": _signed_delta(wk_stats["mean"], wok_stats["mean"]),
        },
    }

    runs = []
    for c in cases:
        runs.append({
            "eval_id": c["id"],
            "eval_name": c["name"],
            "configuration": "with_skill",
            "run_number": 1,
            "result": {
                "pass_rate": round(c["with_rate"], 4),
                "passed": c["with_pass"],
                "failed": max(0, c["with_total"] - c["with_pass"]),
                "total": c["with_total"],
                "time_seconds": c["timing_with"],
                "tokens": c["tokens_with"],
            },
        })
        runs.append({
            "eval_id": c["id"],
            "eval_name": c["name"],
            "configuration": "without_skill",
            "run_number": 1,
            "result": {
                "pass_rate": round(c["without_rate"], 4),
                "passed": c["without_pass"],
                "failed": max(0, c["without_total"] - c["without_pass"]),
                "total": c["without_total"],
                "time_seconds": c["timing_without"],
                "tokens": c["tokens_without"],
            },
        })

    notes = _build_notes(cases)

    return {
        "metadata": {
            "skill_name": root.parent.name if root.name.startswith("iter-") else root.name,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "evals_run": [c["name"] for c in cases],
            "runs_per_configuration": 1,
        },
        "runs": runs,
        "run_summary": run_summary,
        "notes": notes,
    }


def verdict_text(delta_mean):
    if delta_mean >= 0.4:
        return "STRONG — skill clearly earns its context cost."
    if delta_mean >= 0.15:
        return "MARGINAL — skill helps on some cases; trim non-discriminating scope."
    return "WEAK — skill may violate Gate 1 (model already does this)."


def build_markdown(root: Path, benchmark):
    """Human-readable benchmark.md mirroring the JSON."""
    rs = benchmark["run_summary"]
    ws = rs["with_skill"]["pass_rate"]["mean"]
    wo = rs["without_skill"]["pass_rate"]["mean"]
    out = [f"# Benchmark — {root.name}\n"]
    out.append("| Metric | with_skill | without_skill | Delta |")
    out.append("|--------|------------|---------------|-------|")
    out.append(f"| Pass rate | {ws:.0%} | {wo:.0%} | {rs['delta']['pass_rate']} |")
    out.append(f"| Mean time (s) | {rs['with_skill']['time_seconds']['mean']:.1f} | "
               f"{rs['without_skill']['time_seconds']['mean']:.1f} | {rs['delta']['time_seconds']} |")
    out.append(f"| Mean tokens | {rs['with_skill']['tokens']['mean']:.0f} | "
               f"{rs['without_skill']['tokens']['mean']:.0f} | {rs['delta']['tokens']} |")
    out.append("\n## Classification\n")
    disc = sum(1 for n in benchmark["notes"] if "discriminating expectation" in n)
    nondisc = sum(1 for n in benchmark["notes"] if "non-discriminating" in n)
    broken = sum(1 for n in benchmark["notes"] if "cases broken" in n)
    out.append(f"- Discriminating: **{disc}**")
    out.append(f"- Non-discriminating: **{nondisc}**")
    out.append(f"- Broken: **{broken}**\n")
    out.append(f"**Verdict:** {verdict_text(ws - wo)}\n")
    if benchmark["notes"]:
        out.append("## Notes\n")
        for n in benchmark["notes"]:
            out.append(f"- {n}")
    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Aggregate per-case grading JSON files into a benchmark summary.")
    parser.add_argument("eval_iter_dir", help="Directory containing eval-*/grading.json layouts.")
    args = parser.parse_args()
    root = Path(args.eval_iter_dir).resolve()
    if not root.is_dir():
        print(f"ERROR: not a directory: {root}", file=sys.stderr)
        return 2
    cases = load_grades(root)
    if not cases:
        print(f"ERROR: no eval-*/grading.json under {root}", file=sys.stderr)
        return 1

    benchmark = build_benchmark(root, cases)
    md = build_markdown(root, benchmark)

    json_path = root / "benchmark.json"
    md_path = root / "benchmark.md"
    json_path.write_text(json.dumps(benchmark, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(md, encoding="utf-8")

    print(md)
    print(f"\n(benchmark.json written to {json_path})")
    print(f"(benchmark.md written to {md_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
