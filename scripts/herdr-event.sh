#!/usr/bin/env bash
# herdr-event.sh — Herdr lifecycle hooks for session-digger
#
# worktree.created  → incremental reindex + real FTS warmup
# worktree.removed  → log only (keep index; history still valuable)
#
# Privacy: logs never store full home paths — basename / redacted only.
# Portable: HERDR_PLUGIN_ROOT / SESSION_DIGGER_ROOT; state under
#           HERDR_PLUGIN_STATE_DIR or session-digger data dir.

set -euo pipefail

EVENT="${1:-${HERDR_PLUGIN_EVENT:-unknown}}"
PLUGIN_ROOT="${HERDR_PLUGIN_ROOT:-${SESSION_DIGGER_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}}"
INDEX_BUILDER="$PLUGIN_ROOT/scripts/index-builder.py"
RECALL="$PLUGIN_ROOT/scripts/sd-recall.py"

# State/log dir: prefer Herdr plugin state, then digger data dir (no hard-coded user tree)
if [[ -n "${HERDR_PLUGIN_STATE_DIR:-}" ]]; then
    LOG_DIR="$HERDR_PLUGIN_STATE_DIR"
elif [[ -n "${SESSION_DIGGER_DATA_DIR:-}" ]]; then
    LOG_DIR="$SESSION_DIGGER_DATA_DIR"
else
    LOG_DIR="${HOME}/.claude/.session-digger"
fi
LOG_FILE="$LOG_DIR/herdr-events.log"
mkdir -p "$LOG_DIR"

# Redact path for logs: keep last 2 segments only
redact_path() {
    local p="${1:-}"
    [[ -z "$p" ]] && { echo "?"; return; }
    python3 -c 'import sys; from pathlib import Path
p=Path(sys.argv[1])
parts=p.parts
print("/".join(parts[-2:]) if len(parts)>=2 else p.name)' "$p" 2>/dev/null || basename "$p"
}

WORK_DIR_RAW="${HERDR_WORK_DIR:-.}"
WORK_DIR_SAFE="$(redact_path "$WORK_DIR_RAW")"
WS_ID="${HERDR_WORKSPACE_ID:-?}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] event=$EVENT workspace=$WS_ID cwd=$WORK_DIR_SAFE $*" >> "$LOG_FILE"
}

log "triggered"

case "$EVENT" in
    worktree.created)
        echo "session-digger: 检测到新工作树"

        # 1. Incremental reindex (cross-agent)
        if [[ -f "$INDEX_BUILDER" ]]; then
            python3 "$INDEX_BUILDER" build --agent cross >> "$LOG_FILE" 2>&1 || true
            echo "  索引已增量更新"
            log "reindex: ok"
        else
            echo "  警告: index-builder.py 缺失"
            log "reindex: missing builder"
        fi

        # 2. Real warmup: cheap FTS/list hit so first interactive query is hot
        if [[ -f "$RECALL" ]]; then
            python3 "$RECALL" sessions --scope all --limit 5 >/dev/null 2>&1 || true
            python3 "$RECALL" stats >/dev/null 2>&1 || true
            echo "  查询缓存已预热"
            log "warmup: sessions+stats"
        fi

        # 3. Adaptive multi-agent presence (counts only, no paths)
        if [[ -f "$PLUGIN_ROOT/scripts/echolib.py" ]]; then
            python3 - "$PLUGIN_ROOT/scripts" <<'PY' 2>/dev/null || true
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
try:
    import echolib
    result = echolib.scan_all_environments_parallel()
except Exception as e:
    print(f"  环境探测跳过: {type(e).__name__}")
    raise SystemExit(0)

# Normalize list/dict shapes without printing paths
items = []
if isinstance(result, dict):
    for k, v in result.items():
        if isinstance(v, dict):
            status = v.get("status") or v.get("state") or ("adapted" if v.get("adapted") else "unknown")
            items.append((str(k), status))
        else:
            items.append((str(k), str(type(v).__name__)))
elif isinstance(result, list):
    for it in result:
        if isinstance(it, dict):
            name = it.get("name") or it.get("agent") or it.get("env") or "?"
            status = it.get("status") or it.get("state") or "?"
            items.append((str(name), str(status)))

shown = 0
for name, status in items:
    if status in ("adapted", "discovered", "ok", "present") or "adapt" in status.lower():
        print(f"  环境: {name} ({status})")
        shown += 1
    if shown >= 12:
        break
if shown == 0:
    print("  环境: (无额外探测结果或已在索引中)")
PY
        fi

        echo "  就绪。prefix+d 统计 / prefix+s 搜索 / prefix+t 趋势"
        ;;

    worktree.removed)
        echo "session-digger: 工作树已移除（索引保留）"
        log "worktree removed"
        ;;

    *)
        echo "session-digger: 未知事件 $EVENT"
        log "unknown event"
        ;;
esac
