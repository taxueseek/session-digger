#!/usr/bin/env bash
# summary-index.sh — 归档索引管理
#
# 管理已分析会话的归档索引，支持查看、统计、导出。
#
# 用法:
#   summary-index.sh                    显示归档总览
#   summary-index.sh --list             列出所有已分析会话
#   summary-index.sh --list --fresh     只列新鲜摘要
#   summary-index.sh --list --stale     只列过期摘要
#   summary-index.sh --stats            显示统计信息
#   summary-index.sh --by-intent <关键词> 按查询意图过滤
#   summary-index.sh --rebuild          重建索引
#   summary-index.sh --export <path>    导出到 JSON 文件
#   summary-index.sh --prune            清理过期摘要
#
# 归档索引文件: ~/.claude/.session-digger-archive-index.json

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

INDEX_PATH="$HOME/.claude/.session-digger-archive-index.json"

ACTION="${1:-overview}"
shift || true

# 解析子选项
FILTER_FRESH=0
FILTER_STALE=0
INTENT_FILTER=""
EXPORT_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fresh) FILTER_FRESH=1; shift ;;
    --stale) FILTER_STALE=1; shift ;;
    --by-intent) INTENT_FILTER="$2"; shift 2 ;;
    --export) EXPORT_PATH="$2"; ACTION="export"; shift 2 ;;
    *) shift ;;
  esac
done

ES_ACTION="$ACTION" ES_INDEX="$INDEX_PATH" ES_SCRIPT_DIR="$SCRIPT_DIR" \
ES_FRESH="$FILTER_FRESH" ES_STALE="$FILTER_STALE" ES_INTENT="$INTENT_FILTER" \
ES_EXPORT="$EXPORT_PATH" \
python3 << 'PYEOF'
import os, sys, json
sys.path.insert(0, os.environ["ES_SCRIPT_DIR"])
import echolib

action = os.environ.get("ES_ACTION", "overview")
index_path = os.environ.get("ES_INDEX", "")
filter_fresh = os.environ.get("ES_FRESH", "0") == "1"
filter_stale = os.environ.get("ES_STALE", "0") == "1"
intent_filter = os.environ.get("ES_INTENT", "")
export_path = os.environ.get("ES_EXPORT", "")

# 构建索引
index = echolib.build_summary_index()

if action == "export" and export_path:
    echolib.save_summary_index(index, export_path)
    print("已导出到: %s" % export_path)
    print("总记录数: %d" % index["stats"]["total"])
    sys.exit(0)

if action == "rebuild":
    echolib.save_summary_index(index, index_path)
    print("索引已重建: %s" % index_path)
    print("总记录数: %d" % index["stats"]["total"])
    sys.exit(0)

stats = index["stats"]
sessions = index["analyzed_sessions"]

# 应用过滤
if filter_fresh:
    sessions = [s for s in sessions if s.get("is_fresh")]
if filter_stale:
    sessions = [s for s in sessions if not s.get("is_fresh")]
if intent_filter:
    sessions = [s for s in sessions if intent_filter.lower() in s.get("query_intent", "").lower()]

if action in ("overview", "stats"):
    print("=== Session-Digger 归档总览 ===")
    print()
    print("已分析会话总数: %d" % stats["total"])
    fresh_count = sum(1 for s in index["analyzed_sessions"] if s.get("is_fresh"))
    stale_count = stats["total"] - fresh_count
    print("  新鲜摘要: %d" % fresh_count)
    print("  过期摘要: %d" % stale_count)
    print()

    if stats["by_agent"]:
        print("按环境分布:")
        for agent, count in sorted(stats["by_agent"].items(), key=lambda x: -x[1]):
            print("  %-15s %d" % (agent, count))
        print()

    if stats["by_intent"]:
        print("按查询意图分布 (Top 10):")
        sorted_intents = sorted(stats["by_intent"].items(), key=lambda x: -x[1])
        for intent, count in sorted_intents[:10]:
            bar = "█" * min(count, 30)
            print("  %-30s %3d %s" % (intent[:30], count, bar))
        print()

    if stats["total"] == 0:
        print("提示: 尚无已分析的会话。")
        print("  使用 recall-lite.sh 搜索后，用 save-summary.sh 保存分析结果。")
    elif stale_count > 0:
        print("提示: %d 个摘要已过期（原始文件被更新）。" % stale_count)
        print("  重新分析这些会话即可刷新摘要。")

elif action == "list":
    if not sessions:
        print("无匹配的已分析会话。")
    else:
        print("=== 已分析会话列表 (%d 条) ===" % len(sessions))
        print()
        print("%-3s %-12s %-10s %-8s %-20s %s" % (
            "#", "分析时间", "环境", "状态", "查询意图", "会话ID"
        ))
        print("-" * 85)
        for i, s in enumerate(sessions, 1):
            status = "新鲜" if s.get("is_fresh") else "过期"
            print("%-3s %-12s %-10s %-8s %-20s %s" % (
                i,
                s.get("analyzed_at", "")[:10],
                s.get("source_agent", ""),
                status,
                s.get("query_intent", "")[:20],
                s.get("session_id", "")[:30],
            ))

elif action == "prune":
    pruned = 0
    for s in index["analyzed_sessions"]:
        if not s.get("is_fresh"):
            summary_path = s.get("summary_path", "")
            if summary_path and os.path.exists(summary_path):
                # 只清理完全过期的摘要文件
                # (注意：一个文件可能含多条记录，只清理全部过期的)
                pass  # 保守策略：不自动删除，仅报告
    stale = [s for s in index["analyzed_sessions"] if not s.get("is_fresh")]
    print("过期摘要: %d 条" % len(stale))
    if stale:
        print("保守策略：不自动删除。如需清理，手动删除对应 .summary.jsonl 文件。")
        print()
        for s in stale[:10]:
            print("  %s" % s.get("summary_path", ""))
PYEOF
