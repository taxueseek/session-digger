#!/usr/bin/env bash
# topic-scan.sh — 会话主题扫描 + 聚类 + 路由推荐。
# 扫描所有会话，按主题聚类，统计成本，推荐路由到对应的 taxue-* 技能。
#
# 用法: topic-scan.sh [--days N] [--format text|json] [--limit N]
#        topic-scan.sh --topic <编号>  提取指定主题的上下文包
#        topic-scan.sh --session <file>  单会话分析
#
# 输出格式（text）：
#   会话主题总览 → 用户选择主题 → 提取上下文包 → 路由建议
#
# 共享分类逻辑在 scripts/topic_classify.py（避免 heredoc 间重复）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DAY_LIMIT=30
FORMAT="text"
LIMIT=200
SHOW_TOPIC=""
SESSION_FILE=""

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --days) DAY_LIMIT="$2"; shift 2 ;;
    --format) FORMAT="$2"; shift 2 ;;
    --limit) LIMIT="$2"; shift 2 ;;
    --topic) SHOW_TOPIC="$2"; shift 2 ;;
    --session) SESSION_FILE="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: topic-scan.sh [--days N] [--format text|json] [--limit N]"
      echo "       topic-scan.sh --topic <编号>  提取指定主题的会话数据包"
      echo "       topic-scan.sh --session <path>  单会话分析"
      echo ""
      echo "Options:"
      echo "  --days N          Limit scan to last N days (0 = no limit, default: 30)"
      echo "  --format text|json Output format (default: text)"
      echo "  --limit N         Max sessions to scan (default: 200)"
      echo "  --topic <编号>    提取指定主题的上下文包"
      echo "  --session <path>  分析单个会话文件"
      exit 0
      ;;
    -*)
      echo "ERROR: Unknown option: $1" >&2
      echo "Usage: topic-scan.sh [--days N] [--format text|json] [--limit N]" >&2
      echo "       topic-scan.sh --topic <编号>  提取指定主题的会话数据包" >&2
      exit 1
      ;;
    *)
      echo "ERROR: Unexpected argument: $1" >&2
      exit 1
      ;;
  esac
done

export TS_SCRIPT_DIR="$SCRIPT_DIR"
export TS_FORMAT="$FORMAT"
# --days 0 means no limit
if [[ "$DAY_LIMIT" -eq 0 ]]; then
  export TS_DAYS="0"
else
  export TS_DAYS="$DAY_LIMIT"
fi

# ---- 模式 2: 提取指定主题的上下文包 ----
extract_topic_package() {
  local topic_id="$1"
  TOPIC_ID="$topic_id" python3 << 'PYEOF'
import json, os, sys
from collections import Counter
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.environ["TS_SCRIPT_DIR"])
import topic_classify as tc

target_topic_id = int(os.environ.get("TOPIC_ID", "0"))

# Apply days filter
days_limit = int(os.environ.get("TS_DAYS", "30"))
since_date = ""
if days_limit > 0:
    since_date = (datetime.now(timezone.utc) - timedelta(days=days_limit)).strftime("%Y-%m-%d")

topic_sessions, topic_stats = tc.scan_and_classify(limit=500, since_date=since_date)

sessions = topic_sessions.get(target_topic_id, [])
rule = tc.topic_rule(target_topic_id)
topic_name = rule["name"] if rule else f"主题{target_topic_id}"
skill = rule["skill"] if rule else "taxue-solve"
ts = topic_stats.get(target_topic_id, {})
stock_codes = ts.get("stock_codes", set())

# Output structured context package
print(f"# 会话数据包：{topic_name}")
print()
print("## 概览")
print(f"- 会话数：{ts.get('count', 0)}")
print(f"- 总成本：${ts.get('total_cost', 0):.2f}")
print(f"- 总 Token：{ts.get('total_tokens', 0):,}")
if stock_codes:
    print(f"- 涉及标的：{', '.join(sorted(stock_codes)[:10])}")
if ts.get("start"):
    print(f"- 时间范围：{str(ts['start'])[:10]} ~ {str(ts['end'])[:10]}")
print(f"- 路由技能：`{skill}`")
print()

# Check for duplicate analysis
if stock_codes and len(sessions) > 1:
    # Find stocks analyzed multiple times
    stock_counts = Counter()
    for s in sessions:
        for code in s.get("classification", {}).get("stock_codes", []):
            stock_counts[code] += 1
    dupes = {k: v for k, v in stock_counts.items() if v > 1}
    if dupes:
        print("## 重复分析提示")
        for code, cnt in sorted(dupes.items()):
            dup_cost = sum(s["cost"] for s in sessions if code in s.get("classification", {}).get("stock_codes", []))
            print(f"- **{code}**：{cnt} 次分析，合计 ${dup_cost:.2f}")
        print()

# Tool usage summary
all_tools = Counter()
for s in sessions:
    for t in s.get("classification", {}).get("tools_used", []):
        if tc.is_relevant_tool(t):
            all_tools[t] += 1
if all_tools:
    print("## 工具使用频率")
    for tool, cnt in all_tools.most_common(10):
        print(f"- `{tool}`：{cnt} 次")
    print()

print("## 会话列表")
for idx, s in enumerate(sessions, 1):
    st = s["stats"]
    print(f"\n### {idx}. {s['first_prompt'][:80]}...")
    print(f"- 时间：{str(st.get('started', ''))[:19]}")
    print(f"- 成本：${s['cost']}")
    print(f"- Token：{st.get('input_tokens', 0):,} in / {st.get('output_tokens', 0):,} out")
    print(f"- 工具：{', '.join(s['classification']['tools_used'][:5])}")
    if s['classification']['stock_codes']:
        print(f"- 标的：{', '.join(s['classification']['stock_codes'])}")
    print(f"- 路径：`{s['path']}`")

print()
print("---")
print(f"使用 `/taxue-industry`（或其他匹配技能）基于以上数据做深入分析。")
PYEOF
}

# ---- 模式 1: 输出主题总览 ----
generate_overview() {
  python3 << 'PYEOF'
import json, os, sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.environ["TS_SCRIPT_DIR"])
import topic_classify as tc

# Apply days filter
days_limit = int(os.environ.get("TS_DAYS", "30"))
since_date = ""
if days_limit > 0:
    since_date = (datetime.now(timezone.utc) - timedelta(days=days_limit)).strftime("%Y-%m-%d")

_, topic_stats = tc.scan_and_classify(limit=200, since_date=since_date)

# Sort by cost
sorted_topics = sorted(
    [(tid, st) for tid, st in topic_stats.items() if tid > 0 and st["count"] > 0],
    key=lambda x: x[1]["total_cost"],
    reverse=True
)

unclassified = topic_stats.get(0, {})
unc_count = unclassified.get("count", 0)
unc_cost = unclassified.get("total_cost", 0.0)
total_cost = sum(st["total_cost"] for _, st in sorted_topics) + unc_cost
total_sessions = sum(st["count"] for _, st in sorted_topics) + unc_count

fmt = os.environ.get("TS_FORMAT", "text")

if fmt == "json":
    output = {
        "total_sessions": total_sessions,
        "total_cost": round(total_cost, 2),
        "topics": []
    }
    for tid, st in sorted_topics:
        rule = tc.topic_rule(tid)
        output["topics"].append({
            "id": tid,
            "name": rule["name"] if rule else f"Topic {tid}",
            "skill": rule["skill"] if rule else "taxue-solve",
            "count": st["count"],
            "total_cost": round(st["total_cost"], 2),
            "total_tokens": st["total_tokens"],
            "tools": list(st["tools"]),
            "stock_codes": list(st["stock_codes"]),
            "start": str(st.get("start", ""))[:10] if st.get("start") else "",
            "end": str(st.get("end", ""))[:10] if st.get("end") else "",
        })
    if unc_count > 0:
        output["topics"].append({
            "id": 0,
            "name": "未分类",
            "skill": "taxue-solve",
            "count": unc_count,
            "total_cost": round(unc_cost, 2),
        })
    print(json.dumps(output, ensure_ascii=False, indent=2))
else:
    print("=" * 65)
    print("  会话主题总览")
    print("=" * 65)
    print()
    all_starts = [st.get("start") for _, st in sorted_topics if st.get("start")]
    all_ends = [st.get("end") for _, st in sorted_topics if st.get("end")]
    if all_starts and all_ends:
        min_start = min(all_starts)[:10]
        max_end = max(all_ends)[:10]
        print(f"  共 {total_sessions} 次会话 | 总成本 ${total_cost:.2f} | {min_start} ~ {max_end}")
    else:
        print(f"  共 {total_sessions} 次会话 | 总成本 ${total_cost:.2f}")
    print()
    print("请选择你想深入分析的主题（使用 `--topic <编号>`）：")
    print()
    for tid, st in sorted_topics:
        rule = tc.topic_rule(tid)
        name = rule["name"] if rule else f"主题{tid}"
        skill = rule["skill"] if rule else "taxue-solve"
        pct = (st["total_cost"] / total_cost * 100) if total_cost > 0 else 0
        time_range = ""
        if st.get("start") and st.get("end"):
            time_range = f"{str(st['start'])[:10]} ~ {str(st['end'])[:10]}"
        tools_list = ", ".join(sorted(st["tools"])[:5]) if st["tools"] else ""
        print(f"[{tid}] {name}")
        print(f"    {st['count']} 次会话 | ${st['total_cost']:.2f} ({pct:.0f}%) | {time_range}")
        if tools_list:
            print(f"    涉及：{tools_list}")
        if st["stock_codes"]:
            codes = ", ".join(sorted(st["stock_codes"])[:8])
            print(f"    标的：{codes}")
        print(f"    建议路由技能：`{skill}`")
        print()
    if unc_count > 0:
        pct = (unc_cost / total_cost * 100) if total_cost > 0 else 0
        print(f"[0] 未分类（{unc_count} 次会话 | ${unc_cost:.2f} ({pct:.0f}%)）")
        print(f"    建议路由技能：`taxue-solve`")
        print()
    print("使用方式：topic-scan.sh --topic <编号>  提取该主题的会话数据包")
    print("然后交由对应的技能做深度分析。")
PYEOF
}

# ---- 模式 3: 单会话分析 ----
analyze_single_session() {
  local file="$1"
  TS_FILE="$file" python3 << 'PYEOF'
import json, os, sys
sys.path.insert(0, os.environ["TS_SCRIPT_DIR"])
import echolib

file_path = os.environ["TS_FILE"]
stats = echolib.session_stats(file_path)
cost = (stats.get("input_tokens", 0) / 1_000_000 * 3.0) + (stats.get("output_tokens", 0) / 1_000_000 * 15.0)

fmt = os.environ.get("TS_FORMAT", "text")

# Tool analysis
tools_used = []
for t in echolib.extract_tools(file_path, limit=50):
    tools_used.append(t["name"])

# Messages
first_prompt = ""
for m in echolib.extract_messages(file_path, role="user", limit=1):
    first_prompt = m.get("text", "")[:300]

if fmt == "json":
    print(json.dumps({
        "file": file_path,
        "stats": stats,
        "cost": round(cost, 2),
        "first_prompt": first_prompt,
        "tools_used": tools_used,
    }, ensure_ascii=False, indent=2))
else:
    print(f"会话分析: {file_path}")
    print(f"模型: {stats.get('model', '?')}")
    print(f"成本: ${cost:.2f}")
    print(f"Token: {stats.get('input_tokens', 0)} in / {stats.get('output_tokens', 0)} out")
    print(f"工具: {', '.join(set(tools_used))}")
    print(f"首条消息: {first_prompt[:100]}")
PYEOF
}

# ---- Main dispatch ----
if [[ -n "$SESSION_FILE" ]]; then
  analyze_single_session "$SESSION_FILE"
elif [[ -n "$SHOW_TOPIC" ]]; then
  extract_topic_package "$SHOW_TOPIC"
else
  generate_overview
fi
