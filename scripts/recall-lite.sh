#!/usr/bin/env bash
# recall-lite.sh — Local-only session search. Zero API calls.
#
# Pure-shell counterpart to /session-digger:recall. Use this when you cannot or
# do not want to spend a Claude model turn — for example when your session is
# pinned to a billing tier (such as 1M-context Opus with Extra Usage disabled)
# that rejects requests outright, or when you just want fast, deterministic
# raw matches without synthesis.
#
# v0.6: 增加摘要优先逻辑 — 分析过一次的会话直接读摘要，不再重复全量解析。
#
# Usage:
#   recall-lite.sh <keyword> [--scope current|all] [--limit N] [--deep] [--agent claude|grok|kimi_code|cross|auto] [--no-summary]
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
AGENT="auto"
NO_SUMMARY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope) SCOPE="$2"; shift 2 ;;
    --limit) LIMIT="$2"; shift 2 ;;
    --deep)  DEEP=1; shift ;;
    --agent) AGENT="$2"; shift 2 ;;
    --no-summary) NO_SUMMARY=1; shift ;;
    *) echo "ERROR: Unknown option: $1" >&2; exit 1 ;;
  esac
done

if ! [[ "$LIMIT" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --limit must be a number, got: $LIMIT" >&2
  exit 1
fi

if [[ "$SCOPE" != "current" && "$SCOPE" != "all" ]]; then
  echo "ERROR: --scope must be 'current' or 'all', got: $SCOPE" >&2
  exit 1
fi

echo "=== recall-lite: query='$QUERY' scope=$SCOPE limit=$LIMIT agent=$AGENT ==="
if [[ "$NO_SUMMARY" -eq 0 ]]; then
  echo "  (摘要优先：已分析的会话直接读缓存，--no-summary 可跳过)"
fi
echo

# list-sessions.sh exits 1 when no entries match, even if sessions exist for
# the project — that's a known quirk. Capture stderr so we can distinguish
# "no matches" (benign) from a real failure (fatal).
LIST_STDERR_FILE="$(mktemp)"
trap 'rm -f "$LIST_STDERR_FILE"' EXIT

if MATCHES="$("$SCRIPT_DIR/list-sessions.sh" "$SCOPE" --grep "$QUERY" --limit "$LIMIT" --agent "$AGENT" 2>"$LIST_STDERR_FILE")"; then
  : # success path; MATCHES populated
else
  STATUS=$?
  STDERR_CONTENT="$(cat "$LIST_STDERR_FILE")"
  # The "no session directory found" error fires both when truly no sessions
  # exist AND when grep filtered everything out. Treat it as "no matches".
  if [[ "$STDERR_CONTENT" == *"No Claude session directory found"* ]]; then
    echo "No matching sessions found for '$QUERY' in scope '$SCOPE'."
    if [[ "$SCOPE" == "current" ]]; then
      echo "Hint: try --scope all to search every project."
    fi
    exit 0
  fi
  echo "list-sessions.sh failed (status $STATUS):" >&2
  echo "$STDERR_CONTENT" >&2
  exit 1
fi

if [[ -z "$MATCHES" ]]; then
  echo "No matching sessions found for '$QUERY' in scope '$SCOPE'."
  exit 0
fi

echo "--- Matching sessions (SESSION_ID  CREATED  MODIFIED  MSG_COUNT  BRANCH  SUMMARY  FIRST_PROMPT  PROJECT_PATH  FULL_PATH) ---"
echo "$MATCHES"
echo

# Iterate top-N matches and dump evidence per session.
# Note: bash `read` with IFS=$'\t' collapses consecutive tabs because tab is
# whitespace IFS, which corrupts rows where SUMMARY is empty. Swap tabs for a
# non-whitespace delimiter (\x1f, ASCII unit separator) before parsing.
i=0
cached=0
parsed=0
while IFS=$'\x1f' read -r session_id created modified msg_count branch summary first_prompt project_path full_path; do
  [[ -z "${full_path:-}" ]] && continue
  [[ ! -e "$full_path" ]] && continue
  i=$((i + 1))
  echo "============================================================"
  echo "Session $i/$LIMIT"
  echo "  Summary : $summary"
  echo "  Created : $created"
  echo "  Modified: $modified"
  echo "  Branch  : $branch"
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
  "$SCRIPT_DIR/extract-messages.sh" "$full_path" --role user --limit 15 2>/dev/null \
    || echo "(extract-messages failed)"
  echo
  echo "--- Tool errors (if any) ---"
  "$SCRIPT_DIR/extract-tools.sh" "$full_path" --errors-only --limit 20 2>/dev/null \
    || echo "(extract-tools failed)"
  echo
  if [[ "$DEEP" -eq 1 ]]; then
    echo "--- Full excerpt (both roles, up to 30 messages) ---"
    "$SCRIPT_DIR/extract-messages.sh" "$full_path" --role both --limit 30 2>/dev/null || \
      echo "(extract-messages failed)"
    echo
  fi
done < <(printf '%s\n' "$MATCHES" | tr '\t' $'\x1f')

echo "=== recall-lite done. $i session(s) inspected: $cached cached, $parsed parsed. ==="
if [[ "$parsed" -gt 0 ]]; then
  echo "提示: 使用 save-summary.sh <session_path> \"分析结果\" \"$QUERY\" 可保存分析结果供下次复用"
fi
