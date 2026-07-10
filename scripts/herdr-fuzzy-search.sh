#!/usr/bin/env bash
# herdr-fuzzy-search.sh — Fuzzy search pane for session-digger
#
# Opens an interactive fzf selector over all indexed sessions.
# User picks one → shows detail (messages, tools, files touched).
#
# Requirement: fzf (https://github.com/junegunn/fzf)
# Install:  brew install fzf | apt install fzf | etc.
#
# Environment from Herdr:
#   HERDR_PLUGIN_ROOT  — absolute path to plugin directory
#   HERDR_WORKSPACE_ID — current workspace id
#   HERDR_ENV          — "1"

set -euo pipefail

PLUGIN_ROOT="${HERDR_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
RECALL="$PLUGIN_ROOT/scripts/sd-recall.py"

if [[ ! -f "$RECALL" ]]; then
    echo "ERROR: sd-recall.py not found at $RECALL" >&2
    exit 1
fi

if ! command -v fzf &>/dev/null; then
    echo "ERROR: fzf not found. Install it first:" >&2
    echo "  brew install fzf    # macOS" >&2
    echo "  apt install fzf    # Debian/Ubuntu" >&2
    echo "  pacman -S fzf      # Arch" >&2
    echo "" >&2
    echo "Falling back to list mode..." >&2
    python3 "$RECALL" sessions --scope all --limit 30
    exit 0
fi

# Build candidate list from sd-recall.py sessions (TSV output)
# Format for fzf: "session_id  agent  date  msgs"
format_candidates() {
    python3 - "$RECALL" <<'PYEOF'
import subprocess, sys

recall = sys.argv[1]
result = subprocess.run(
    [sys.executable, recall, "sessions", "--scope", "all", "--limit", "200"],
    capture_output=True, text=True
)
lines = result.stdout.strip().split("\n")
# TSV format: SESSION_ID \t CREATED \t MODIFIED \t MSGS \t BRANCH \t AGENT \t PATH
# Skip header row (starts with SESSION_ID)
for line in lines:
    parts = line.split("\t")
    if len(parts) >= 6 and parts[0] != "SESSION_ID":
        sid = parts[0][:12]
        created = parts[1][:10] if parts[1] else "?"
        msgs = parts[3] if len(parts) > 3 else "?"
        agent = parts[5] if len(parts) > 5 else "?"
        print(f"{sid}  {agent}  {created}  {msgs} msgs")
PYEOF
}

export -f format_candidates

# Keyword from arguments (optional)
KEYWORD="${1:-}"

# fzf: interactive mode if TTY available, filter mode otherwise
# (filter mode = non-interactive search, prints matches directly)
if [[ -n "$KEYWORD" ]]; then
    # Search keyword provided → filter mode (works with or without TTY)
    echo "搜索: $KEYWORD"
    echo "---"
    format_candidates 2>/dev/null | fzf --filter "$KEYWORD" --no-sort 2>/dev/null || true
elif [[ -t 0 && -t 1 ]]; then
    # Interactive TTY → full fzf experience
    selected=$(
        format_candidates 2>/dev/null | \
        fzf --height 40% \
            --reverse \
            --header "搜索会话 (Ctrl+R 刷新 / 输入即搜索 / Enter 选择)" \
            --prompt "session> " \
            --bind "ctrl-r:reload(bash -c 'format_candidates')" \
            --no-multi \
            || true
    )

    if [[ -z "$selected" ]]; then
        exit 0
    fi

    # Extract session id (first column) → show full detail
    session_id=$(echo "$selected" | awk '{print $1}')
    echo "=== 会话详情: $session_id ==="
    echo ""
    python3 "$RECALL" search "$session_id" --scope all --limit 1
    echo ""
    echo "--- 消息预览 ---"
    python3 - "$RECALL" "$session_id" <<'PYEOF'
import subprocess, sys
from pathlib import Path

recall, sid = sys.argv[1], sys.argv[2]
base = Path.home() / ".claude" / "projects"
for jf in base.rglob("*.jsonl"):
    if jf.stem.startswith(sid[:8]):
        subprocess.run([sys.executable, recall, "messages", str(jf), "--limit", "50"])
        sys.exit(0)
print(f"(未找到 {sid} 的原始 JSONL 文件)")
PYEOF
else
    # No TTY, no keyword → plain list
    echo "最近会话 (传入关键词或使用 TTY 启用 fzf):"
    echo "---"
    format_candidates 2>/dev/null | head -30
fi
