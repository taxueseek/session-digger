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

# Build candidate list from index: "session_id | agent | date | msgs | tools"
# Use Python to format for fzf (avoids fragile bash parsing)
format_candidates() {
    python3 - "$RECALL" <<'PYEOF'
import subprocess, sys, json
from pathlib import Path

recall = sys.argv[1]
result = subprocess.run(
    [sys.executable, recall, "sessions", "--scope", "all", "--limit", "200"],
    capture_output=True, text=True
)
# sd-recall.py sessions outputs human-readable text; parse line by line
lines = result.stdout.strip().split("\n")
for line in lines:
    line = line.strip()
    if not line or line.startswith("📂") or line.startswith("===") or line.startswith("最近"):
        continue
    # Each session line looks like: abc123...  claude ·  15 msgs · 2026-07-10
    parts = [p for p in line.split("  ") if p.strip()]
    if len(parts) >= 2:
        print(line)
PYEOF
}

export -f format_candidates

# fzf fuzzy selector
selected=$(
    format_candidates 2>/dev/null | \
    fzf --height 40% \
        --reverse \
        --header "搜索会话 (Ctrl+R 刷新 / / 键搜索关键词 / Enter 查看详情)" \
        --prompt "session> " \
        --bind "ctrl-r:reload(bash -c 'format_candidates')" \
        --preview "$RECALL search {1} --scope all --limit 3 2>/dev/null || echo '(需要关键词而非 ID)'" \
        --preview-window right:50%:wrap \
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

# messages 子命令接受文件路径而非 id，所以用 search 关键词 + session-stats 组合
python3 "$RECALL" search "$session_id" --scope all --limit 1
echo ""
echo "--- 全量消息 ---"
# 找到实际路径再跑 messages
python3 - "$RECALL" "$session_id" <<'PYEOF'
import subprocess, sys
from pathlib import Path

recall, sid = sys.argv[1], sys.argv[2]

# Locate the JSONL by scanning ~/.claude/projects/
base = Path.home() / ".claude" / "projects"
for jf in base.rglob("*.jsonl"):
    if jf.stem.startswith(sid[:8]):
        subprocess.run([sys.executable, recall, "messages", str(jf), "--limit", "50"])
        sys.exit(0)
print(f"(未找到 {sid} 的原始 JSONL 文件)")
PYEOF
