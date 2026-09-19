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
# v0.8: 逐会话证据合并为单进程（sd-recall.py lite-report），不再每会话 spawn 1-6 个 Python。
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
    echo "  (当前项目无会话，尝试跨代理搜索...)" >&2
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
    echo "No matching sessions found for '$QUERY' in scope '$SCOPE'." >&2
  fi
  if [[ "$AGENT" == "auto" && "$SCOPE" != "all" ]]; then
    echo "Hint: try --scope all --agent cross." >&2
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

# Per-session evidence: one python process (sd-recall.py lite-report).
# The old bash loop spawned 1 interpreter per session just for the summary
# cache lookup and up to 5 more per cache miss (messages/tools/deep/
# decisions/save-summary), each re-importing echolib. lite-report streams the
# rows and renders the exact same blocks in a single process.
LITE_FLAGS=()
[[ "$DEEP" -eq 1 ]] && LITE_FLAGS+=(--deep)
[[ "$DECISIONS" -eq 1 ]] && LITE_FLAGS+=(--decisions)
[[ "$NO_SUMMARY" -eq 1 ]] && LITE_FLAGS+=(--no-summary)
printf '%s\n' "$MATCHES" | python3 "$SCRIPT_DIR/sd-recall.py" lite-report \
  --query "$QUERY" --limit "$LIMIT" ${LITE_FLAGS[@]+"${LITE_FLAGS[@]}"}

# CLI history gap hint
HISTORY_COUNT=$(wc -l < ~/.claude/history.jsonl 2>/dev/null | tr -d ' ')
JSONL_COUNT=$(find ~/.claude/projects -name "*.jsonl" -not -path "*/subagents/*" 2>/dev/null | wc -l | tr -d ' ')
if [[ -n "$HISTORY_COUNT" && -n "$JSONL_COUNT" && "$HISTORY_COUNT" -gt 0 ]]; then
  GAP=$((HISTORY_COUNT - JSONL_COUNT))
  if [[ "$GAP" -gt 20 ]]; then
    echo ""
    echo "  注意: CLI history 有 ${HISTORY_COUNT} 条会话，但 JSONL 仅 ${JSONL_COUNT} 个文件，" >&2
    echo "       约 ${GAP} 个会话未被保存（可能被 Claude Code 压缩清理）。" >&2
	    echo "       工具: sd-recall.py schema ~/.claude/history.jsonl 可恢复 prompt 文本。" >&2
  fi
fi
