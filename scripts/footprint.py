#!/usr/bin/env python3
"""Session retrieval footprint harness — PR#2 measurement, made executable.

Quantifies, on the fixed corpus (scripts/quality_corpus.py):

  ingestion / parsing / retrieval stage timings, candidate counts,
  duplicate rate, evidence coverage & recall, final context size,
  peak memory, end-to-end latency.

This is a measurement tool, not an optimizer: per the P1 gate in
docs/engineering/p1-retrieval-footprint-measurement.md, no behavior is
changed here. The JSON report is the baseline envelope future optimization
PRs must compare against.

Retrieval stage measures the real production *fallback* path (registry
file-scan with 50KB head window, see sd-recall.find_sessions) — the path
taken whenever the FTS index is absent. Index-backed FTS is measured
separately by tests/test_perf.py against live data.
"""
from __future__ import annotations

import json
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "tests"))

from quality_corpus import build_corpus  # noqa: E402

HEAD_WINDOW = 50_000  # _dup_rate signature window (retrieval itself now streams)


def _stage_ingestion(files: dict) -> dict:
    t0 = time.perf_counter()
    total_bytes = 0
    for p in files.values():
        st = Path(p).stat()
        total_bytes += st.st_size
    return {"ingestion_ms": round((time.perf_counter() - t0) * 1000, 2),
            "corpus_files": len(files), "corpus_bytes": total_bytes}


def _stage_parsing(files: dict) -> dict:
    from echolib import _claude

    t0 = time.perf_counter()
    msg_total = 0
    parse_errors = []
    for name, p in sorted(files.items()):
        try:
            stats = _claude.session_stats(p)
            msgs = list(_claude.extract_messages(p, limit=0))
            msg_total += stats.get("user_messages", 0) + stats.get("assistant_messages", 0)
            assert len(msgs) >= 1, f"{name}: extract_messages returned 0"
        except Exception as exc:  # parse failure is itself a quality signal
            parse_errors.append({"file": name, "error": str(exc)[:200]})
    return {"parsing_ms": round((time.perf_counter() - t0) * 1000, 2),
            "parsed_messages": msg_total, "parse_errors": parse_errors}


def _retrieve(paths: dict, queries: list) -> tuple:
    """Production-parity retrieval: stream_contains + near-dup collapse.

    Same helpers sd-recall.find_sessions uses for its file-scan fallback, so
    the harness measures what production does (P1-A + P1-B parity).
    """
    from retrieval_utils import collapse_near_dups, stream_contains

    t0 = time.perf_counter()
    results = []
    for q in queries:
        candidates = []
        for name, p in sorted(paths.items()):
            if stream_contains(p, q["query"]):
                candidates.append(name)
        kept, collapsed = collapse_near_dups(candidates, key_func=lambda n: paths[n])
        results.append({"query": q["query"], "candidates": kept,
                        "collapsed": collapsed})
    return results, round((time.perf_counter() - t0) * 1000, 2)


def _dup_rate(names: list, paths: dict) -> float:
    """Fraction of near-duplicate candidates (normalized-prefix collision)."""
    if len(names) <= 1:
        return 0.0
    sigs = {}
    for n in names:
        try:
            with open(paths[n], encoding="utf-8", errors="replace") as f:
                sig = " ".join(f.read(HEAD_WINDOW).lower().split())[:400]
        except OSError:
            continue
        sigs.setdefault(sig, []).append(n)
    dupes = sum(len(v) - 1 for v in sigs.values())
    return round(dupes / len(names), 3)


def run_footprint(corpus_root: Path, manifest: dict) -> dict:
    files = manifest["files"]
    report = {"schema": "sd-footprint-v1", "corpus": str(corpus_root)}
    report.update(_stage_ingestion(files))
    report.update(_stage_parsing(files))

    paths = manifest["files"]
    results, retrieval_ms = _retrieve(paths, manifest["queries"])
    results2, _ = _retrieve(paths, manifest["queries"])
    deterministic = [r["candidates"] for r in results] == [r["candidates"] for r in results2]

    recalls, dup_rates, dup_raw = [], [], []
    ctx_chars = 0
    for q, r in zip(manifest["queries"], results):
        expect = set(q["expect_files"])
        found = expect.intersection(r["candidates"])
        recalls.append(len(found) / len(expect) if expect else 1.0)
        if str(q.get("note", "")).startswith("near-dup blast radius"):
            dup_rates.append(_dup_rate(r["candidates"], paths))
            raw_total = len(r["candidates"]) + r.get("collapsed", 0)
            dup_raw.append(round(r.get("collapsed", 0) / raw_total, 3)
                           if raw_total else 0.0)
        # context expansion cost: extracted evidence for one candidate
        if r["candidates"]:
            from echolib import _claude
            c = sorted(r["candidates"])[0]
            msgs = list(_claude.extract_messages(paths[c], limit=5))
            ctx_chars += sum(len(str(m.get("text", ""))) for m in msgs)

    tracemalloc.start()
    _, retrieval_ms_profiled = _retrieve(paths, manifest["queries"])
    parsing_ms_profiled = _stage_parsing(files)["parsing_ms"]
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    report.update({
        "retrieval_ms": retrieval_ms,
        "candidate_count_total": sum(len(r["candidates"]) for r in results),
        "collapsed_total": sum(r.get("collapsed", 0) for r in results),
        "avg_candidates_per_query": round(
            sum(len(r["candidates"]) for r in results) / max(len(results), 1), 2),
        "duplicate_rate": dup_rates,
        "duplicate_rate_raw": dup_raw,
        "dup_note": "residual dups are intentional: keep-longest folding was "
                    "ablation-rejected (drops the smaller evidence-bearing "
                    "original, corpus s7); only byte-identical dups fold",
        "recall_mean": round(sum(recalls) / max(len(recalls), 1), 3),
        "recall_per_query": {
            q["query"][:40]: round(rec, 3) for q, rec in zip(manifest["queries"], recalls)},
        "evidence_coverage": round(
            sum(1 for rec in recalls if rec >= 1.0) / max(len(recalls), 1), 3),
        "context_size_chars": ctx_chars,
        "peak_memory_mb": round(peak / 1e6, 2),
        "profiled_retrieval_ms": retrieval_ms_profiled,
        "profiled_parsing_ms": parsing_ms_profiled,
        "deterministic": deterministic,
    })
    report["e2e_ms"] = report["ingestion_ms"] + report["parsing_ms"] + report["retrieval_ms"]
    return report


def main() -> int:
    import argparse
    import tempfile

    ap = argparse.ArgumentParser(description="measure session retrieval footprint (PR#2)")
    ap.add_argument("--corpus-dir", help="reuse an existing corpus dir (default: fresh tmp)")
    ap.add_argument("--json", dest="as_json", action="store_true", help="emit JSON only")
    args = ap.parse_args()

    if args.corpus_dir:
        root = Path(args.corpus_dir)
    else:
        root = Path(tempfile.mkdtemp(prefix="sd-footprint-"))
    # corpus content is deterministic — rebuilding into an existing dir is safe
    manifest = build_corpus(root)

    report = run_footprint(root, manifest)
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("session retrieval footprint (fixed corpus)")
        for k in ("corpus_files", "corpus_bytes", "ingestion_ms", "parsing_ms",
                  "retrieval_ms", "candidate_count_total", "recall_mean",
                  "evidence_coverage", "context_size_chars", "peak_memory_mb",
                  "e2e_ms", "deterministic"):
            print(f"  {k:24s} {report.get(k)}")
        if report.get("parse_errors"):
            print("  parse_errors:", report["parse_errors"])
        print(f"  corpus: {report['corpus']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
