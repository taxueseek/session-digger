#!/usr/bin/env python3
"""
format-detector.py — Identify which agent produced a transcript file.

A signature-matching router, not a hardcoded parser selector. New agent
formats should be added to references/format-signatures.md as new signatures,
not as new code branches, unless the format needs genuinely different parsing.

Complements session-digger's echolib adapter registry: the registry handles
known agents (Claude Code, Grok, Kimi Code), while this script handles
unknown/external transcripts that don't match any registered adapter.

Usage:
    python format-detector.py <path1> [path2 ...]
    python format-detector.py ~/.claude/projects/-Users-example/*.jsonl

Output: JSON to stdout, one entry per file:
{
  "path": "...",
  "format": "claude_code_jsonl" | "grok_jsonl" | "kimi_code_jsonl" |
            "generic_markdown" | "cline_like" | "aider_like" | "unknown",
  "confidence": "high" | "medium" | "low",
  "evidence": ["..."],
  "sample_keys": [...],
  "adapter": "claude" | "grok" | "kimi_code" | null  # maps to echolib adapter
}
"""

import json
import os
import sys


# --- Known signatures -------------------------------------------------
# Each signature is a scoring function. Higher score = more confident match.

def _is_claude_code_jsonl(lines):
    """Claude Code session logs: JSONL with type/user/assistant/system,
    sessionId/uuid, cwd, gitBranch, toolUseResult.

    Uses indicator set to avoid false positives from environments that share
    some fields (e.g. deepcode has sessionId but no cwd/toolUseResult).
    """
    evidence = []
    keys_seen = set()
    checked = 0
    indicators = set()
    for line in lines[:20]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return 0, [], []
        checked += 1
        if isinstance(obj, dict):
            keys_seen.update(obj.keys())
            if "sessionId" in obj or "uuid" in obj:
                indicators.add("session_id")
            if obj.get("type") in ("user", "assistant", "system", "tool_use", "tool_result"):
                indicators.add("type_field")
            if "cwd" in obj or "gitBranch" in obj:
                indicators.add("cwd")
                evidence.append("cwd/gitBranch field present (Claude Code specific)")
            if "toolUseResult" in obj:
                indicators.add("toolUseResult")
                evidence.append("toolUseResult field present (Claude Code specific)")
            msg = obj.get("message")
            if isinstance(msg, dict) and msg.get("model", "").startswith("claude-"):
                indicators.add("claude_model")
    if checked == 0:
        return 0, [], []
    # Score: type_field required, plus strong indicators
    score = 0
    if "type_field" in indicators:
        score += 2
    if "cwd" in indicators:
        score += 4
    if "toolUseResult" in indicators:
        score += 4
    if "claude_model" in indicators:
        score += 3
    if "session_id" in indicators:
        score += 1
    # Bare type=user|assistant is shared by Grok/Kimi/others — do not claim Claude
    # on type_field alone (score 2). Require ≥3 so cwd/toolUseResult/model/session+type win.
    if score < 3:
        return 0, [], sorted(keys_seen)
    if score >= 6:
        evidence.append(f"jsonl structural score={score} (indicators: {indicators})")
    return score, evidence, sorted(keys_seen)


def _is_grok_jsonl(lines):
    """Grok Build sessions: JSONL with type/content structure.

    Uses indicator set — only truly Grok-specific signals count:
    - type=reasoning (not in Claude Code)
    - assistant message with tool_calls array
    - model_id field (Grok uses model_id, not model)
    - synthetic_reason field
    """
    evidence = []
    keys_seen = set()
    checked = 0
    indicators = set()
    for line in lines[:20]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return 0, [], []
        checked += 1
        if isinstance(obj, dict):
            keys_seen.update(obj.keys())
            rtype = obj.get("type", "")
            if rtype == "reasoning":
                indicators.add("reasoning")
                evidence.append("type='reasoning' (Grok-specific)")
            if rtype == "assistant" and "tool_calls" in obj:
                indicators.add("assistant_tool_calls")
                evidence.append("assistant message with tool_calls array (Grok-specific)")
            if "model_id" in obj:
                indicators.add("model_id")
                evidence.append("model_id field present (Grok-specific)")
            if "synthetic_reason" in obj:
                indicators.add("synthetic_reason")
                evidence.append("synthetic_reason field (Grok-specific)")
    if checked == 0:
        return 0, [], []
    score = 0
    if "reasoning" in indicators:
        score += 4
    if "assistant_tool_calls" in indicators:
        score += 4
    if "model_id" in indicators:
        score += 3
    if "synthetic_reason" in indicators:
        score += 2
    if score >= 4:
        evidence.append(f"grok structural score={score} (indicators: {indicators})")
    return score, evidence, sorted(keys_seen)


def _is_kimi_code_jsonl(lines):
    """Kimi Code sessions: wire.jsonl with specific event structure.

    Distinguishes kimi_code from kimi non-code by checking for kimi_code-specific
    type values (turn.prompt, context.append_loop_event, etc.) vs kimi non-code
    message types (TurnBegin, StepBegin, ContentPart).
    """
    score = 0
    evidence = []
    keys_seen = set()
    checked = 0
    has_kimi_code_types = False
    has_kimi_noncode_types = False
    kimi_code_specific = {"config.update", "turn.prompt", "context.append_loop_event",
                          "context.append_message", "tools.set_active_tools",
                          "tools.update_store", "usage.record",
                          "full_compaction.begin", "turn.cancel",
                          "permission.record_approval_result"}
    for line in lines[:20]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return 0, [], []
        checked += 1
        if isinstance(obj, dict):
            keys_seen.update(obj.keys())
            rtype = obj.get("type", "")
            # Kimi-specific type values (high confidence)
            if rtype in kimi_code_specific:
                score += 2
                has_kimi_code_types = True
            # protocol_version is shared by both kimi_code and kimi_noncode
            if "protocol_version" in obj:
                score += 5
                evidence.append("protocol_version field (Kimi family)")
            # Detect kimi non-code message types
            msg = obj.get("message", {})
            if isinstance(msg, dict):
                msg_type = msg.get("type", "")
                if msg_type in ("TurnBegin", "StepBegin", "ContentPart", "ToolCallBegin", "ToolCallEnd"):
                    has_kimi_noncode_types = True
            # context.append_loop_event with nested event
            if rtype == "context.append_loop_event":
                event = obj.get("event", {})
                if isinstance(event, dict):
                    etype = event.get("type", "")
                    if etype in ("content.part", "tool.call", "tool.result",
                                 "step.begin", "step.end"):
                        score += 3
                        evidence.append(f"nested event.type='{etype}' (Kimi Code specific)")
            # turn.prompt is unique to Kimi Code
            if rtype == "turn.prompt":
                score += 3
                evidence.append("turn.prompt type (Kimi Code unique)")
            # time field (Kimi uses 'time', not 'timestamp')
            if "time" in obj and "timestamp" not in obj:
                score += 1
            # kimi in model name
            if "kimi" in str(obj.get("model", obj.get("modelAlias", ""))).lower():
                score += 4
                evidence.append("model field contains 'kimi'")
    # If kimi non-code types are present but kimi code types are not,
    # this is kimi non-code format
    if has_kimi_noncode_types and not has_kimi_code_types:
        score = 0
        evidence = ["kimi non-code format (TurnBegin/StepBegin/ContentPart) — not kimi_code"]
    if score >= 4:
        evidence.append(f"kimi_code structural score={score}")
    return score, evidence, sorted(keys_seen)


def _is_codex_jsonl(lines):
    """Codex sessions: rollout-*.jsonl with response_item/payload structure.

    Codex format characteristics:
    - type field: "response_item"
    - payload.type: "message", "function_call", "reasoning"
    - timestamp field at top level
    - payload.role for message type (user/assistant)
    """
    score = 0
    evidence = []
    keys_seen = set()
    checked = 0
    for line in lines[:20]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return 0, [], []
        checked += 1
        if isinstance(obj, dict):
            keys_seen.update(obj.keys())
            if obj.get("type") == "response_item":
                score += 3
                evidence.append("type='response_item' (Codex specific)")
            payload = obj.get("payload", {})
            if isinstance(payload, dict):
                ptype = payload.get("type", "")
                if ptype in ("message", "function_call", "reasoning"):
                    score += 2
                if ptype == "message" and "role" in payload:
                    score += 1
    if score >= 4:
        evidence.append(f"codex structural score={score}")
    return score, evidence, sorted(keys_seen)


def _is_generic_agent_markdown(text):
    """Exported/markdown-style transcripts: role markers, code fences."""
    score = 0
    evidence = []
    markers = ["Human:", "User:", "Assistant:", "### Tool", "```", "> Tool call", "AI:"]
    for m in markers:
        if m in text:
            score += 1
            evidence.append(f"found marker '{m}'")
    return score, evidence


def _is_other_known_agent(lines, text):
    """Placeholder hooks for other agents (Cline, Aider, etc)."""
    candidates = []

    # Cline / Roo-Code: say/ask + ts fields
    for line in lines[:10]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and ("say" in obj or "ask" in obj) and "ts" in obj:
            candidates.append(("cline_like", 4, ["'say'/'ask'+'ts' fields (Cline-family signature)"]))
            break

    # Aider: .aider.chat.history.md or #### markers
    if "aider" in text.lower()[:2000] or "#### " in text[:2000]:
        candidates.append(("aider_like", 2, ["'aider' keyword or '#### ' prompt marker"]))

    if candidates:
        return max(candidates, key=lambda c: c[1])
    return (None, 0, [])


def detect_one(path):
    """Detect the format of a single file."""
    result = {
        "path": path, "format": "unknown", "confidence": "low",
        "evidence": [], "sample_keys": [], "adapter": None,
    }
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception as e:
        result["format"] = "error"
        result["evidence"] = [f"could not read file: {e}"]
        return result

    lines = text.splitlines()

    # Score each known format
    cc_score, cc_evidence, keys = _is_claude_code_jsonl(lines)
    grok_score, grok_evidence, _ = _is_grok_jsonl(lines)
    kimi_score, kimi_evidence, _ = _is_kimi_code_jsonl(lines)
    codex_score, codex_evidence, _ = _is_codex_jsonl(lines)
    other_agent, other_score, other_evidence = _is_other_known_agent(lines, text)
    md_score, md_evidence = _is_generic_agent_markdown(text)

    scored = [
        ("claude_code_jsonl", cc_score, cc_evidence, "claude"),
        ("grok_jsonl", grok_score, grok_evidence, "grok"),
        ("kimi_code_jsonl", kimi_score, kimi_evidence, "kimi_code"),
        ("codex_jsonl", codex_score, codex_evidence, "codex"),
        (other_agent or "other_agent_unmatched", other_score, other_evidence, None),
        ("generic_markdown", md_score, md_evidence, None),
    ]
    scored = [s for s in scored if s[1] > 0]

    if not scored:
        result["format"] = "unknown"
        result["confidence"] = "low"
        result["evidence"] = ["no known signature matched — candidate for new format signature"]
        result["sample_keys"] = keys
        return result

    scored.sort(key=lambda s: s[1], reverse=True)
    best_format, best_score, best_evidence, adapter = scored[0]

    result["format"] = best_format
    result["evidence"] = best_evidence
    result["sample_keys"] = keys
    result["adapter"] = adapter
    if best_score >= 6:
        result["confidence"] = "high"
    elif best_score >= 3:
        result["confidence"] = "medium"
    else:
        result["confidence"] = "low"
        result["evidence"].append("weak match — treat as unconfirmed, consider adding a signature")

    return result


def main():
    args = [a for a in sys.argv[1:] if a not in ("-h", "--help")]
    if not args or any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(
            "usage: format-detector.py <path> [path ...]\n"
            "Detect agent transcript format (Claude/Grok/Kimi/Codex/…).\n"
            "Accepts files or directories; prints JSON results.",
            file=sys.stderr if not args else sys.stdout,
        )
        sys.exit(0 if any(a in ("-h", "--help") for a in sys.argv[1:]) else 1)

    results = []
    for path in args:
        if os.path.isdir(path):
            for root, _, files in os.walk(path):
                for fn in files:
                    if fn.startswith("."):
                        continue
                    results.append(detect_one(os.path.join(root, fn)))
        else:
            results.append(detect_one(path))

    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
