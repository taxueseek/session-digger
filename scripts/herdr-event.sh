#!/usr/bin/env bash
# herdr-event.sh — Herdr event hooks for session-digger
#
# Triggered by Herdr lifecycle events:
#   worktree.created  — new worktree created → reindex + warm up project sessions
#   worktree.removed  — worktree removed → log + cleanup
#
# On worktree.created, the handler:
#   1. Rebuilds the session-digger index (incremental, fast)
#   2. Detects Agent session directories relevant to the new worktree
#   3. Pre-warms the FTS index so first query is instant
#
# Environment from Herdr:
#   HERDR_PLUGIN_ROOT   — absolute path to plugin directory
#   HERDR_WORKSPACE_ID  — current workspace id
#   HERDR_WORK_DIR      — working directory of the new worktree (if available)
#   HERDR_ENV           — "1"

set -euo pipefail

EVENT="${1:-unknown}"
PLUGIN_ROOT="${HERDR_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
INDEX_BUILDER="$PLUGIN_ROOT/scripts/index-builder.py"
RECALL="$PLUGIN_ROOT/scripts/sd-recall.py"
LOG_DIR="$HOME/.claude/.session-digger"
LOG_FILE="$LOG_DIR/herdr-events.log"

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] event=$EVENT workspace=${HERDR_WORKSPACE_ID:-?} cwd=${HERDR_WORK_DIR:-?} $*" >> "$LOG_FILE"
}

log "triggered"

case "$EVENT" in
    worktree.created)
        echo "session-digger: 检测到新工作树"

        # 1. 增量重建索引
        if [[ -f "$INDEX_BUILDER" ]]; then
            python3 "$INDEX_BUILDER" build --agent cross >> "$LOG_FILE" 2>&1 || true
            echo "  索引已重建"
        fi

        # 2. 探测该目录相关的 Agent 会话并预热
        work_dir="${HERDR_WORK_DIR:-.}"
        if [[ -d "$work_dir" ]]; then
            echo "  探测目录: $work_dir"

            # 检测 ~/.claude/projects/ 下是否有该项目的会话
            claude_projects="$HOME/.claude/projects"
            if [[ -d "$claude_projects" ]]; then
                # 从路径提取项目 slug（Claude Code 用 - 分隔路径哈希）
                project_name=$(basename "$work_dir")
                session_count=$(find "$claude_projects" -name "*.jsonl" -newer "$work_dir/.git/index" 2>/dev/null | wc -l | tr -d ' ')
                if [[ "$session_count" -gt 0 ]]; then
                    echo "  发现 $session_count 个近期 Agent 会话，已预热索引"
                    log "warmup: $session_count recent sessions for $project_name"
                else
                    echo "  暂无历史 Agent 会话（首次使用）"
                    log "warmup: no sessions yet for $project_name"
                fi
            fi

            # 3. 检查其他 Agent 环境
            for agent_dir in "$HOME/.grok/sessions" "$HOME/.kimi-code/sessions"; do
                if [[ -d "$agent_dir" ]]; then
                    count=$(find "$agent_dir" -name "*.jsonl" -mtime -7 2>/dev/null | wc -l | tr -d ' ')
                    if [[ "$count" -gt 0 ]]; then
                        agent_name=$(basename "$(dirname "$agent_dir")")
                        echo "  $agent_name: $count 个活跃会话（近 7 天）"
                        log "detect: $agent_name has $count active sessions"
                    fi
                fi
            done
        fi

        echo " 就绪。按 prefix+d 打开统计面板，prefix+s 搜索会话。"
        ;;

    worktree.removed)
        echo "session-digger: 工作树已移除"
        log "worktree removed"
        # 索引保留（历史会话仍有价值），仅记录
        ;;

    *)
        echo "session-digger: 未知事件 $EVENT"
        log "unknown event"
        ;;
esac
