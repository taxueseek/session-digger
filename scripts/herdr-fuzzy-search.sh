#!/usr/bin/env bash
# herdr-fuzzy-search.sh — Fuzzy browse of indexed sessions (cross-agent)
#
# Uses sd-recall sessions TSV (includes PATH) so Grok/Codex/ZCode/… work,
# not only ~/.claude/projects. Display never prints absolute paths.
#
# Env: HERDR_PLUGIN_ROOT, SESSION_DIGGER_ROOT
# Optional: $1 keyword for non-interactive fzf --filter

set -euo pipefail

PLUGIN_ROOT="${HERDR_PLUGIN_ROOT:-${SESSION_DIGGER_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}}"
RECALL="$PLUGIN_ROOT/scripts/sd-recall.py"

if [[ ! -f "$RECALL" ]]; then
    echo "ERROR: sd-recall.py not found (set HERDR_PLUGIN_ROOT or SESSION_DIGGER_ROOT)" >&2
    exit 1
fi

if ! command -v fzf &>/dev/null; then
    echo "fzf not found; falling back to list mode." >&2
    echo "Install: brew install fzf | apt install fzf" >&2
    python3 "$RECALL" sessions --scope all --limit 30
    exit 0
fi

# Columns: display fields + hidden full path (for messages lookup)
# sid | agent | created | msgs | path
format_candidates() {
    python3 - "$RECALL" <<'PYEOF'
import subprocess, sys

recall = sys.argv[1]
result = subprocess.run(
    [sys.executable, recall, "sessions", "--scope", "all", "--limit", "200"],
    capture_output=True, text=True
)
for line in result.stdout.strip().split("\n"):
    parts = line.split("\t")
    if len(parts) < 6 or parts[0] == "SESSION_ID":
        continue
    sid = parts[0]
    created = (parts[1] or "?")[:10]
    msgs = parts[3] if len(parts) > 3 else "?"
    agent = parts[5] if len(parts) > 5 else "?"
    # PATH is last column when present (sd-recall TSV)
    path = parts[-1] if len(parts) >= 7 else ""
    # short id for display; keep full sid in field 0 for search
    print(f"{sid}\t{agent}\t{created}\t{msgs} msgs\t{path}")
PYEOF
}

export -f format_candidates
export RECALL

show_session() {
    local session_id="$1"
    local path="${2:-}"
    echo "=== 会话详情: ${session_id:0:36} ==="
    echo ""
    python3 "$RECALL" search "$session_id" --scope all --limit 1 2>/dev/null || true
    echo ""
    echo "--- 消息预览 ---"
    if [[ -n "$path" && -e "$path" ]]; then
        python3 "$RECALL" messages "$path" --limit 50
    else
        # Cross-agent fallback: resolve path from sessions list without scanning home trees
        python3 - "$RECALL" "$session_id" <<'PYEOF'
import subprocess, sys

recall, sid = sys.argv[1], sys.argv[2]
result = subprocess.run(
    [sys.executable, recall, "sessions", "--scope", "all", "--limit", "500"],
    capture_output=True, text=True,
)
path = None
for line in result.stdout.splitlines():
    parts = line.split("\t")
    if not parts or parts[0] == "SESSION_ID":
        continue
    # match full id or prefix (fzf may truncate display elsewhere)
    if parts[0] == sid or parts[0].startswith(sid[:12]) or sid.startswith(parts[0][:12]):
        if len(parts) >= 7:
            path = parts[-1]
            break
if path:
    subprocess.run([sys.executable, recall, "messages", path, "--limit", "50"])
else:
    print(f"(未在索引中解析到会话路径: {sid[:36]})")
PYEOF
    fi
}

KEYWORD="${1:-}"

if [[ -n "$KEYWORD" ]]; then
    echo "搜索: $KEYWORD"
    echo "---"
    # Filter display cols only; do not print path column
    format_candidates 2>/dev/null | fzf --filter "$KEYWORD" --no-sort 2>/dev/null \
        | awk -F'\t' '{printf "%s  %s  %s  %s\n", substr($1,1,12), $2, $3, $4}' || true
elif [[ -t 0 && -t 1 ]]; then
    selected=$(
        format_candidates 2>/dev/null | \
        fzf --height 40% \
            --reverse \
            --delimiter $'\t' \
            --with-nth=1,2,3,4 \
            --header "搜索会话 (Ctrl+R 刷新 / 输入即搜索 / Enter 选择) · 路径不显示" \
            --prompt "session> " \
            --bind "ctrl-r:reload(bash -c 'format_candidates')" \
            --no-multi \
            || true
    )
    if [[ -z "$selected" ]]; then
        exit 0
    fi
    session_id=$(printf '%s' "$selected" | awk -F'\t' '{print $1}')
    path=$(printf '%s' "$selected" | awk -F'\t' '{print $5}')
    show_session "$session_id" "$path"
else
    echo "最近会话 (传入关键词或使用 TTY 启用 fzf):"
    echo "---"
    format_candidates 2>/dev/null \
        | awk -F'\t' '{printf "%s  %s  %s  %s\n", substr($1,1,12), $2, $3, $4}' \
        | head -30
fi
