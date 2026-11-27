#!/usr/bin/env python3
"""
intent-classify.py — Classify user intent from session messages across all environments.

Supports `--session`, `--jsonl` (file path or virtual URI), `--text`, and single-pass
`--with-errors` combined diagnosis via ``echolib.dispatch_parse_session()``.
"""

import argparse
import importlib.util
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SKILLS_DIR = _REPO_ROOT / "skills"
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _p in (_SCRIPTS_DIR, _SKILLS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import common_paths  # noqa: E402
import echolib  # noqa: E402
from echolib._helpers import get_ro_sqlite  # noqa: E402
from index_builder._reader import is_virtual_path  # noqa: E402

# ---------------------------------------------------------------------------
# Intent keyword rules & precompiled word-boundary matchers
# ---------------------------------------------------------------------------

_ASCII_WORD_RE = re.compile(r"^[A-Za-z0-9_.\- ]+$")
_CLAUSE_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")
_PURE_PATH_OR_CMD_RE = re.compile(r"^(?:/[\w./-]+|https?://\S+|\$[A-Z0-9_]+|/[a-z-]+)$")

ACTION_VERBS: Dict[str, str] = {
    "优化": "OPTIMIZATION", "改进": "OPTIMIZATION", "提升": "OPTIMIZATION",
    "提高": "OPTIMIZATION", "增强": "OPTIMIZATION",
    "加速": "OPTIMIZATION", "精简": "OPTIMIZATION", "升级": "OPTIMIZATION",
    "改善": "OPTIMIZATION", "调优": "OPTIMIZATION",
    "optimize": "OPTIMIZATION", "improve": "OPTIMIZATION",
    "enhance": "OPTIMIZATION", "boost": "OPTIMIZATION",
    "upgrade": "OPTIMIZATION",
    "修复": "DEBUGGING", "排查": "DEBUGGING", "定位报错": "DEBUGGING", "修一下": "DEBUGGING",
    "fix": "DEBUGGING", "debug": "DEBUGGING", "troubleshoot": "DEBUGGING",
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
            "search", "check", "inspect", "audit",
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
            "优化", "改进", "提升", "提高", "增强", "改良",
            "加速", "精简", "压缩", "升级", "改善", "调优",
            "optimize", "improve", "enhance", "boost", "upgrade", "update",
        ],
        "weight": 1.2,
    },
    "DEVELOPMENT": {
        "keywords": [
            "开发", "创建", "增加", "实现", "构建", "新建", "添加",
            "写一个", "做一个", "搭建", "编写", "生成", "制造",
            "develop", "create", "build", "implement", "add", "make",
            "write a", "generate",
        ],
        "weight": 1.2,
    },
    "DEBUGGING": {
        "keywords": [
            "为什么", "报错", "失败", "问题", "错误", "修复", "排查", "定位报错", "修一下",
            "bug", "异常", "不行", "出了", "崩溃", "无法", "不能", "卡住",
            "why", "error", "failed", "failure", "issue", "problem",
            "broken", "crash", "not working", "exception", "fix", "debug", "troubleshoot",
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


def _compile_kw_pattern(kw: str) -> Optional[re.Pattern]:
    """ASCII keywords require word boundaries so 'add' never matches 'padding'."""
    if _ASCII_WORD_RE.match(kw):
        return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(kw) + r"(?![A-Za-z0-9_])", re.IGNORECASE)
    return None


def _match_kw(kw: str, pat: Optional[re.Pattern], text_lower: str) -> int:
    """Return last-match start index, or -1 if absent."""
    if pat is not None:
        m = None
        for m in pat.finditer(text_lower):
            pass
        return m.start() if m is not None else -1
    return text_lower.rfind(kw.lower())


_COMPILED_INTENT_RULES: Dict[str, Dict[str, Any]] = {
    intent: {
        "weight": cfg["weight"],
        "matchers": [(kw, _compile_kw_pattern(kw)) for kw in cfg["keywords"]],
    }
    for intent, cfg in INTENT_RULES.items()
}
_COMPILED_ACTION_VERBS: List[Tuple[str, str, Optional[re.Pattern]]] = [
    (kw, intent, _compile_kw_pattern(kw)) for kw, intent in ACTION_VERBS.items()
]


def _tail_clause(text_lower: str) -> str:
    """Extract the final non-empty clause (where actual intent usually sits)."""
    parts = [p.strip() for p in _CLAUSE_SPLIT_RE.split(text_lower) if p.strip()]
    if len(parts) >= 2:
        return parts[-1]
    cutoff = max(0, int(len(text_lower) * 0.6))
    return text_lower[cutoff:]


def classify_text(text: str) -> Dict[str, Any]:
    """Classify user intent from text with ASCII word boundaries & trailing action-verb boost."""
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

    for intent, rules in _COMPILED_INTENT_RULES.items():
        score = 0.0
        weight = rules["weight"]
        for kw, pat in rules["matchers"]:
            idx = _match_kw(kw, pat, text_lower)
            if idx != -1:
                position_factor = 1.0 + 0.5 * (idx / text_len)
                score += len(kw) * weight * position_factor
                matched_keywords.setdefault(intent, []).append(kw)
        scores[intent] = score

    # Boost intents signaled by explicit action verbs in the final clause/goal segment.
    tail = _tail_clause(text_lower)
    boosted_intents = set()
    for kw, target_intent, pat in _COMPILED_ACTION_VERBS:
        if _match_kw(kw, pat, tail) != -1 and scores.get(target_intent, 0.0) > 0:
            boosted_intents.add(target_intent)
    for intent in boosted_intents:
        scores[intent] *= 1.35

    if not any(scores.values()):
        return {
            "intent": "UNKNOWN",
            "confidence": 0.0,
            "keywords_matched": [],
            "all_scores": scores,
        }

    best_intent = max(scores, key=lambda k: scores[k])
    best_score = scores[best_intent]
    sorted_scores = sorted(scores.values(), reverse=True)
    if len(sorted_scores) > 1 and sorted_scores[1] > 0:
        confidence = best_score / (best_score + sorted_scores[1])
    else:
        confidence = 1.0 if best_score > 0 else 0.0
    confidence = min(1.0, max(0.0, confidence))
    if len(matched_keywords.get(best_intent, [])) >= 3:
        confidence = min(1.0, confidence + 0.1)

    return {
        "intent": best_intent,
        "confidence": round(confidence, 2),
        "keywords_matched": matched_keywords.get(best_intent, []),
        "all_scores": {k: round(v, 2) for k, v in scores.items()},
    }


def _is_weak_prompt(text: str) -> bool:
    s = (text or "").strip()
    if len(s) < 6:
        return True
    if _PURE_PATH_OR_CMD_RE.match(s):
        return True
    return False


def classify_messages(messages: List[str]) -> Dict[str, Any]:
    """Classify session intent using up to the first 3 meaningful user messages."""
    cleaned = [m.strip() for m in messages if m and m.strip()]
    if not cleaned:
        return classify_text("")
    non_weak = [m for m in cleaned if not _is_weak_prompt(m)]
    if non_weak:
        first_res = classify_text(non_weak[0])
        if first_res["intent"] != "UNKNOWN":
            return first_res
        candidates = non_weak[:3]
    else:
        candidates = cleaned[:3]

    merged_scores: Dict[str, float] = {}
    merged_kws: Dict[str, List[str]] = {}
    weights = (1.0, 0.85, 0.7)
    for idx, msg in enumerate(candidates):
        r = classify_text(msg)
        w = weights[idx]
        for k, v in r.get("all_scores", {}).items():
            merged_scores[k] = merged_scores.get(k, 0.0) + float(v) * w
        if r.get("intent") and r["intent"] != "UNKNOWN":
            for kw in r.get("keywords_matched", []):
                merged_kws.setdefault(r["intent"], [])
                if kw not in merged_kws[r["intent"]]:
                    merged_kws[r["intent"]].append(kw)

    if not any(merged_scores.values()):
        return classify_text(candidates[0])
    best_intent = max(merged_scores, key=lambda k: merged_scores[k])
    sorted_scores = sorted(merged_scores.values(), reverse=True)
    best_score = merged_scores[best_intent]
    conf = best_score / (best_score + sorted_scores[1]) if len(sorted_scores) > 1 and sorted_scores[1] > 0 else 1.0
    return {
        "intent": best_intent,
        "confidence": round(min(1.0, max(0.0, conf)), 2),
        "keywords_matched": merged_kws.get(best_intent, []),
        "all_scores": {k: round(v, 2) for k, v in merged_scores.items()},
    }


# ---------------------------------------------------------------------------
# Session extraction & single-pass combined diagnosis
# ---------------------------------------------------------------------------

def _load_erc_module():
    erc_path = Path(__file__).resolve().with_name("error-root-cause.py")
    spec = importlib.util.spec_from_file_location("_deep_analysis_erc", str(erc_path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def extract_user_messages(jsonl_path: str, limit: int = 3) -> List[str]:
    try:
        msgs = echolib.dispatch_extract_messages(jsonl_path, role="user", no_tools=True, limit=limit)
        return [(m.get("text") or "").strip() for m in msgs if (m.get("text") or "").strip()]
    except Exception:
        return []


def extract_first_user_message(jsonl_path: str, agent: str = "auto") -> Optional[str]:
    """Extract the first real user message from any supported session via echolib."""
    del agent
    msgs = extract_user_messages(jsonl_path, limit=1)
    return msgs[0] if msgs else None


def detect_agent_type(jsonl_path: str) -> str:
    """Detect which agent produced the session via echolib."""
    return echolib.dispatch_resolve_agent(jsonl_path)


def _lookup_session_in_index(session_id: str) -> Tuple[Optional[str], str, str]:
    """Return (jsonl_path, agent, first_prompt) from index.db via shared read-only pool."""
    db = common_paths.index_db_path()
    if not session_id or not db.is_file():
        return None, "", ""
    try:
        conn = get_ro_sqlite(db)
        row = conn.execute(
            "SELECT jsonl_path, agent, first_prompt FROM sessions "
            "WHERE id = ? OR id LIKE ? LIMIT 1",
            (session_id, f"%:{session_id}"),
        ).fetchone()
        if row:
            return row[0], row[1] or "", row[2] or ""
    except sqlite3.Error:
        pass
    ref = common_paths.resolve_session_ref(session_id)
    return ref, "", ""


def resolve_jsonl_path(session_id: str) -> Optional[str]:
    """Resolve session_id -> transcript path or virtual URI."""
    return common_paths.resolve_session_ref(session_id)


def diagnose_session(
    jsonl_path: str,
    agent_hint: str = "",
    first_prompt_hint: str = "",
) -> Dict[str, Any]:
    """Single-pass diagnosis returning BOTH intent and error root causes."""
    parsed = None
    try:
        parsed = echolib.dispatch_parse_session(jsonl_path)
    except Exception:
        parsed = None

    agent = agent_hint or detect_agent_type(jsonl_path)
    if parsed is not None:
        user_msgs = [
            (m.get("text") or "").strip()
            for m in parsed.get("messages", [])
            if (m.get("role") or "").lower() == "user" and (m.get("text") or "").strip()
        ]
        if first_prompt_hint and (not user_msgs or user_msgs[0] != first_prompt_hint.strip()):
            user_msgs = [first_prompt_hint.strip()] + user_msgs
        tools = parsed.get("tools", [])
    else:
        user_msgs = []
        if first_prompt_hint and not _is_weak_prompt(first_prompt_hint):
            user_msgs = [first_prompt_hint.strip()]
        else:
            extracted = extract_user_messages(jsonl_path, limit=3)
            if first_prompt_hint:
                user_msgs = [first_prompt_hint.strip()] + [m for m in extracted if m != first_prompt_hint.strip()]
            else:
                user_msgs = extracted
        tools = echolib.dispatch_extract_tools(jsonl_path, errors_only=True, limit=0)

    intent_res = classify_messages(user_msgs)
    erc = _load_erc_module()
    errors = erc.extract_errors_from_tools(tools, agent)
    return {
        "session_path": jsonl_path,
        "agent": agent,
        "first_message": (user_msgs[0] if user_msgs else "")[:300],
        "intent": intent_res,
        "errors": errors,
        "error_summary": {
            "total_errors": len(errors),
            "by_type": erc.summarize_errors(errors),
        },
    }


def main():
    parser = argparse.ArgumentParser(
        description="Classify user intent across all session-digger environments"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--session", help="Session ID to analyze")
    group.add_argument("--jsonl", help="Direct path or virtual URI (zcode://, dimcode://)")
    group.add_argument("--text", help="Direct text to classify")
    parser.add_argument("--with-errors", action="store_true", help="Also extract & classify error root causes in one pass")
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

        if args.with_errors:
            output = diagnose_session(jsonl_path, agent_hint=cached_agent, first_prompt_hint=cached_prompt)
        else:
            agent = cached_agent or detect_agent_type(jsonl_path)
            if cached_prompt and not _is_weak_prompt(cached_prompt):
                msgs = [cached_prompt]
            else:
                msgs = extract_user_messages(jsonl_path, limit=3)
                if cached_prompt and (not msgs or msgs[0] != cached_prompt):
                    msgs = [cached_prompt] + msgs
            result = classify_messages(msgs)
            output["session_path"] = jsonl_path
            output["agent"] = agent
            output["first_message"] = (msgs[0] if msgs else "")[:300]
            output.update(result)

    print(json.dumps(output, ensure_ascii=False, indent=2 if args.pretty else None))


if __name__ == "__main__":
    main()
