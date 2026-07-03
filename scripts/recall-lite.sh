#!/usr/bin/env bash
# recall-lite.sh — Local-only session search. Zero API calls.
#
# Pure-shell counterpart to /session-digger:recall. Use this when you cannot or
# do not want to spend a Claude model turn — for example when your session is
# pinned to a billing tier (such as 1M-context Opus with Extra Usage disabled)
# that rejects requests outright, or when you just want fast, deterministic
# raw matches without synthesis.
#
# v0.7: 实现跨代理搜索 — --agent cross 不再只是文档死 API，真正搜索所有环境。
#
# Usage:
#   recall-lite.sh <keyword> [--scope current|all] [--limit N] [--deep] [--decisions] [--agent claude|grok|kimi_code|cross|auto] [--no-summary]
#
#   <keyword>          Single search term. Use the most distinctive word from
#                      your question. Substring match, case-insensitive at the
#                      index level. Required.
#   --scope current    Search only the current project's sessions (default).
#   --scope all        Search across every project under ~/.claude/projects.
#   --limit N          Number of matching sessions to deep-dive into. Default 5.
#   --deep             Also dump full conversation excerpts (--thinking off,
#                      role both, up to 30 messages) instead of only user
#                      messages and tool errors. Slower; produces more output.
#   --decisions        Decision-point filter: extract only messages containing
#                      decision keywords (decided, chose, instead, going to use,
#                      will use, decided to, 决定, 选择, 改用). Inspired by
#                      session-recall --decisions. Great for "why did we..."
#                      archaeology.
#   --agent            Which agent's sessions to search (default: auto-detect).
#   --no-summary       跳过摘要缓存，强制全量解析（调试用）。
#
# Output:
#   1. A header listing matching sessions (tab-separated, 9 fields).
#   2. For each of the top N matches:
#      a. 如果有新鲜摘要 → 直接输出摘要（标记 [CACHED]）
#      b. 如果无摘要 → 全量解析提取证据（现有逻辑）
#   3. 提示用户可用 /save-summary 保存分析结果
#
# This is raw evidence, not a synthesized summary. You read it yourself.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $# -lt 1 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 2
fi

QUERY="$1"
shift

SCOPE="current"
LIMIT=5
DEEP=0
DECISIONS=0
AGENT="auto"
NO_SUMMARY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope) SCOPE="$2"; shift 2 ;;
    --limit) LIMIT="$2"; shift 2 ;;
    --deep)  DEEP=1; shift ;;
    --decisions) DECISIONS=1; shift ;;
    --agent) AGENT="$2"; shift 2 ;;
    --no-summary) NO_SUMMARY=1; shift ;;
    *) echo "ERROR: Unknown option: $1" >&2; exit 1 ;;
  esac
done

if ! [[ "$LIMIT" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --limit must be a number, got: $LIMIT" >&2
  exit 1
fi

if [[ "$SCOPE" != "current" && "$SCOPE" != "all" && "$SCOPE" != "auto" ]]; then
  echo "ERROR: --scope must be 'current', 'all', or 'auto', got: $SCOPE" >&2
  exit 1
fi

echo "=== recall-lite: query='$QUERY' scope=$SCOPE limit=$LIMIT agent=$AGENT decisions=$DECISIONS ==="
if [[ "$NO_SUMMARY" -eq 0 ]]; then
  echo "  (摘要优先：已分析的会话直接读缓存，--no-summary 可跳过)"
fi
echo

# list-sessions.sh exits 1 when no entries match, even if sessions exist for
# the project — that's a known quirk. Capture stderr so we can distinguish
# "no matches" (benign) from a real failure (fatal).
LIST_STDERR_FILE="$(mktemp)"
trap 'rm -f "$LIST_STDERR_FILE"' EXIT

MATCHES=""
LIST_STATUS=0
SESSIONS_OUTPUT="$(python3 "$SCRIPT_DIR/sd-recall.py" sessions --scope "$SCOPE" --limit "$LIMIT" --agent "$AGENT" 2>"$LIST_STDERR_FILE")" || LIST_STATUS=$?

# Skip the header row; get data rows only
MATCHES="$(echo "$SESSIONS_OUTPUT" | tail -n +2 | head -n "$LIMIT")"

# Check stderr content
STDERR_CONTENT="$(cat "$LIST_STDERR_FILE" 2>/dev/null || echo "")"

# Cross-agent fallback: no matches in current scope, try all environments
if [[ -z "$MATCHES" && "$AGENT" == "auto" && "$SCOPE" != "all" ]]; then
  if [[ "$STDERR_CONTENT" == *"No Claude"* || "$LIST_STATUS" -ne 0 ]]; then
    echo "  (当前项目无会话，尝试跨代理搜索...)"
    SESSIONS_OUTPUT="$(python3 "$SCRIPT_DIR/sd-recall.py" sessions --scope all --limit "$LIMIT" --agent cross 2>/dev/null)" || true
    MATCHES="$(echo "$SESSIONS_OUTPUT" | tail -n +2 | head -n "$LIMIT")"
    if [[ -n "$MATCHES" ]]; then
      echo "  (跨代理搜索命中)"
      echo
    fi
  fi
fi

# Still no matches
if [[ -z "$MATCHES" ]]; then
  if [[ "$STDERR_CONTENT" == *"No Claude"* ]]; then
    echo "No session directory found for current project."
  else
    echo "No matching sessions found for '$QUERY' in scope '$SCOPE'."
  fi
  if [[ "$AGENT" == "auto" && "$SCOPE" != "all" ]]; then
    echo "Hint: try --scope all --agent cross."
  fi
  if [[ "$LIST_STATUS" -ne 0 && "$STDERR_CONTENT" != *"No Claude"* ]]; then
    echo "sd-recall.py error:" >&2
    echo "$STDERR_CONTENT" >&2
    exit 1
  fi
  exit 0
fi

echo "--- Matching sessions (SESSION_ID  CREATED  MODIFIED  MSGS  BRANCH  AGENT  PATH) ---"
echo "$MATCHES"
echo

# Iterate top-N matches and dump evidence per session.
# Note: bash `read` with IFS=$'\t' collapses consecutive tabs because tab is
# whitespace IFS, which corrupts rows where SUMMARY is empty. Swap tabs for a
# non-whitespace delimiter (\x1f, ASCII unit separator) before parsing.
i=0
cached=0
parsed=0
silent_cached=0
while IFS=$'\x1f' read -r session_id created modified msg_count branch agent full_path; do
  [[ -z "${full_path:-}" ]] && continue
  [[ ! -e "$full_path" ]] && continue
  i=$((i + 1))
  echo "============================================================"
  echo "Session $i/$LIMIT"
  echo "  Summary : $agent"
  echo "  Created : $created"
  echo "  Modified: $modified"
  echo "  Agent   : $agent"
  echo "  Messages: $msg_count"
  echo "  Path    : $full_path"
  echo "============================================================"
  echo

  # --- 摘要优先逻辑 ---
  if [[ "$NO_SUMMARY" -eq 0 ]]; then
    SUMMARY_HIT=$(ES_INPUT="$full_path" ES_QUERY="$QUERY" ES_SCRIPT_DIR="$SCRIPT_DIR" \
      python3 << 'PYEOF' 2>/dev/null || echo "MISS"
import os, sys, json
sys.path.insert(0, os.environ["ES_SCRIPT_DIR"])
import echolib

session_path = os.environ["ES_INPUT"]
query = os.environ.get("ES_QUERY", "")

# 检查是否有新鲜摘要
results = echolib.load_analysis_result(session_path, query_intent=query)
if results:
    for rec in results:
        tier = rec.get("memory_tier", "periodic")
        tier_label = {"permanent": "永久", "periodic": "7天", "once": "24h"}.get(tier, tier)
        print("=== [CACHED] 分析意图: %s ===" % rec.get("query_intent", ""))
        print("  分析时间: %s" % rec.get("analyzed_at", ""))
        print("  时效等级: %s (%s)" % (tier, tier_label))
        excluded = rec.get("excluded", [])
        if excluded:
            print("  已否决方向:")
            for ex in excluded:
                print("    - %s" % ex)
        print("  ---")
        print(rec.get("analysis", ""))
        print("---")
    print("HIT")
else:
    print("MISS")
PYEOF
    )

    if [[ "$SUMMARY_HIT" == *"HIT"* ]]; then
      echo "$SUMMARY_HIT" | grep -v "^HIT$"
      cached=$((cached + 1))
      echo
      continue
    fi
  fi

  # --- 慢路径：全量解析（现有逻辑不变）---
  parsed=$((parsed + 1))
  echo "--- User messages (intent) ---"
  PARSED_OUTPUT=$(python3 "$SCRIPT_DIR/sd-recall.py" messages "$full_path" --role user --limit 15 2>/dev/null || echo "(sd-recall messages failed)")
  echo "$PARSED_OUTPUT"
  echo
  echo "--- Tool errors (if any) ---"
  TOOL_OUTPUT=$(python3 "$SCRIPT_DIR/sd-recall.py" tools "$full_path" --errors-only --limit 20 2>/dev/null || echo "(sd-recall tools failed)")
  echo "$TOOL_OUTPUT"
  echo
  if [[ "$DEEP" -eq 1 ]]; then
    echo "--- Full excerpt (both roles, up to 30 messages) ---"
    python3 "$SCRIPT_DIR/sd-recall.py" messages "$full_path" --role both --limit 30 2>/dev/null || \
      echo "(sd-recall messages failed)"
    echo
  fi

  # --- Decision points (--decisions mode) ---
  if [[ "$DECISIONS" -eq 1 ]]; then
    echo "--- Decision points ---"
    ES_FILE="$full_path" python3 << 'PYEOF'
import json, os, sys, re
sys.path.insert(0, os.path.dirname(os.environ.get("ES_SCRIPT_DIR", ".")))
import echolib

DECISION_PATTERNS = [
    r"(?i)\b(decided|decide|deciding)\s+to\b",
    r"(?i)\b(chose|choose|choosing)\s+(to|instead)\b",
    r"(?i)\bgoing\s+to\s+(use|switch|try|migrate)\b",
    r"(?i)\bwill\s+(use|switch|try|migrate|go\s+with)\b",
    r"(?i)\binstead\s+of\b",
    r"(?i)\bswitch(ed|ing)?\s+to\b",
    r"(?i)\buse\s+\w+\s+over\b",
    r"(?i)\bmoving\s+to\b",
    r"(?i)决定",
    r"(?i)选择",
    r"(?i)改用",
    r"(?i)还是",
    r"(?i)换成",
    r"(?i)放弃",
]

path = os.environ["ES_FILE"]
count = 0
for rec in echolib.extract_messages(path, role="both"):
    text = rec["text"]
    if len(text) < 10:
        continue
    for pat in DECISION_PATTERNS:
        if re.search(pat, text):
            ts = rec.get("timestamp", "")[:19]
            role = rec.get("role", "?")
            line = text[:200].replace("\n", " ")
            print(f"  [{ts}] {role}: {line}")
            count += 1
            break
    if count >= 15:
        break

if count == 0:
    print("  (no decision points found)")
PYEOF
    echo
  fi

  # --- 自动缓存：解析完自动存摘要，下次命中 [CACHED] ---
  # 注意：--no-summary 只跳过读取缓存，写入仍然执行（调试时也会存）
  # 用子 shell 隔离 set -e，避免 save-summary 内部非零退出码终止 recall-lite
  if [[ "$PARSED_OUTPUT" != "(sd-recall messages failed)" ]]; then
	    printf '%s\n---\n%s' "$PARSED_OUTPUT" "$TOOL_OUTPUT" | head -c 2000 | \
	      python3 "$SCRIPT_DIR/sd-recall.py" save-summary "$full_path" --stdin --query "$QUERY" --agent auto --tier periodic 2>/dev/null && silent_cached=$((silent_cached + 1))
  fi
done < <(printf '%s\n' "$MATCHES" | tr '\t' $'\x1f')

echo "=== recall-lite done. $i session(s) inspected: $cached cached, $parsed parsed. ==="
if [[ "$parsed" -gt 0 ]]; then
  echo "  (本次解析结果已自动缓存，下次 recall 同主题将命中 [CACHED])"
fi

# CLI history gap hint
HISTORY_COUNT=$(wc -l < ~/.claude/history.jsonl 2>/dev/null | tr -d ' ')
JSONL_COUNT=$(find ~/.claude/projects -name "*.jsonl" -not -path "*/subagents/*" 2>/dev/null | wc -l | tr -d ' ')
if [[ -n "$HISTORY_COUNT" && -n "$JSONL_COUNT" && "$HISTORY_COUNT" -gt 0 ]]; then
  GAP=$((HISTORY_COUNT - JSONL_COUNT))
  if [[ "$GAP" -gt 20 ]]; then
    echo ""
    echo "  注意: CLI history 有 ${HISTORY_COUNT} 条会话，但 JSONL 仅 ${JSONL_COUNT} 个文件，"
    echo "       约 ${GAP} 个会话未被保存（可能被 Claude Code 压缩清理）。"
	    echo "       工具: sd-recall.py schema ~/.claude/history.jsonl 可恢复 prompt 文本。"
  fi
fi
