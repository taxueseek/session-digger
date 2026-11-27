#!/usr/bin/env python3
"""
intent-classify.py — Classify user intent from session first message.

Usage:
    python3 intent-classify.py --session <session_id>
    python3 intent-classify.py --jsonl <path>
    python3 intent-classify.py --text "user message"

Output: JSON object:
    {intent, confidence, keywords_matched, all_scores}

Intent types: ANALYSIS | COMPARISON | OPTIMIZATION | DEVELOPMENT | DEBUGGING | REFLECTION
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import echolib  # noqa: E402
from index_builder._reader import is_virtual_path  # noqa: E402
from index_builder._schema import DB_PATH  # noqa: E402

# ---------------------------------------------------------------------------
# Intent keyword rules
# ---------------------------------------------------------------------------

# Action verbs signal what the user actually wants done (vs framing/context)
ACTION_VERBS: Dict[str, str] = {
    "优化": "OPTIMIZATION", "改进": "OPTIMIZATION", "提升": "OPTIMIZATION",
    "修复": "OPTIMIZATION", "提高": "OPTIMIZATION", "增强": "OPTIMIZATION",
    "加速": "OPTIMIZATION", "精简": "OPTIMIZATION", "升级": "OPTIMIZATION",
    "改善": "OPTIMIZATION", "调优": "OPTIMIZATION",
    "optimize": "OPTIMIZATION", "improve": "OPTIMIZATION",
    "enhance": "OPTIMIZATION", "fix": "OPTIMIZATION", "boost": "OPTIMIZATION",
    "upgrade": "OPTIMIZATION",
    "对比": "COMPARISON", "比较": "COMPARISON", "区别": "COMPARISON",
    "差异": "COMPARISON", "优劣": "COMPARISON", "优缺点": "COMPARISON",
    "compare": "COMPARISON", "comparison": "COMPARISON", "difference": "COMPARISON",
    "开发": "DEVELOPMENT", "创建": "DEVELOPMENT", "实现": "DEVELOPMENT",
    "构建": "DEVELOPMENT", "搭建": "DEVELOPMENT", "编写": "DEVELOPMENT",
    "develop": "DEVELOPMENT", "create": "DEVELOPMENT", "build": "DEVELOPMENT",
    "implement": "DEVELOPMENT",
    "回顾": "REFLECTION", "总结": "REFLECTION", "复盘": "REFLECTION",
    "反思": "REFLECTION", "梳理": "REFLECTION", "归纳": "REFLECTION",
    "汇总": "REFLECTION",
    "summary": "REFLECTION", "recap": "REFLECTION", "report": "REFLECTION",
}

INTENT_RULES: Dict[str, Dict[str, Any]] = {
    "ANALYSIS": {
        "keywords": [
            "评估", "分析", "看看", "怎么样", "诊断", "评价", "判断",
            "审视", "检视", "研究", "探查", "解析", "了解", "看下",
            "找一下", "找找", "查一下", "查查",
            "evaluate", "analyze", "analysis", "assess", "find", "look for",
            "search",
        ],
        "weight": 1.0,
    },
    "COMPARISON": {
        "keywords": [
            "对比", "区别", "优劣", "优缺点", "哪个", "比较", "相较",
            "差异", "不同", "vs", "versus", "哪个好", "选哪个",
            "compare", "comparison", "difference", "better",
        ],
        "weight": 1.0,
    },
    "OPTIMIZATION": {
        "keywords": [
            "优化", "改进", "提升", "修复", "提高", "增强", "改良",
            "加速", "精简", "压缩", "升级", "改善", "调优",
            "optimize", "improve", "enhance", "fix", "boost", "upgrade",
        ],
        "weight": 1.2,  # Action verbs: higher weight
    },
    "DEVELOPMENT": {
        "keywords": [
            "开发", "创建", "增加", "实现", "构建", "新建", "添加",
            "写一个", "做一个", "搭建", "编写", "生成", "制造",
            "develop", "create", "build", "implement", "add", "make",
            "write a", "generate",
        ],
        "weight": 1.2,  # Action verbs: higher weight
    },
    "DEBUGGING": {
        "keywords": [
            "为什么", "报错", "失败", "问题", "错误", "bug", "异常",
            "不行", "出了", "崩溃", "无法", "不能", "卡住",
            "why", "error", "failed", "failure", "issue", "problem",
            "broken", "crash", "not working", "exception",
        ],
        "weight": 1.2,
    },
    "REFLECTION": {
        "keywords": [
            "回顾", "总结", "关键词", "趋势", "统计", "报告",
            "复盘", "反思", "梳理", "归纳", "汇总",
            "summary", "recap", "review", "trend", "statistics",
            "report", "overview", "keywords",
        ],
        "weight": 1.1,
    },
}


def classify_text(text: str) -> Dict[str, Any]:
    """Classify user intent from a text message.

    Returns intent scores and the best match with confidence.
    Uses position-weighted scoring: keywords appearing later in the message
    get higher weight (the actual request often follows context-setting).
    """
    if not text or not text.strip():
        return {
            "intent": "UNKNOWN",
            "confidence": 0.0,
            "keywords_matched": [],
            "all_scores": {},
        }

    text_lower = text.lower()
    text_len = max(len(text_lower), 1)
    scores: Dict[str, float] = {}
    matched_keywords: Dict[str, List[str]] = {}

    for intent, rules in INTENT_RULES.items():
        score = 0.0
        keywords = rules["keywords"]
        weight = rules["weight"]

        for kw in keywords:
            kw_lower = kw.lower()
            idx = text_lower.find(kw_lower)
            if idx != -1:
                # Position bonus: keywords later in message get up to 1.5x weight
                position_factor = 1.0 + 0.5 * (idx / text_len)
                # Longer keyword matches are more specific
                score += len(kw) * weight * position_factor
                matched_keywords.setdefault(intent, []).append(kw)

        scores[intent] = score

    # Determine the best match
    if not any(scores.values()):
        return {
            "intent": "UNKNOWN",
            "confidence": 0.0,
            "keywords_matched": [],
            "all_scores": scores,
        }

    best_intent = max(scores, key=lambda k: scores[k])
    best_score = scores[best_intent]

    # Calculate confidence: best_score / (sum of top 2 scores)
    sorted_scores = sorted(scores.values(), reverse=True)
    if len(sorted_scores) > 1 and sorted_scores[1] > 0:
        confidence = best_score / (best_score + sorted_scores[1])
    else:
        confidence = 1.0 if best_score > 0 else 0.0

    # Normalize confidence to 0.0 - 1.0
    confidence = min(1.0, max(0.0, confidence))

    # Boost confidence if multiple keywords matched
    kw_count = len(matched_keywords.get(best_intent, []))
    if kw_count >= 3:
        confidence = min(1.0, confidence + 0.1)

    return {
        "intent": best_intent,
        "confidence": round(confidence, 2),
        "keywords_matched": matched_keywords.get(best_intent, []),
        "all_scores": {k: round(v, 2) for k, v in scores.items()},
    }


# ---------------------------------------------------------------------------
# First user message extractor
# ---------------------------------------------------------------------------

def extract_first_user_message(jsonl_path: str, agent: str = "auto") -> Optional[str]:
    """Extract the first real user message from any supported session via echolib."""
    del agent
    try:
        for msg in echolib.dispatch_extract_messages(jsonl_path, role="user", limit=1):
            text = (msg.get("text") or "").strip()
            if text:
                return text
    except Exception:
        pass
    return None


def detect_agent_type(jsonl_path: str) -> str:
    """Detect which agent produced the session via echolib."""
    return echolib.dispatch_resolve_agent(jsonl_path)


def _lookup_session_in_index(session_id: str) -> Tuple[Optional[str], str, str]:
    """Return (jsonl_path, agent, first_prompt) from index.db when available."""
    if not session_id or not DB_PATH.exists():
        return None, "", ""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            row = conn.execute(
                "SELECT jsonl_path, agent, first_prompt FROM sessions "
                "WHERE id = ? OR id LIKE ? LIMIT 1",
                (session_id, f"%:{session_id}"),
            ).fetchone()
            if row:
                return row[0], row[1] or "", row[2] or ""
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    return None, "", ""


def resolve_jsonl_path(session_id: str) -> Optional[str]:
    """Resolve session_id -> transcript path or virtual URI via index.db."""
    if not session_id:
        return None
    if is_virtual_path(session_id) or os.path.exists(session_id):
        return session_id
    path, _, _ = _lookup_session_in_index(session_id)
    return path


def main():
    parser = argparse.ArgumentParser(
        description="Classify user intent across all session-digger environments"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--session", help="Session ID to analyze")
    group.add_argument("--jsonl", help="Direct path or virtual URI (zcode://, dimcode://)")
    group.add_argument("--text", help="Direct text to classify")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print output")

    args = parser.parse_args()
    output: Dict[str, Any] = {}

    if args.text:
        result = classify_text(args.text)
        output["input_text"] = args.text[:200]
        output.update(result)
    elif args.jsonl or args.session:
        jsonl_path = args.jsonl
        cached_agent = ""
        cached_prompt = ""
        if args.session:
            if is_virtual_path(args.session) or os.path.exists(args.session):
                jsonl_path = args.session
            else:
                jsonl_path, cached_agent, cached_prompt = _lookup_session_in_index(args.session)
            if not jsonl_path:
                print(json.dumps({"error": f"Cannot resolve session: {args.session}"}), file=sys.stderr)
                sys.exit(1)

        if not is_virtual_path(jsonl_path) and not os.path.exists(jsonl_path):
            print(json.dumps({"error": f"File not found: {jsonl_path}"}), file=sys.stderr)
            sys.exit(1)

        agent = cached_agent or detect_agent_type(jsonl_path)
        first_message = cached_prompt or extract_first_user_message(jsonl_path, agent)
        result = classify_text(first_message or "")

        output["session_path"] = jsonl_path
        output["agent"] = agent
        output["first_message"] = (first_message or "")[:300]
        output.update(result)

    print(json.dumps(output, ensure_ascii=False, indent=2 if args.pretty else None))


if __name__ == "__main__":
    main()
