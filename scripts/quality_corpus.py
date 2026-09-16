#!/usr/bin/env python3
"""Deterministic evaluation corpus for session analysis quality baselines.

Implements the "fixed evaluation corpus" required by
docs/engineering/quality-performance-baseline.md and
docs/engineering/p1-retrieval-footprint-measurement.md:

  short / long / repeated-topics / noisy-tool-traces / missing-metadata /
  multilingual (CJK) / near-duplicate sessions — all synthetic, all
  deterministic, zero dependence on real user data.

Evidence markers are planted deliberately; the manifest returned by
``build_corpus`` lists every planted marker so the footprint harness can
compute recall / coverage instead of guessing.
"""
from __future__ import annotations

import json
from pathlib import Path

MARK_SHORT = "EVID-SHORT-ALPHA"
MARK_LONG_HEAD = "EVID-LONG-HEAD"
MARK_LONG_DEEP = "EVID-DEEP-BETA"
MARK_REPEAT = "EVID-REPEAT-GAMMA"
MARK_NOISY = "EVID-NOISY-DELTA"
MARK_SPARSE = "EVID-SPARSE-EPSILON"
MARK_CJK = "证据-中文-ZETA"
MARK_ORIG = "EVID-ORIG-ZETA"

REPEAT_TOPIC = "context-quantization"

# Long sessions must push the deep marker well past the 50KB head window
# used by retrieval's file-scan fallback — that hole is a *measurement
# target* (PR#2), not a fixture bug.
_LONG_FILLER_LINES = 600
_LONG_FILLER_LINE = json.dumps({"type": "assistant", "timestamp": "2026-06-01T12:00:00.000Z",
                                "message": {"role": "assistant", "model": "claude-sonnet-4-5",
                                            "content": [{"type": "text", "text": "running benchmark loop iteration, payload padding " + "x" * 120}]}})


def _msg(type_, text, ts="2026-06-01T12:00:00.000Z", extra=None):
    rec = {
        "type": type_,
        "uuid": f"u-{abs(hash(text)) % 10**8:08d}",
        "sessionId": "corpus",
        "timestamp": ts,
        "message": {"role": "user" if type_ == "user" else "assistant",
                    "content": text if isinstance(text, str) else text},
        "cwd": "/tmp/corpus-project", "gitBranch": "main", "slug": "corpus",
        "version": "2.1.39", "isSidechain": False,
    }
    if type_ == "assistant":
        rec["message"]["model"] = "claude-sonnet-4-5"
        rec["message"]["content"] = [{"type": "text", "text": text}]
    if extra:
        rec.update(extra)
    return rec


def _write(path: Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _short(root: Path) -> Path:
    recs = [
        _msg("user", f"investigate flaky retry in the sync worker {MARK_SHORT}"),
        _msg("assistant", "the retry loop resets the backoff timer on every reconnect"),
        _msg("user", "please summarize the root cause"),
        _msg("assistant", "root cause: backoff timer resets; fix by carrying state across reconnects"),
    ]
    p = root / "s1-short.jsonl"
    _write(p, recs)
    return p


def _long(root: Path) -> Path:
    recs = [_msg("user", f"deep dive kickoff {MARK_LONG_HEAD}")]
    recs += [json.loads(_LONG_FILLER_LINE) for _ in range(_LONG_FILLER_LINES)]
    recs += [
        _msg("user", f" buried conclusion after long tail {MARK_LONG_DEEP}"),
        _msg("assistant", "noted the buried conclusion"),
    ]
    p = root / "s2-long.jsonl"
    _write(p, recs)
    return p


def _repeated(root: Path, n: int = 6) -> list:
    paths = []
    for i in range(n):
        marker = MARK_REPEAT if i == 0 else "no marker in this copy"
        recs = [
            _msg("user", f"discussing {REPEAT_TOPIC} thresholds, round {i}"),
            _msg("assistant", f"{REPEAT_TOPIC} round {i}: budget gate at 0.0937"),
            _msg("user", marker),
        ]
        p = root / f"s3-repeat-{i}.jsonl"
        _write(p, recs)
        paths.append(p)
    return paths


def _noisy(root: Path) -> Path:
    # 120 records ≈ 34KB: noise stays inside the 50KB head window so the
    # noisy-scenario marker tests *noise handling*, not the deep-marker hole
    # (that is s2-long's job — scenarios stay orthogonal).
    recs = []
    for i in range(120):
        recs.append({
            "type": "user", "uuid": f"n-{i}", "sessionId": "corpus",
            "timestamp": "2026-06-01T12:01:00.000Z",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"t-{i}",
                 "content": f"Exit Code: 1\nTraceback (most recent call last): noise {i}"}]},
            "cwd": "/tmp/corpus-project", "isSidechain": False,
        })
    recs.append(_msg("assistant", f"after the noise storm the real finding is here {MARK_NOISY}"))
    p = root / "s4-noisy.jsonl"
    _write(p, recs)
    return p


def _sparse(root: Path) -> Path:
    recs = [
        {"type": "user", "message": {"role": "user", "content": f"no timestamps no cwd here {MARK_SPARSE}"}},
        {"type": "assistant", "message": {"role": "assistant",
                                          "content": [{"type": "text", "text": "ack, still parseable"}]}},
    ]
    p = root / "s5-sparse.jsonl"
    _write(p, recs)
    return p


def _cjk(root: Path) -> Path:
    recs = [
        _msg("user", f"检索一下中文召回：分词器在 CJK 字符级切分 {MARK_CJK}"),
        _msg("assistant", "韓文与日文也覆盖：한국어 토큰、日本語トークン確認"),
        _msg("user", "mixing languages in one session for recall testing"),
    ]
    p = root / "s6-cjk.jsonl"
    _write(p, recs)
    return p


def _near_dups(root: Path) -> list:
    base = [
        _msg("user", "design the retrieval dedup pipeline for near-duplicate sessions"),
        _msg("assistant", "dedup key = normalized text hash over first N messages"),
    ]
    orig = list(base) + [_msg("user", f"the dedup decision marker lives only here {MARK_ORIG}")]
    paths = []
    p = root / "s7-dup-orig.jsonl"
    _write(p, orig)
    paths.append(p)
    for i in range(1, 3):  # two 95%-identical copies without the marker
        copy = list(base) + [_msg("user", f"copy variant {i} without any marker, padding padding padding")]
        p = root / f"s7-dup-copy-{i}.jsonl"
        _write(p, copy)
        paths.append(p)
    return paths


def build_corpus(root: Path) -> dict:
    """Write the fixed corpus under *root* and return its manifest."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    repeat_paths = _repeated(root)
    dup_paths = _near_dups(root)
    files = {
        "s1_short": _short(root),
        "s2_long": _long(root),
        "s3_repeat": repeat_paths,
        "s4_noisy": _noisy(root),
        "s5_sparse": _sparse(root),
        "s6_cjk": _cjk(root),
        "s7_dup": dup_paths,
    }
    flat = {}
    for k, v in files.items():
        for p in (v if isinstance(v, list) else [v]):
            flat[p.name] = str(p)

    # queries: retrieval probe -> which files MUST be found (planted evidence)
    queries = [
        {"query": MARK_SHORT, "expect_files": ["s1-short.jsonl"]},
        {"query": MARK_LONG_HEAD, "expect_files": ["s2-long.jsonl"]},
        {"query": MARK_LONG_DEEP, "expect_files": ["s2-long.jsonl"], "note": "deep-beyond-head-window"},
        {"query": MARK_REPEAT, "expect_files": ["s3-repeat-0.jsonl"]},
        {"query": MARK_NOISY, "expect_files": ["s4-noisy.jsonl"]},
        {"query": MARK_SPARSE, "expect_files": ["s5-sparse.jsonl"]},
        {"query": MARK_CJK, "expect_files": ["s6-cjk.jsonl"]},
        {"query": MARK_ORIG, "expect_files": ["s7-dup-orig.jsonl"]},
        {"query": REPEAT_TOPIC,
         "expect_files": [f"s3-repeat-{i}.jsonl" for i in range(6)],
         "note": "repeated-topic blast radius"},
        {"query": "dedup key = normalized text hash",
         "expect_files": ["s7-dup-orig.jsonl", "s7-dup-copy-1.jsonl", "s7-dup-copy-2.jsonl"],
         "note": "near-dup blast radius (copies share base text)"},
    ]

    manifest = {
        "version": 1,
        "files": flat,
        "scenarios": sorted(files.keys()),
        "queries": queries,
        "near_dup_groups": [{
            "original": "s7-dup-orig.jsonl",
            "copies": ["s7-dup-copy-1.jsonl", "s7-dup-copy-2.jsonl"],
            "marker_only_in": "original",
        }],
        "markers": [MARK_SHORT, MARK_LONG_HEAD, MARK_LONG_DEEP, MARK_REPEAT,
                    MARK_NOISY, MARK_SPARSE, MARK_CJK, MARK_ORIG],
    }
    return manifest


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="build the fixed evaluation corpus")
    ap.add_argument("outdir")
    args = ap.parse_args()
    m = build_corpus(Path(args.outdir))
    print(json.dumps({"files": len(m["files"]), "queries": len(m["queries"])}, ensure_ascii=False))
