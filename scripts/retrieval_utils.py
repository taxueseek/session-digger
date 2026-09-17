#!/usr/bin/env python3
"""Retrieval quality helpers — P1 optimizations, measured in tests/test_quality_baseline.py.

stream_contains    : file-scan retrieval must not lose evidence that sits
                     beyond the 50KB head window (quantified recall-0 hole,
                     corpus s2-long, fixed 2026-09-16).
collapse_near_dups : near-duplicate candidates must not flood the candidate
                     list (quantified dup_rate 0.667, corpus s7-dup) without
                     dropping the evidence-bearing original.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Hashable

# Head window kept identical to the historical fast path (sd-recall fallback).
HEAD_WINDOW = 50_000
# Per-file deep-scan cap after the head window misses. Bounds the worst case
# (needle absent everywhere) while covering real long sessions; the corpus
# long session is ~124KB total. Beyond the cap the miss is honest and final.
MAX_EXTRA_BYTES = 2_000_000
_CHUNK = 262_144


def stream_contains(path, needle: str, head: int = HEAD_WINDOW,
                    max_extra: int = MAX_EXTRA_BYTES) -> bool:
    """Case-insensitive containment over the whole file, bounded and early-exit.

    Fast path matches the historical behavior exactly: first `head` bytes.
    On a head miss the remainder is streamed in chunks (byte-level, with a
    carry of len(needle)-1 bytes so a match spanning a chunk seam is still
    found). ASCII case-folding only — byte-level matching keeps CJK needles
    exact; keyword.lower() on bytes is a no-op for CJK.
    """
    nb = needle.lower().encode("utf-8")
    if not nb:
        return False
    try:
        with open(path, "rb") as f:
            head_buf = f.read(head)
            if nb in head_buf.lower():
                return True
            scanned = len(head_buf)
            carry = b""
            limit = head + max_extra
            while scanned < limit:
                chunk = f.read(min(_CHUNK, limit - scanned))
                if not chunk:
                    break
                scanned += len(chunk)
                if nb in (carry + chunk).lower():
                    return True
                carry = chunk[-(len(nb) - 1):] if len(nb) > 1 else b""
        return False
    except OSError:
        return False


def _file_hash(path) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return ""


def collapse_near_dups(candidates, key_func=None):
    """Fold byte-identical duplicates to one representative each.

    candidates : sequence of items; each item's file path comes from key_func
                 (default: item[1], matching find_sessions' (sid, path, agent)).

    There is deliberately no ``keep=`` policy knob. One existed and was never
    passed by any caller — a leftover hook from the rejected keep-longest
    ablation, sitting in the signature of the function that replaced it.

    Deliberately conservative: only files with **identical full content**
    are folded (zero evidence-loss risk by construction). Content-similar
    but non-identical "pseudo-duplicates" are kept — the keep-longest
    heuristic was tested as an ablation (2026-09-16) and REJECTED: on the
    dup corpus the evidence-bearing original is *smaller* than its padding
    copies, so size-based folding dropped the original's evidence
    (PR#1 rejection rule). See tests/test_retrieval_p1.py.

    Returns (kept_in_original_order, collapsed_count).
    """
    if key_func is None:
        key_func = lambda item: item[1]  # noqa: E731
    seen: dict[str, int] = {}
    kept, collapsed = [], 0
    for item in candidates:
        h = _file_hash(key_func(item))
        if h and h in seen:
            collapsed += 1
            continue
        seen[h] = 1
        kept.append(item)
    return kept, collapsed
