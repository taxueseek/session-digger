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

    timestamp="$(date +%Y-%m-%d)"
    rule_line="- $rule"
    # Append rule at the END of the `## Session Learned Rules` section.
    # 旧逻辑锚在「最后一行 session-digger:apply 标记」上，标记缺失（手写的
    # 小节、老版本写过的文件）就整段退到 EOF 追加——规则落进别的小节；
    # 且 splitlines()+'\n' 重组会把 CRLF 文件整篇改写成 LF。这里统一锚定
    # 在小节标题上：插在下一个 ## 标题之前，标记缺失照常补，换行符保持现状。
    #
    # 并发：读-改-写整段在一个 flock 临界区里完成，文件与小节的首次创建
    # 也在锁内。旧结构是 shell 先 grep+echo 建节、python 再读改写，两段
    # 之间没有锁——并发 append 在小节缺失时各插一个标题，在章节存在时是
    # 后写者覆盖前写者的丢更新。锁文件 <target>.lock 常驻（flock 的标准
    # 做法；unlink 锁文件本身有竞态，不做）。
    # newline=''：read_text 的默认通用换行翻译会在**读取时**就把 \r\n 折成 \n，
    # CRLF 文件随后被整篇改写。newline='' 保留原始字节里的换行形态。
    # fallback 块不能和 heredoc 写在同一行（|| { 会被 heredoc 正文吞掉），
    # 用 if ! 结构让 bash 先读完 heredoc 再解析 then。
    if ! python3 - "$target" "$rule_line" "$timestamp" <<'PYRULE'
import fcntl, sys, pathlib
p = pathlib.Path(sys.argv[1])
rule = sys.argv[2]
marker = f"<!-- session-digger:apply {sys.argv[3]} -->"
# 锁文件用隐藏名：它会常驻（flock 的标准做法，unlink 锁文件本身有竞态），
# 但不该在用户的仓库里以一个显眼的 CLAUDE.md.lock 出现。
with open(str(p.parent / ("." + p.name + ".lock")), "w") as lf:
    fcntl.flock(lf, fcntl.LOCK_EX)
    if p.exists():
        with open(p, 'r', encoding='utf-8', newline='') as f:
            text = f.read()
        nl = '\r\n' if '\r\n' in text else '\n'
        had_trailing_nl = text.endswith(nl)
        lines = text.split(nl)
    else:
        nl = '\n'
        had_trailing_nl = False
        lines = ['# Project Rules', '']
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
    if had_trailing_nl and lines[-1] != '':
        lines.append('')
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

    # 并发：序号选取、教训文件与 MEMORY.md 追加原本是三段无锁的
    # check-then-act——同一秒的两个并发写各自选中同一个文件名，后写者的
    # 截断式 cat > 静默吃掉前写者。整段收进一个 flock 临界区：fcntl 由
    # 内核保证进程退出即释放（SIGKILL 也不例外），没有陈锁需要清理。
    # 规则文本经 argv 进入 python，不再经 heredoc 插值——带引号/反引号/$
    # 的规则不再有篡改 MD 正文的机会。
    if ! python3 - "$mem_dir" "$rule" "${rest[0]:-feedback}" <<'PYMEM'
import fcntl, sys
from datetime import datetime
from pathlib import Path

mem_dir = Path(sys.argv[1])
rule = sys.argv[2]
category = sys.argv[3] or "feedback"

with open(mem_dir / ".lessons.lock", "w") as lf:
    fcntl.flock(lf, fcntl.LOCK_EX)
    date_tag = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_file = mem_dir / f"lessons-{date_tag}.md"
    seq = 1
    while out_file.exists():
        out_file = mem_dir / f"lessons-{date_tag}-{seq}.md"
        seq += 1
    out_file.write_text(
        f'---\nname: "Session lesson {date_tag}"\n'
        f'description: "Auto-extracted rule from session analysis"\n'
        f'type: {category}\n---\n\n- {rule}\n',
        encoding="utf-8",
    )
    memo = mem_dir / "MEMORY.md"
    if memo.exists():
        with open(memo, "r", encoding="utf-8") as f:
            links = f.read()
        if out_file.stem not in links:
            with open(memo, "a", encoding="utf-8") as f:
                f.write(f"![[{out_file.name}]]\n")
print(f"OK: written to {out_file}")
PYMEM
    then
        echo "ERROR: memory write failed" >&2
        exit 1
    fi
    ;;

  -h|--help)
    echo "Usage: apply-rules.sh <format|dump-json|append|memory> [args]"
    echo "  format <session.jsonl>                       Extract rules from a session"
    echo "  dump-json <session.jsonl>                    Dump analysis as JSON"
    echo "  append <rule> [--target <file.md>]           Append rule to the learned-rules section (default: ./CLAUDE.md)"
    echo "  memory <rule> [--project <path>]             Write a session lesson to the memory dir"
    exit 0
    ;;

  *)
    echo "Usage: apply-rules.sh <format|dump-json|append|memory> [args]" >&2
    exit 1
    ;;
esac
