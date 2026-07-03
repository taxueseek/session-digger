#!/usr/bin/env bash
# analyze-session.sh — Pattern analysis: retry loops, errors, user corrections.
# Produces candidate rules for CLAUDE.md. Zero API calls.
#
# Usage: analyze-session.sh <session.jsonl> [--format text|json]
#        analyze-session.sh --all [--limit N] [--project PATH]
#
# Output sections:
#   RETRY PATTERNS    — Tools retried 3+ times with similar input
#   ERROR SUMMARY      — Total errors, breakdown by tool
#   USER CORRECTIONS   — Imperative rejections (don't, stop, wrong, actually)
#   CANDIDATE RULES    — Concrete suggestions ready for /apply
#
# v1.0: Inspired by session-recall --report + echo-sleuth /lessons

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

FORMAT="text"
ALL_MODE=0
LIMIT=10
PROJECT=""
SESSION_FILE=""

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --format) FORMAT="$2"; shift 2 ;;
    --all) ALL_MODE=1; shift ;;
    --limit) LIMIT="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    -*)
      echo "ERROR: Unknown option: $1" >&2
      echo "Usage: analyze-session.sh <file.jsonl>|--all [--limit N] [--format text|json]" >&2
      exit 1
      ;;
    *)
      SESSION_FILE="$1"
      shift
      ;;
  esac
done

export ES_SCRIPT_DIR="$SCRIPT_DIR"

# ---- Single-session analysis ----
analyze_single() {
  local file="$1"
  ES_FILE="$file" python3 << 'PYEOF'
import json, os, sys, re
from collections import Counter, defaultdict
sys.path.insert(0, os.environ["ES_SCRIPT_DIR"])
import echolib

file_path = os.environ["ES_FILE"]
fmt = os.environ.get("ANALYZE_FORMAT", "text")

stats = echolib.session_stats(file_path)

# --- Pass 1: Collect tool call sequences (for retry detection) ---
tool_sequences = []  # (name, key_input, is_error, timestamp)
tool_errors_by_name = Counter()
total_errors = 0

for t in echolib.extract_tools(file_path):
    tool_sequences.append((t["name"], t["key_input"], t["status"], t["timestamp"]))
    if t["status"] == "error":
        total_errors += 1
        tool_errors_by_name[t["name"]] += 1

# --- Retry detection: consecutive same-tool-error sequences ---
retry_patterns = []
i = 0
while i < len(tool_sequences):
    name, key, status, ts = tool_sequences[i]
    if status == "error":
        run_len = 1
        j = i + 1
        while j < len(tool_sequences) and tool_sequences[j][0] == name and tool_sequences[j][2] == "error":
            # Check if key_inputs are similar (same tool, overlapping)
            run_len += 1
            j += 1
        if run_len >= 3:
            retry_patterns.append({
                "tool": name,
                "count": run_len,
                "sample_input": key[:120],
                "timestamp": ts,
            })
        i = j
    else:
        i += 1

# --- Pass 2: User corrections ---
correction_patterns = [
    r"(?i)\bno[,，]\s*(that'?s|that is|wrong|incorrect|not right)",
    r"(?i)\b(don't|stop|quit)\s+(do|use|try|run|that)",
    r"(?i)\bactually[,，]",
    r"(?i)\bthat'?s\s+(wrong|incorrect|not\s+what)",
    r"(?i)\bnot\s+(quite|exactly|right)",
    r"(?i)\byou'?re\s+(wrong|incorrect|mistaken)",
    r"(?i)\b(try|use|do)\s+(again|differently|this\s+way|instead)",
    r"(?i)\bes\s+(incorrecto?|equivocado?)",
    r"(?i)不(对|行|可以|要|是)",
    r"(?i)别(这样|用|搞)",
    r"(?i)其实",
    r"(?i)应该",
    r"(?i)重新",
]

corrections = []
for rec in echolib.extract_messages(file_path, role="user"):
    text = rec["text"]
    # Skip very short messages (likely just tool results or confirmations)
    if len(text) < 20:
        continue
    for pat in correction_patterns:
        if re.search(pat, text):
            corrections.append({
                "timestamp": rec["timestamp"],
                "text": text[:200],
            })
            break

# --- Deduplicate corrections by similarity (simple prefix match) ---
seen_prefixes = []
deduped_corrections = []
for c in corrections:
    prefix = c["text"][:50]
    is_dup = False
    for sp in seen_prefixes:
        if prefix.startswith(sp) or sp.startswith(prefix):
            is_dup = True
            break
    if not is_dup:
        seen_prefixes.append(prefix)
        deduped_corrections.append(c)
corrections = deduped_corrections

# --- Generate candidate rules ---
suggestions = []
for rp in retry_patterns:
    suggestions.append({
        "rule": f"When {rp['tool']} fails {rp['count']} times in a row, switch approach. Do not keep retrying the same command.",
        "evidence": f"Retried {rp['count']}x at {rp['timestamp']}: {rp['sample_input']}",
        "target": "CLAUDE.md",
        "category": "retry",
    })

for c in corrections[:10]:  # Cap at 10
    # Extract the correction as a rule
    rule_text = c["text"].strip()
    if len(rule_text) > 150:
        rule_text = rule_text[:150] + "..."
    suggestions.append({
        "rule": rule_text,
        "evidence": f"User correction at {c['timestamp']}",
        "target": "CLAUDE.md",
        "category": "correction",
    })

# --- Output ---
if fmt == "json":
    print(json.dumps({
        "file": file_path,
        "stats": stats,
        "retry_patterns": retry_patterns,
        "total_errors": total_errors,
        "errors_by_tool": dict(tool_errors_by_name),
        "corrections": corrections,
        "suggestions": suggestions,
    }, ensure_ascii=False, indent=2))
    sys.exit(0)

# Text format
print("=" * 60)
print("SESSION ANALYSIS")
print(f"  File  : {file_path}")
print(f"  Branch: {stats.get('branch', 'n/a')}")
print(f"  Model : {stats.get('model', 'n/a')}")
print(f"  Range : {stats.get('started', '?')[:10]} .. {stats.get('ended', '?')[:10]}")
print("=" * 60)
print()

tool_calls = stats.get("tool_calls", 0)
print(f"OVERVIEW")
print(f"  User messages  : {stats.get('user_messages', 0)}")
print(f"  Tool calls     : {tool_calls}")
print(f"  Errors         : {total_errors}")
if tool_calls > 0:
    err_pct = round(100 * total_errors / max(tool_calls, 1))
    print(f"  Error rate     : {err_pct}%")
print(f"  Compactions    : {stats.get('compactions', 0)}")
print(f"  Tokens (in/out): {stats.get('input_tokens', 0)} / {stats.get('output_tokens', 0)}")
print()

if retry_patterns:
    print("RETRY PATTERNS")
    for rp in retry_patterns:
        print(f"  [{rp['tool']}] failed {rp['count']}x — {rp['sample_input'][:80]}")
    print()

if tool_errors_by_name:
    print("ERRORS BY TOOL")
    for name, count in tool_errors_by_name.most_common():
        print(f"  {name}: {count}")
    print()

if corrections:
    print(f"USER CORRECTIONS ({len(corrections)})")
    for c in corrections[:5]:
        print(f"  [{c['timestamp'][:19]}] {c['text'][:100]}")
    if len(corrections) > 5:
        print(f"  ... and {len(corrections) - 5} more")
    print()

if suggestions:
    print(f"CANDIDATE RULES ({len(suggestions)})")
    for idx, s in enumerate(suggestions, 1):
        print(f"  ({idx}) [{s['category']}] {s['rule']}")
        print(f"      Evidence: {s['evidence'][:100]}")
    print()
    print("Run /apply to review and persist these rules.")
else:
    print("No significant patterns detected.")
PYEOF
}

# ---- Cross-session analysis ----
analyze_all() {
  local scope="${PROJECT:-all}"
  local tmp_list
  tmp_list="$(mktemp)"

	  if [[ -n "$PROJECT" ]]; then
	    python3 "$SCRIPT_DIR/sd-recall.py" sessions --scope current --limit "$LIMIT" 2>/dev/null | tail -n +2 > "$tmp_list" || true
	  else
	    python3 "$SCRIPT_DIR/sd-recall.py" sessions --scope all --limit "$LIMIT" 2>/dev/null | tail -n +2 > "$tmp_list" || true
	  fi

	  if [[ ! -s "$tmp_list" ]]; then
	    echo "No sessions found for analysis."
	    rm -f "$tmp_list"
	    return
	  fi

	  echo "=== Cross-session analysis (last $LIMIT sessions) ==="
	  echo

	  # Use unit separator for safe parsing
	  total_errors=0
	  total_retries=0
	  total_tool_calls=0
	  declare -A tool_error_counts
	  declare -A retry_tool_counts
	  all_suggestions=0

	  # sd-recall.py sessions output: SESSION_ID CREATED MODIFIED MSGS BRANCH AGENT PATH
	  while IFS=$'\x1f' read -r session_id created modified msg_count branch agent full_path; do
    [[ -z "${full_path:-}" ]] && continue
    [[ ! -e "$full_path" ]] && continue

    result="$(ANALYZE_FORMAT=json ES_FILE="$full_path" analyze_single "$full_path" --format json 2>/dev/null)" || continue

    # Extract key metrics using Python (reliable JSON parsing)
    metrics="$(echo "$result" | python3 -c "
import json, sys
d = json.load(sys.stdin)
e = d.get('total_errors', 0)
r = len(d.get('retry_patterns', []))
t = float(d.get('stats', {}).get('tool_calls', 0))
s = len(d.get('suggestions', []))
# Count retry tools
rtools = Counter(p['tool'] for p in d.get('retry_patterns', []))
tools_by_err = d.get('errors_by_tool', {})
print(f'{e}\t{r}\t{t}\t{s}\t{json.dumps(dict(rtools))}\t{json.dumps(tools_by_err)}')
")"

    IFS=$'\t' read -r errors retries tool_count sug_count rtools_json tools_err_json <<< "$metrics"

    total_errors=$((total_errors + errors))
    total_retries=$((total_retries + retries))
    total_tool_calls=$((total_tool_calls + tool_count))
    all_suggestions=$((all_suggestions + sug_count))

    # Merge tool error counts
    while IFS=':' read -r tname tcount; do
      [[ -z "$tname" ]] && continue
      tname="$(echo "$tname" | tr -d '{}\" ')"
      tcount="$(echo "$tcount" | tr -d '} ')"
      [[ -z "$tname" || "$tname" == "None" ]] && continue
      tool_error_counts["$tname"]=$(( ${tool_error_counts["$tname"]:-0} + tcount ))
    done < <(echo "$tools_err_json" | python3 -c "
import json, sys
d = json.load(sys.stdin)
for k, v in d.items(): print(f'{k}:{v}')
" 2>/dev/null)

  done < <(tr '\t' $'\x1f' < "$tmp_list")
  rm -f "$tmp_list"

  echo "AGGREGATE (last $LIMIT sessions)"
  echo "  Total tool calls : $total_tool_calls"
  echo "  Total errors    : $total_errors"
  if [[ "$total_tool_calls" -gt 0 ]]; then
    echo "  Error rate      : $(( 100 * total_errors / total_tool_calls ))%"
  fi
  echo "  Retry patterns  : $total_retries"
  echo "  Candidates      : $all_suggestions"
  echo

  if [[ ${#tool_error_counts[@]} -gt 0 ]]; then
    echo "ERRORS BY TOOL (cross-session)"
    for tname in "${!tool_error_counts[@]}"; do
      echo "  $tname: ${tool_error_counts[$tname]}"
    done | sort -t: -k2 -rn
    echo
  fi

  echo "Tip: Run analyze-session.sh <file> for detailed per-session rules."
}

# ---- Main ----
if [[ "$ALL_MODE" -eq 1 ]]; then
  analyze_all
elif [[ -n "$SESSION_FILE" ]]; then
  if [[ ! -f "$SESSION_FILE" ]]; then
    echo "ERROR: File not found: $SESSION_FILE" >&2
    exit 1
  fi
  analyze_single "$SESSION_FILE"
else
  echo "Usage: analyze-session.sh <file.jsonl>|--all [--limit N] [--format text|json]" >&2
  exit 1
fi
