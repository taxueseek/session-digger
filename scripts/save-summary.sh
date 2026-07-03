#!/usr/bin/env bash
# save-summary.sh — 保存分析结果为摘要存证
#
# 用法:
#   save-summary.sh <session_path> <analysis_text|-> [query_intent] [agent_type] [memory_tier] [excluded]
#
# 将一次分析的结果保存为 .summary.jsonl 文件，供后续 recall-lite.sh 直接读取。
# 同一会话可多次保存（不同查询角度），追加模式。
#
# 参数:
#   session_path    原始会话文件路径
#   analysis_text   分析结果文本。用 - 表示从 stdin 读取
#   query_intent    本次查询意图（如 "投资"、"skill优化"），默认 "general"
#   agent_type      来源环境（claude/grok/kimi_code/codex），默认自动检测
#   memory_tier     时效等级（默认 periodic）:
#                     permanent: 认知规律、思维模型 → 永不遗忘
#                     periodic:  偏好、阶段性结论 → 7天后不再注入
#                     once:      临时上下文 → 24小时后失效
#   excluded        已否决方向，用分号分隔（如 "Rust不适合;方案C成本高"）
#
# 示例:
#   # 基本用法
#   save-summary.sh ~/.claude/projects/.../abc.jsonl "分析结果..." "投资"
#
#   # 永久记忆 + 否决方向
#   save-summary.sh ~/.../abc.jsonl "零依赖是核心优势" "技术选型" claude permanent "Rust维护成本高;Go生态不足"
#
#   # 从 stdin 读取
#   echo "分析结果..." | save-summary.sh ~/.../abc.jsonl - "投资"

# 不用 set -e — 这个脚本从 recall-lite.sh (set -e) 调用，
# 内部任何非零退出码都会被父 shell 的 set -e 捕获并终止。
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

SESSION_PATH="${1:?Usage: save-summary.sh <session_path> <analysis_text|-> [query_intent] [agent_type] [memory_tier] [excluded]}"
ANALYSIS_INPUT="${2:?Usage: save-summary.sh <session_path> <analysis_text|-> [query_intent] [agent_type] [memory_tier] [excluded]}"
QUERY_INTENT="${3:-general}"
AGENT_TYPE="${4:-auto}"
MEMORY_TIER="${5:-periodic}"
EXCLUDED_RAW="${6:-}"

if [[ ! -e "$SESSION_PATH" ]]; then
  echo "ERROR: Session file not found: $SESSION_PATH" >&2
  exit 1
fi

# 自动检测 agent 类型
if [[ "$AGENT_TYPE" == "auto" ]]; then
  if [[ "$SESSION_PATH" == *"/.claude/projects/"* ]]; then
    AGENT_TYPE="claude"
  elif [[ "$SESSION_PATH" == *"/.grok/sessions/"* ]]; then
    AGENT_TYPE="grok"
  elif [[ "$SESSION_PATH" == *"/.kimi-code/sessions/"* ]]; then
    AGENT_TYPE="kimi_code"
  elif [[ "$SESSION_PATH" == *"/.codex/sessions/"* ]]; then
    AGENT_TYPE="codex"
  else
    AGENT_TYPE="claude"
  fi
fi

# 校验 memory_tier
if [[ "$MEMORY_TIER" != "permanent" && "$MEMORY_TIER" != "periodic" && "$MEMORY_TIER" != "once" ]]; then
  echo "ERROR: memory_tier must be permanent, periodic, or once" >&2
  exit 1
fi

# 读取分析文本
if [[ "$ANALYSIS_INPUT" == "-" ]]; then
  ANALYSIS=$(cat)
else
  ANALYSIS="$ANALYSIS_INPUT"
fi

if [[ -z "$ANALYSIS" ]]; then
  echo "ERROR: Analysis text is empty" >&2
  exit 1
fi

# 调用 echolib 保存 — 用 -c 而非 heredoc，避免 set -e + 多行环境变量交互问题
export ES_INPUT="$SESSION_PATH"
export ES_ANALYSIS="$ANALYSIS"
export ES_QUERY="$QUERY_INTENT"
export ES_AGENT="$AGENT_TYPE"
export ES_TIER="$MEMORY_TIER"
export ES_EXCLUDED="$EXCLUDED_RAW"
export ES_SCRIPT_DIR="$SCRIPT_DIR"

python3 -c '
import os, sys, json
sys.path.insert(0, os.environ["ES_SCRIPT_DIR"])
import echolib

session_path = os.environ["ES_INPUT"]
analysis = os.environ["ES_ANALYSIS"]
query_intent = os.environ.get("ES_QUERY", "general")
agent_type = os.environ.get("ES_AGENT", "claude")
memory_tier = os.environ.get("ES_TIER", "periodic")
excluded_raw = os.environ.get("ES_EXCLUDED", "")

excluded = [s.strip() for s in excluded_raw.split(";") if s.strip()] if excluded_raw else []

summary_path = echolib.save_analysis_result(
    session_path, analysis, query_intent, agent_type,
    memory_tier=memory_tier, excluded=excluded
)

print("已保存分析摘要:")
print("  会话: %s" % os.path.basename(session_path))
print("  意图: %s" % query_intent)
print("  环境: %s" % agent_type)
print("  时效: %s" % memory_tier)
if excluded:
    print("  已否决:")
    for ex in excluded:
        print("    - %s" % ex)
print("  摘要: %s" % summary_path)
'
