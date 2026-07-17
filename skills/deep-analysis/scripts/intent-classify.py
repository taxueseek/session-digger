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
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
    """Extract the first real user message from a JSONL file."""
    if agent == "auto":
        agent = detect_agent_type(jsonl_path)

    try:
        if agent == "claude_code":
            return _first_user_claude(jsonl_path)
        elif agent == "grok":
            return _first_user_grok(jsonl_path)
        elif agent == "kimi_code":
            return _first_user_kimi(jsonl_path)
        elif agent == "codex":
            return _first_user_codex(jsonl_path)
        else:
            # Try all extractors
            for extractor in [_first_user_claude, _first_user_grok,
                              _first_user_kimi, _first_user_codex]:
                result = extractor(jsonl_path)
                if result:
                    return result
    except (IOError, OSError):
        pass
    return None


def _first_user_claude(jsonl_path: str) -> Optional[str]:
    """Extract first user message from Claude Code JSONL."""
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line.strip())
            except (json.JSONDecodeError, ValueError):
                continue
            if record.get("type") != "user":
                continue
            message = record.get("message", {})
            if message.get("role") != "user":
                continue
            content = message.get("content", "")
            # Skip meta/compact messages
            if record.get("isMeta") or record.get("isCompactSummary"):
                continue
            # Skip tool result-only messages
            if isinstance(content, list):
                texts = []
                for block in content:
                    if block.get("type") == "text":
                        texts.append(block.get("text", ""))
                if texts:
                    return " ".join(texts).strip()
            elif isinstance(content, str) and content.strip():
                return content.strip()
    return None


def _first_user_grok(jsonl_path: str) -> Optional[str]:
    """Extract first user message from Grok chat_history.jsonl."""
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line.strip())
            except (json.JSONDecodeError, ValueError):
                continue
            if record.get("type") != "user":
                continue
            content = record.get("content", "")
            if isinstance(content, list):
                texts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        # Skip system reminders, user_info blocks
                        if text.startswith("<user_info>") or text.startswith("<system-reminder>"):
                            continue
                        # Extract from <user_query> tags (Grok's real user input format)
                        uq_match = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.DOTALL)
                        if uq_match:
                            query = uq_match.group(1).strip()
                            if query:
                                return query
                        # Skip embedded system-reminder tags within text
                        text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL).strip()
                        # Skip if only contains XML tags or noise
                        if not text or re.match(r"^<[^>]+>", text):
                            continue
                        texts.append(text)
                if texts:
                    combined = " ".join(texts).strip()
                    # Must be a real user query (has CJK chars or meaningful length)
                    if combined and len(combined) > 15 and re.search(r"[一-鿿]", combined):
                        return combined
            elif isinstance(content, str) and len(content.strip()) > 15:
                # Try <user_query> tag extraction
                uq_match = re.search(r"<user_query>\s*(.*?)\s*</user_query>", content, re.DOTALL)
                if uq_match:
                    query = uq_match.group(1).strip()
                    if query:
                        return query
                # Verify it's not just a system message
                if re.search(r"[一-鿿]", content):
                    return content.strip()
    return None


def _first_user_kimi(wire_path: str) -> Optional[str]:
    """Extract first user message from Kimi wire.jsonl."""
    with open(wire_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line.strip())
            except (json.JSONDecodeError, ValueError):
                continue
            message = record.get("message", {})
            if message.get("type") != "TurnBegin":
                continue
            payload = message.get("payload", {})
            user_input = payload.get("user_input", [])
            texts = []
            for item in user_input:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text", "")
                    if not text.startswith("<user_info>") and not text.startswith("<system-reminder>"):
                        texts.append(text)
            if texts:
                return " ".join(texts).strip()
    return None


def _first_user_codex(jsonl_path: str) -> Optional[str]:
    """Extract first user message from Codex JSONL."""
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line.strip())
            except (json.JSONDecodeError, ValueError):
                continue
            if record.get("type") != "response_item":
                continue
            payload = record.get("payload", {})
            if not isinstance(payload, dict):
                continue
            if payload.get("type") != "message":
                continue
            if payload.get("role") != "user":
                continue
            content = payload.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "input_text":
                        text = block.get("text", "").strip()
                        if text and not text.startswith("[Request"):
                            return text
    return None


def detect_agent_type(jsonl_path: str) -> str:
    """Detect which agent produced the JSONL file."""
    path_lower = jsonl_path.lower()
    if ".codex" in path_lower:
        return "codex"
    if ".kimi" in path_lower:
        return "kimi_code"
    if ".grok" in path_lower:
        return "grok"

    # Claude Code heuristic: look for is_error in user messages
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= 30:
                    break
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if record.get("type") == "user":
                    msg = record.get("message", {})
                    if isinstance(msg, dict):
                        content = msg.get("content", [])
                        if isinstance(content, list):
                            for block in content:
                                if block.get("type") == "tool_result":
                                    return "claude_code"
    except (IOError, OSError):
        pass

    return "unknown"


# ---------------------------------------------------------------------------
# Session ID resolver（与 error-root-cause 共用 common_paths / index.db）
# ---------------------------------------------------------------------------

def resolve_jsonl_path(session_id: str) -> Optional[str]:
    """Resolve session_id → JSONL。优先 session-digger index.db。"""
    skills_dir = Path(__file__).resolve().parents[2]
    if str(skills_dir) not in sys.path:
        sys.path.insert(0, str(skills_dir))
    try:
        from common_paths import resolve_session_jsonl  # type: ignore
    except ImportError:
        return None
    p = resolve_session_jsonl(session_id)
    return str(p) if p is not None else None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Classify user intent from session first message"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--session", help="Session ID to analyze")
    group.add_argument("--jsonl", help="Direct path to JSONL file")
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
        if args.session:
            resolved = resolve_jsonl_path(args.session)
            if not resolved:
                print(json.dumps({
                    "error": f"Cannot resolve session: {args.session}"
                }), file=sys.stderr)
                sys.exit(1)
            jsonl_path = resolved

        if not os.path.exists(jsonl_path):
            print(json.dumps({
                "error": f"File not found: {jsonl_path}"
            }), file=sys.stderr)
            sys.exit(1)

        agent = detect_agent_type(jsonl_path)
        first_message = extract_first_user_message(jsonl_path, agent)
        result = classify_text(first_message or "")

        output["session_path"] = jsonl_path
        output["agent"] = agent
        output["first_message"] = (first_message or "")[:300]
        output.update(result)

    indent = 2 if args.pretty else None
    print(json.dumps(output, ensure_ascii=False, indent=indent))


if __name__ == "__main__":
    main()
