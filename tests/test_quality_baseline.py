#!/usr/bin/env python3
"""Quality baseline gates — PR#1 measurement, made executable.

Implements the quantitative gates from
docs/engineering/quality-performance-baseline.md on the fixed corpus
(scripts/quality_corpus.py) via the footprint harness (scripts/footprint.py).

Baseline envelope (re-recorded 2026-09-16 after P1 round, v0.9.22+):
  * recall_mean = 1.0 — P1-A (stream_contains) closed the measured 50KB
    head-window hole (was 0.9 with EVID-DEEP-BETA missing; cost ≈ +0.5ms).
  * duplicate_rate = 0.667 RESIDUAL BY DESIGN — keep-longest folding was
    ablation-rejected (drops the smaller evidence-bearing original); only
    byte-identical dups fold (collapse_near_dups). See test_retrieval_p1.
  * CJK recall = 1.0 — Chinese evidence is retrievable on this path.

Gates here reject regressions; improvements require re-recording the
envelope in this file with a note (P1 rule: single-variable + ablation).
"""
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from footprint import run_footprint  # noqa: E402
from quality_corpus import build_corpus  # noqa: E402

# Baseline envelope — re-record deliberately, never silently.
ENVELOPE = {
    "recall_mean_min": 0.95,
    "allowed_miss_queries": (),
    "recall_mean_frozen": 1.0,
    "e2e_ms_max": 10_000,
    "peak_memory_mb_max": 300,
    "deterministic_required": True,
}


def _report():
    root = Path(tempfile.mkdtemp(prefix="sd-quality-"))
    manifest = build_corpus(root)
    return manifest, run_footprint(root, manifest)


def test_corpus_manifest_self_consistent():
    manifest = build_corpus(Path(tempfile.mkdtemp(prefix="sd-manifest-")))
    assert len(manifest["scenarios"]) == 7
    assert len(manifest["files"]) == 14
    assert len(manifest["queries"]) == 10
    assert len(manifest["markers"]) == 8
    for q in manifest["queries"]:
        for expect in q["expect_files"]:
            assert expect in manifest["files"], f"{q['query']} expects unknown file {expect}"


def test_parse_stage_zero_errors():
    _, report = _report()
    assert report["parse_errors"] == [], f"parse failures: {report['parse_errors']}"
    assert report["parsed_messages"] >= 14  # every file yields >=1 message
    assert report["corpus_files"] == 14


def test_recall_baseline_envelope():
    _, report = _report()
    assert report["recall_mean"] >= ENVELOPE["recall_mean_min"]
    assert report["recall_mean"] == ENVELOPE["recall_mean_frozen"], (
        "baseline envelope shifted — re-record deliberately in ENVELOPE "
        "with a note (P1 rule: single-variable change + ablation)"
    )
    misses = [q for q, rec in report["recall_per_query"].items() if rec < 1.0]
    assert set(misses) <= set(ENVELOPE["allowed_miss_queries"]), f"unexpected misses: {misses}"


def test_cjk_evidence_retrievable():
    _, report = _report()
    cjk = [rec for q, rec in report["recall_per_query"].items() if "ZETA" in q and "ORIG" not in q]
    assert cjk and cjk[0] == 1.0, "中文证据在兜底检索路径上必须可达"


def test_near_dup_blast_radius_documented():
    _, report = _report()
    # copies are retrieved together with the original today — freeze as documented
    dup_q = report["recall_per_query"]["dedup key = normalized text hash"]
    assert dup_q == 1.0
    assert report["duplicate_rate"], "duplicate_rate must be recorded"
    assert all(0 < d <= 1.0 for d in report["duplicate_rate"])


def test_resource_ceilings_and_determinism():
    _, report = _report()
    assert report["e2e_ms"] < ENVELOPE["e2e_ms_max"]
    assert report["peak_memory_mb"] < ENVELOPE["peak_memory_mb_max"]
    assert report["deterministic"] is ENVELOPE["deterministic_required"]


def test_report_schema_covers_pr2_metrics():
    _, report = _report()
    required = {
        "ingestion_ms", "parsing_ms", "retrieval_ms",          # stage timings
        "candidate_count_total", "duplicate_rate",              # candidates
        "recall_mean", "evidence_coverage",                     # quality
        "context_size_chars", "peak_memory_mb", "e2e_ms",       # footprint
    }
    missing = required - set(report)
    assert not missing, f"PR#2 metrics missing from report: {missing}"


def test_footprint_cli_json_output():
    import subprocess
    out = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "footprint.py"), "--json"],
        capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr[-300:]
    report = json.loads(out.stdout)
    assert report["schema"] == "sd-footprint-v1"
