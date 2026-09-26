#!/usr/bin/env bash
# apply-rules.sh — Format candidate rules for interactive review and write to targets.
# Helper for /apply command. Zero API calls.
#
# Usage:
#   apply-rules.sh format <session.jsonl>           # Output numbered candidate rules (text)
#   apply-rules.sh dump-json <session.jsonl>        # Output suggestions as JSON array
#   apply-rules.sh append <rule_text> [--target FILE]  # Append rule to target file
#   apply-rules.sh memory <rule_text> [category] [--project PATH]  # Write to memory/
#
# v1.0: Inspired by session-recall --apply interactive review flow.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export ES_SCRIPT_DIR="$SCRIPT_DIR"

cmd="${1:-}"
shift || true

case "$cmd" in
  format)
    session="${1:-}"
    [[ -z "$session" ]] && { echo "ERROR: Missing session file" >&2; exit 1; }
    [[ ! -f "$session" ]] && { echo "ERROR: File not found: $session" >&2; exit 1; }
    ES_FILE="$session" python3 << 'PYEOF'
import json, os, sys
sys.path.insert(0, os.environ["ES_SCRIPT_DIR"])
import echolib
from collections import Counter

file_path = os.environ["ES_FILE"]
fmt = os.environ.get("ANALYZE_FORMAT", "text")

stats = echolib.session_stats(file_path)

# Retry detection
tool_sequences = []
for t in echolib.extract_tools(file_path):
    tool_sequences.append((t["name"], t["key_input"], t["status"], t["timestamp"]))

import re
retry_patterns = []
i = 0
while i < len(tool_sequences):
    name, key, status, ts = tool_sequences[i]
    if status == "error":
        run_len = 1
        j = i + 1
        while j < len(tool_sequences) and tool_sequences[j][0] == name and tool_sequences[j][2] == "error":
            run_len += 1
            j += 1
        if run_len >= 2:
            retry_patterns.append({"tool": name, "count": run_len, "sample_input": key[:120], "timestamp": ts})
        i = j
    else:
        i += 1

# Rules
suggestions = []
for rp in retry_patterns:
    suggestions.append({
        "rule": "When {} fails {} times in a row, switch approach. Do not keep retrying the same command.".format(rp["tool"], rp["count"]),
        "category": "retry",
        "evidence": "Retried {}x at {}: {}".format(rp["count"], rp["timestamp"], rp["sample_input"][:80]),
    })

# User corrections (simplified sample)
# 这里的输出经 /apply 落进 CLAUDE.md，误报直接变成持久化规则：
# 裸「别」匹配别人/别墅/级别，裸「应该/重新」在问句里是提问不是纠正。
correction_patterns = [
    r"(?i)\bdon't\b", r"(?i)\bstop\b", r"(?i)\bwrong\b", r"(?i)\bactually\b",
    r"(?i)不(对|行|是)", r"(?i)别(这样|用|搞|再|改|乱|弄)", r"(?i)其实", r"(?i)应该", r"(?i)重新",
]
_weak = {"(?i)其实", "(?i)应该", "(?i)重新"}


def _is_question(t):
    return "？" in t or t.rstrip().endswith(("?", "吗", "么"))


seen = set()
for rec in echolib.extract_messages(file_path, role="user"):
    text = rec["text"]
    if len(text) < 20 or len(text) > 200:
        continue
    if _is_question(text) and not any(re.search(p, text) for p in correction_patterns if p not in _weak):
        continue
    for pat in correction_patterns:
        if re.search(pat, text):
            key = text[:40]
            if key not in seen:
                seen.add(key)
                suggestions.append({
                    "rule": text.strip(),
                    "category": "correction",
                    "evidence": "User message at {}".format(rec["timestamp"][:19]),
                })
            break

# Output
print("CANDIDATE RULES FROM: {}\n".format(file_path))
for idx, s in enumerate(suggestions, 1):
    print("  ({}) [{}] {}".format(idx, s["category"], s["rule"]))
    print("      Evidence: {}".format(s["evidence"]))
    print()

if not suggestions:
    print("  No patterns detected.")
PYEOF
    ;;

  dump-json)
    session="${1:-}"
    [[ -z "$session" ]] && { echo "ERROR: Missing session file" >&2; exit 1; }
    ES_FILE="$session" ES_SCRIPT_DIR="$SCRIPT_DIR" python3 << 'PYEOF'
import json, os, sys, re
sys.path.insert(0, os.environ["ES_SCRIPT_DIR"])
import echolib

file_path = os.environ["ES_FILE"]
tool_sequences = [(t["name"], t["key_input"], t["status"], t["timestamp"])
                  for t in echolib.extract_tools(file_path)]

retry_patterns = []
i = 0
while i < len(tool_sequences):
    name, key, status, ts = tool_sequences[i]
    if status == "error":
        run_len = 1
        j = i + 1
        while j < len(tool_sequences) and tool_sequences[j][0] == name and tool_sequences[j][2] == "error":
            run_len += 1
            j += 1
        if run_len >= 2:
            retry_patterns.append({"tool": name, "count": run_len, "sample_input": key[:120], "timestamp": ts})
        i = j
    else:
        i += 1

suggestions = []
for rp in retry_patterns:
    suggestions.append({
        "rule": "When {} fails {} times in a row, switch approach. Do not keep retrying.".format(rp["tool"], rp["count"]),
        "category": "retry",
        "evidence": "Retried {}x at {}".format(rp["count"], rp["timestamp"]),
        "target": "CLAUDE.md",
    })

correction_patterns = [r"(?i)\bdon't\b", r"(?i)\bstop\b", r"(?i)\bwrong\b",
                       r"(?i)\bactually\b", r"(?i)不对", r"(?i)别", r"(?i)应该"]
seen = set()
for rec in echolib.extract_messages(file_path, role="user"):
    text = rec["text"]
    if len(text) < 20 or len(text) > 200:
        continue
    for pat in correction_patterns:
        if re.search(pat, text):
            key = text[:40]
            if key not in seen:
                seen.add(key)
                suggestions.append({
                    "rule": text.strip(),
                    "category": "correction",
                    "evidence": "User message at {}".format(rec["timestamp"][:19]),
                    "target": "CLAUDE.md",
                })
            break

print(json.dumps(suggestions, ensure_ascii=False, indent=2))
PYEOF
    ;;

  append)
    rule="${1:-}"
    [[ -z "$rule" ]] && { echo "ERROR: Missing rule text" >&2; exit 1; }
    target="${2:-CLAUDE.md}"
    [[ "$target" == "--target" ]] && { target="${3:-CLAUDE.md}"; }

    # Create if missing
    if [[ ! -f "$target" ]]; then
      echo "# Project Rules" > "$target"
      echo "" >> "$target"
    fi

    # Check if section exists
    if ! grep -q "## Session Learned Rules" "$target" 2>/dev/null; then
      echo "" >> "$target"
      echo "## Session Learned Rules" >> "$target"
      echo "" >> "$target"
    fi

    # Append rule at the END of the `## Session Learned Rules` section.
    # 旧逻辑锚在「最后一行 session-digger:apply 标记」上，标记缺失（手写的
    # 小节、老版本写过的文件）就整段退到 EOF 追加——规则落进别的小节；
    # 且 splitlines()+'\n' 重组会把 CRLF 文件整篇改写成 LF。这里统一锚定
    # 在小节标题上：插在下一个 ## 标题之前，标记缺失照常补，换行符保持现状。
    timestamp="$(date +%Y-%m-%d)"
    rule_line="- $rule"
    # fallback 块不能和 heredoc 写在同一行（|| { 会被 heredoc 正文吞掉），
    # 用 if ! 结构让 bash 先读完 heredoc 再解析 then。
    if ! python3 - "$target" "$rule_line" "$timestamp" <<'PYRULE'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
rule = sys.argv[2]
marker = f"<!-- session-digger:apply {sys.argv[3]} -->"
# newline=''：read_text 的默认通用换行翻译会在**读取时**就把 \r\n 折成 \n，
# CRLF 文件随后被整篇改写。newline='' 保留原始字节里的换行形态。
with open(p, 'r', encoding='utf-8', newline='') as f:
    text = f.read()
nl = '\r\n' if '\r\n' in text else '\n'
lines = text.split(nl)
sec = next((i for i, l in enumerate(lines) if l.strip() == '## Session Learned Rules'), None)
if sec is None:
    if lines and lines[-1].strip():
        lines.append('')
    lines += ['## Session Learned Rules', '', marker, rule]
else:
    end = next((i for i in range(sec + 1, len(lines)) if lines[i].startswith('## ')), len(lines))
    while end > sec + 1 and lines[end - 1].strip() == '':
        end -= 1
    lines[end:end] = [marker, rule]
with open(p, 'w', encoding='utf-8', newline='') as f:
    f.write(nl.join(lines))
PYRULE
    then
        # Fallback: simple append to file (placement NOT inside the section)
        echo "WARN: structured insert failed; appended at EOF" >&2
        echo "$rule_line" >> "$target"
    fi

    echo "OK: appended rule to $target"
    ;;

  memory)
    rule="${1:-}"
    [[ -z "$rule" ]] && { echo "ERROR: Missing rule text" >&2; exit 1; }

    # 先摘走 --project <path>，剩下的第一个位置参数才是 category——
    # 否则 `memory "教训" --project ~/foo` 会把 frontmatter 写成 type: --project。
    project="${PWD}"
    rest=()
    while [[ $# -gt 1 ]]; do
      case "$2" in
        --project)
          if [[ $# -ge 3 ]]; then project="$3"; shift 3; else shift; fi
          ;;
        *)
          rest+=("$2")
          shift
          ;;
      esac
    done
    category="${rest[0]:-feedback}"

    mem_dir="$([[ -d "$project/memory" ]] && echo "$project/memory" || echo "$project/.claude/memory")"
    mkdir -p "$mem_dir"

    date_tag="$(date +%Y%m%d-%H%M%S)"
    out_file="$mem_dir/lessons-${date_tag}.md"
    # 同一秒写两条会话教训：截断式 cat > 会让第一条静默丢失，加序号去重。
    _seq=1
    while [[ -e "$out_file" ]]; do
      out_file="$mem_dir/lessons-${date_tag}-$((_seq++)).md"
    done

    cat > "$out_file" << MDEOF
---
name: "Session lesson ${date_tag}"
description: "Auto-extracted rule from session analysis"
type: ${category}
---

- ${rule}
MDEOF

    # Update MEMORY.md
    # Was "$mem_dir/MORY.md" with a MEMORY.md fallback: the typo never showed
    # because MORY.md essentially never exists, so the fallback always won. It
    # is still a live footgun — a stray MORY.md would silently become the file
    # that gets the link appended.
    memo="$mem_dir/MEMORY.md"
    if [[ -f "$memo" ]]; then
      if ! grep -q "lessons-${date_tag}" "$memo" 2>/dev/null; then
        echo "![[lessons-${date_tag}.md]]" >> "$memo"
      fi
    fi

    echo "OK: written to $out_file"
    ;;

  *)
    echo "Usage: apply-rules.sh <format|dump-json|append|memory> [args]" >&2
    exit 1
    ;;
esac
