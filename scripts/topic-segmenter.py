#!/usr/bin/env python3
"""
topic-segmenter.py — Cross-session topic segmentation and labeling.

Identifies topic boundaries within long sessions or across multiple sessions
of the same project. Uses time-gap heuristics + content similarity.

This is both:
  - A standalone tool for analyzing a single session's topic structure
  - The extraction backend for index-builder.py's topic_boundaries table

Usage:
  topic-segmenter.py <session.jsonl> [--min-gap SECONDS] [--max-topics N]
  topic-segmenter.py --project PATH [--since YYYY-MM-DD]

Output: JSON with segments [{start_ts, end_ts, msg_count, label, keywords}]

v1.0: Inspired by wechat-insight topic analysis + echo-sleuth /lessons architecture.
"""

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
import echolib


def simple_tokenize(text):
    """Tokenize into words. CJK chars are individual tokens."""
    # Split on whitespace and punctuation
    tokens = re.findall(r'[\u4e00-\u9fff]|[a-zA-Z0-9]+', text.lower())
    return tokens


def extract_keywords(texts, top_n=5):
    """Extract top keywords from a list of texts."""
    all_tokens = []
    for t in texts:
        all_tokens.extend(simple_tokenize(t))

    # Remove short/stop words
    stop_words = {"the", "a", "an", "is", "it", "to", "of", "and", "or", "in",
                  "on", "at", "for", "with", "this", "that", "i", "you", "we",
                  "的", "了", "是", "在", "我", "有", "和", "不", "人", "大",
                  "一", "个", "上", "中", "为", "们", "到", "说", "要", "会"}

    filtered = [t for t in all_tokens if t not in stop_words and len(t) > 1]
    if not filtered:
        return []

    counter = Counter(filtered)
    return [w for w, _ in counter.most_common(top_n)]


def segment_session(session_path, min_gap=300, max_topics=20):
    """
    Segment a session into topics.

    Heuristics:
    1. Time gap > min_gap seconds → hard boundary
    2. Low content similarity between consecutive message blocks → soft boundary
    3. Role pattern change (user→assistant→user cycle) → phase marker
    """
    try:
        messages = list(echolib.extract_messages(session_path, role="both"))
    except Exception as e:
        return {"error": str(e), "segments": []}

    if len(messages) < 3:
        return {
            "session": str(session_path),
            "total_messages": len(messages),
            "segments": [{
                "start_ts": messages[0]["timestamp"] if messages else "",
                "end_ts": messages[-1]["timestamp"] if messages else "",
                "msg_count": len(messages),
                "label": "single_topic",
                "keywords": extract_keywords([m["text"] for m in messages]),
            }]
        }

    # Phase 1: detect boundary candidates
    boundaries = [0]  # Always start at beginning

    for i in range(1, len(messages)):
        cur = messages[i]
        prev = messages[i - 1]

        # Time gap check
        ts_cur = cur.get("timestamp", "")
        ts_prev = prev.get("timestamp", "")
        if ts_cur and ts_prev:
            try:
                fmt = "%Y-%m-%dT%H:%M:%S"
                t_cur = datetime.strptime(ts_cur[:19], fmt)
                t_prev = datetime.strptime(ts_prev[:19], fmt)
                gap = (t_cur - t_prev).total_seconds()
                if gap > min_gap:
                    boundaries.append(i)
                    continue
            except (ValueError, TypeError):
                pass

        # Content similarity check (sliding window of 3 messages)
        if i >= 3:
            prev_texts = " ".join(m["text"] for m in messages[max(0,i-3):i])
            cur_texts = " ".join(m["text"] for m in messages[i:min(i+3, len(messages))])

            prev_tokens = set(simple_tokenize(prev_texts))
            cur_tokens = set(simple_tokenize(cur_texts))

            if prev_tokens and cur_tokens:
                overlap = len(prev_tokens & cur_tokens) / max(len(prev_tokens), 1)
                if overlap < 0.1:  # Less than 10% token overlap
                    boundaries.append(i)

    # Deduplicate boundaries that are too close (< 5 messages)
    deduped = [boundaries[0]]
    for b in boundaries[1:]:
        if b - deduped[-1] >= 5:
            deduped.append(b)
    boundaries = deduped[:max_topics]

    # Phase 2: build segments from boundaries
    segments = []
    for idx, start in enumerate(boundaries):
        end = boundaries[idx + 1] if idx + 1 < len(boundaries) else len(messages)
        chunk = messages[start:end]
        texts = [m["text"] for m in chunk]
        keywords = extract_keywords(texts)

        segments.append({
            "start_ts": chunk[0].get("timestamp", ""),
            "end_ts": chunk[-1].get("timestamp", ""),
            "msg_count": len(chunk),
            "label": "_".join(keywords[:3]) if keywords else f"segment_{idx+1}",
            "keywords": keywords,
        })

    return {
        "session": str(session_path),
        "total_messages": len(messages),
        "segment_count": len(segments),
        "segments": segments,
    }


def segment_project(project_dir, since=None):
    """Segment all sessions in a project directory."""
    if not project_dir.exists():
        return {"error": f"Directory not found: {project_dir}"}

    results = []
    for jf in sorted(project_dir.glob("*.jsonl"), key=os.path.getmtime, reverse=True):
        if "subagents" in str(jf):
            continue
        result = segment_session(str(jf))
        results.append(result)

    return {
        "project": str(project_dir),
        "session_count": len(results),
        "sessions": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Session topic segmenter")
    parser.add_argument("input", help="Session .jsonl file or project directory")
    parser.add_argument("--min-gap", type=int, default=300,
                        help="Min time gap (seconds) for topic boundary (default: 300)")
    parser.add_argument("--max-topics", type=int, default=20)
    parser.add_argument("--project", action="store_true",
                        help="Treat input as project directory (multiple sessions)")
    parser.add_argument("--since", default=None,
                        help="Only sessions since YYYY-MM-DD")

    args = parser.parse_args()

    input_path = Path(args.input)

    if args.project or input_path.is_dir():
        result = segment_project(input_path, since=args.since)
    else:
        result = segment_session(str(input_path), min_gap=args.min_gap, max_topics=args.max_topics)

    print(json.dumps(result, ensure_ascii=False, indent=2))
