#!/usr/bin/env bash
# topic-scan.sh — 会话主题扫描 + 聚类 + 路由推荐。
# 扫描所有会话，按主题聚类，统计成本，推荐路由到对应的 taxue-* 技能。
#
# 用法: topic-scan.sh [--days N] [--format text|json] [--limit N]
#        topic-scan.sh --topic <编号>  提取指定主题的上下文包
#
# 输出格式（text）：
#   会话主题总览 → 用户选择主题 → 提取上下文包 → 路由建议
#
# v1.0: 智能路由中枢

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DAYS=30
FORMAT="text"
LIMIT=200
SHOW_TOPIC=""
SESSION_FILE=""

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --days) DAYS="$2"; shift 2 ;;
    --format) FORMAT="$2"; shift 2 ;;
    --limit) LIMIT="$2"; shift 2 ;;
    --topic) SHOW_TOPIC="$2"; shift 2 ;;
    --session) SESSION_FILE="$2"; shift 2 ;;
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

# ---- 模式 2: 提取指定主题的上下文包 ----
extract_topic_package() {
  local topic_id="$1"
  TOPIC_ID="$topic_id" python3 << 'PYEOF'
import json, os, sys, re
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.environ["TS_SCRIPT_DIR"])
import echolib

# ---------- Cross-environment dispatch helpers ----------
ZCODE_SCRIPT = os.path.join(os.environ["TS_SCRIPT_DIR"], "zcode-adapter.py")

def _run_zcode(args):
    """Run zcode-adapter.py and return stdout."""
    import subprocess as _sp
    try:
        result = _sp.run([sys.executable, ZCODE_SCRIPT] + args,
                       capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except Exception:
        pass
    return ""

def _resolve_agent(path):
    if str(path).startswith("zcode://"):
        return "zcode"
    atype = echolib.detect_agent_type(path)
    return atype if atype in echolib.ADAPTER_REGISTRY else "claude"

def _dispatch_stats(path):
    agent = _resolve_agent(path)
    fn = echolib.ADAPTER_REGISTRY.get(agent, {}).get("session_stats")
    return fn(path) if fn else echolib.session_stats(path)

def _dispatch_tools(path, limit=30):
    agent = _resolve_agent(path)
    fn = echolib.ADAPTER_REGISTRY.get(agent, {}).get("extract_tools")
    if fn:
        if agent == "grok":
            import os as _os
            from pathlib import Path as _P
            session_dir = str(_P(path).parent) if _P(path).is_file() else path
            return fn(session_dir, errors_only=False, limit=limit)
        return fn(path, errors_only=False, limit=limit)
    return echolib.extract_tools(path, errors_only=False, limit=limit)

def _dispatch_messages(path, role="user", limit=5):
    agent = _resolve_agent(path)
    fn = echolib.ADAPTER_REGISTRY.get(agent, {}).get("extract_messages")
    if fn:
        return fn(path, role=role, limit=limit, thinking_limit=0)
    return echolib.extract_messages(path, role=role, limit=limit)

def get_all_sessions(limit=500):
    """Cross-environment session listing including ZCode."""
    all_sessions = echolib.cross_tool_list_sessions(limit=limit, keyword="")
    # Also try ZCode adapter
    _zcode_path = os.path.join(os.environ["TS_SCRIPT_DIR"], "zcode-adapter.py")
    if os.path.exists(_zcode_path):
        try:
            import subprocess as _sp
            result = _sp.run([sys.executable, _zcode_path, "list-sessions", "--limit", str(limit)],
                           capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and result.stdout.strip():
                zcode_sessions = json.loads(result.stdout)
                all_sessions.extend(zcode_sessions)
        except Exception:
            pass
    return all_sessions

# ---------- 主题分类映射 ----------
TOPIC_RULES = [
    {
        "id": 1, "name": "投资分析",
        "skill": "taxue-industry",
        "keywords": ["股票", "基金", "ETF", "财报", "PE", "PB", "ROE", "估值",
                      "买入", "卖出", "持有", "加仓", "减仓", "止损", "止盈"],
        "tools": ["eastmoney", "invest-", "financial-report", "stock-analysis",
                  "fund-", "fundfof", "fundscreen", "ttfund"],
        "code_pattern": r"\b[036]\d{5}\b",
        "stock_confirm_kw": ["股票", "基金", "ETF", "财报", "PE", "PB", "ROE", "估值"],
    },
    {
        "id": 2, "name": "内容创作",
        "skill": "taxue-content",
        "keywords": ["文章", "写作", "公众号", "小红书", "知乎", "标题", "排版",
                      "内容", "文案", "稿子", "发布", "配图"],
        "tools": ["content-alchemist", "huajiao-finance-writer", "dbs-content",
                  "wechat-humon-blogger", "taxue-content", "long-form-writing"],
        "code_pattern": None,
    },
    {
        "id": 3, "name": "技能开发",
        "skill": "taxue-skill",
        "keywords": ["skill", "技能", "模板", "触发词", "SKILL.md"],
        "tools": ["skill-creator", "tx-create", "taxue-skill", "taxue-meta",
                  "create-skills"],
        "code_pattern": None,
    },
    {
        "id": 4, "name": "学习",
        "skill": "taxue-learn",
        "keywords": ["学习", "读书", "想学", "入门", "笔记", "阅读", "教程",
                      "课程", "知识", "掌握"],
        "tools": ["taxue-learn", "taxue-weread"],
        "code_pattern": None,
    },
    {
        "id": 5, "name": "商业判断",
        "skill": "taxue-business",
        "keywords": ["项目", "副业", "值不值得", "商业模式", "创业", "变现",
                      "市场", "成本", "收入"],
        "tools": ["taxue-business"],
        "code_pattern": None,
    },
    {
        "id": 6, "name": "职业发展",
        "skill": "taxue-career",
        "keywords": ["简历", "面试", "Offer", "求职", "转行", "跳槽", "职业",
                      "招聘", "工作", "离职"],
        "tools": ["taxue-career", "interview-coach", "taxue-job-search"],
        "code_pattern": None,
    },
    {
        "id": 7, "name": "沟通表达",
        "skill": "taxue-relate",
        "keywords": ["谈判", "吵架", "汇报", "说服", "沟通", "关系", "冲突",
                      "话术", "加薪"],
        "tools": ["taxue-relate", "taxue-speak", "taxue-talk"],
        "code_pattern": None,
    },
    {
        "id": 8, "name": "流量增长",
        "skill": "taxue-traffic",
        "keywords": ["流量", "涨粉", "阅读量", "分发", "推荐", "曝光",
                      "增长", "粉丝"],
        "tools": ["taxue-traffic"],
        "code_pattern": None,
    },
    {
        "id": 9, "name": "系统运维",
        "skill": "taxue-meta",
        "keywords": ["环境", "配置", "报错", "安装", "部署", "升级", "网络",
                      "依赖", "Bug", "修复"],
        "tools": ["sys-doctor", "zcode-guide", "dbs-bridge", "dbs-hook"],
        "code_pattern": None,
    },
]

# 费率（Claude Code）
INPUT_RATE = 3.0   # $/1M tokens
OUTPUT_RATE = 15.0 # $/1M tokens

# 通用工具（不显示在"涉及"中）
GENERIC_TOOLS = frozenset({
    "Agent", "AskUserQuestion", "Bash", "BashOutput", "Edit", "EnterPlanMode",
    "ExitPlanMode", "Grep", "Glob", "LS", "Read", "Write", "Skill",
    "TaskCreate", "WebFetch", "WebSearch", "SendMessage", "TodoWrite",
    "TodoRead", "ReadSessionContext", "ComputerUse", "BrowserUse",
    "TaskOutput", "TaskUpdate", "TaskContinue",
})

def is_relevant_tool(name):
    """Check if a tool name is domain-specific (not generic)."""
    if not name:
        return False
    if name in GENERIC_TOOLS:
        return False
    # Skip internal/plugin tools
    if name.startswith(("mcp__", "_", "zcode_")):
        return False
    return True

def estimate_cost(stats):
    """Estimate USD cost from session stats."""
    inp = stats.get("input_tokens", 0)
    out = stats.get("output_tokens", 0)
    return (inp / 1_000_000 * INPUT_RATE) + (out / 1_000_000 * OUTPUT_RATE)

def classify_session(summary, first_prompt, tools_used_str, file_path=""):
    """Classify a session into a topic category. Returns (topic_id, confidence, details)."""
    text = (summary + " " + first_prompt).lower()
    tools_lower = tools_used_str.lower()
    scores = []

    for rule in TOPIC_RULES:
        score = 0
        matched_kw = []
        matched_tools = []

        for kw in rule["keywords"]:
            if kw.lower() in text:
                score += 2
                matched_kw.append(kw)

        for tool in rule["tools"]:
            if tool.lower() in tools_lower:
                score += 3
                matched_tools.append(tool)

        # Stock code pattern match (requires confirmation keywords)
        if rule.get("code_pattern") and re.search(rule["code_pattern"], text):
            confirm_kw = rule.get("stock_confirm_kw", [])
            has_confirm = any(kw.lower() in text for kw in confirm_kw)
            if has_confirm:
                score += 3
                matched_kw.append("(股票代码)")
            else:
                # Low-confidence match - minimal boost
                score += 1

        if score > 0:
            scores.append((rule["id"], score, matched_kw, matched_tools))

    if not scores:
        return (0, 0, [], [])  # 未分类

    scores.sort(key=lambda x: x[1], reverse=True)
    return scores[0]

def classify_session_with_messages(session_path, stats, first_prompt):
    """Enhanced classification: also scan more messages for better detection."""
    summary = stats.get("summary", "")

    # Collect tool names used in session
    tools_used = set()
    try:
        for t in _dispatch_tools(session_path, limit=30):
            name = t.get("name", "")
            if name:
                tools_used.add(name)
    except Exception:
        pass

    # Also check user messages for deeper classification
    user_texts = []
    try:
        for m in _dispatch_messages(session_path, role="user", limit=5):
            txt = m.get("text", "")
            if txt and len(txt) > 20:
                user_texts.append(txt[:500])
    except Exception:
        pass

    additional_text = " ".join(user_texts) if user_texts else ""
    combined_text = summary + " " + first_prompt + " " + additional_text
    tools_str = ", ".join(sorted(tools_used))

    topic_id, confidence, kws, tools_matched = classify_session(
        summary, first_prompt + " " + additional_text, tools_str
    )

    # Stock code extraction
    stock_codes = set()
    for match in re.finditer(r"\b\d{6}\b", combined_text):
        code = match.group()
        # Filter: only common A-share prefixes
        if code.startswith(("0", "3", "6")):
            stock_codes.add(code)

    return {
        "topic_id": topic_id,
        "confidence": confidence,
        "matched_keywords": kws,
        "matched_tools": tools_matched,
        "tools_used": list(tools_used)[:10],
        "stock_codes": list(stock_codes)[:5],
        "user_text_snippets": user_texts[:2],
    }

# Get the requested topic ID
target_topic_id = int(os.environ.get("TOPIC_ID", "0"))

# Scan sessions
all_sessions = get_all_sessions(limit=500)

# Classify and group
topic_sessions = defaultdict(list)
topic_stats = defaultdict(lambda: {
    "count": 0, "total_cost": 0.0, "total_tokens": 0,
    "tools": set(), "start": None, "end": None,
    "stock_codes": set(), "subjects": set(),
})

for meta in all_sessions:
    path = meta.get("full_path", "") if isinstance(meta, dict) else meta.full_path
    if not path:
        continue
    if not str(path).startswith("zcode://") and not os.path.exists(path):
        continue

    try:
        stats = _dispatch_stats(path)
    except Exception:
        continue

    cost = estimate_cost(stats)
    first_prompt = meta.get("first_prompt", "") if isinstance(meta, dict) else (meta.first_prompt or "")
    classification = classify_session_with_messages(path, stats, first_prompt)
    tid = classification["topic_id"]

    ts = topic_stats[tid]
    ts["count"] += 1
    ts["total_cost"] += cost
    ts["total_tokens"] += stats.get("total_tokens", 0)
    ts["tools"].update(t for t in classification["tools_used"] if is_relevant_tool(t))
    if classification["stock_codes"]:
        ts["stock_codes"].update(classification["stock_codes"])
    if stats.get("started"):
        if not ts["start"] or stats["started"] < ts["start"]:
            ts["start"] = stats["started"]
    if stats.get("ended"):
        if not ts["end"] or stats["ended"] > ts["end"]:
            ts["end"] = stats["ended"]

    # Store session detail for context package
    rule = next((r for r in TOPIC_RULES if r["id"] == tid), None)
    topic_sessions[tid].append({
        "path": path,
        "stats": stats,
        "cost": round(cost, 2),
        "first_prompt": first_prompt[:200],
        "classification": classification,
        "topic_name": rule["name"] if rule else "未分类",
        "skill": rule["skill"] if rule else "taxue-solve",
    })

# If --topic was specified, output the context package for that topic
if target_topic_id > 0:
    sessions = topic_sessions.get(target_topic_id, [])
    rule = next((r for r in TOPIC_RULES if r["id"] == target_topic_id), None)
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
            if is_relevant_tool(t):
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

    sys.exit(0)

# ---- 模式 1: 输出主题总览 ----
print("=" * 65)
print("  会话主题总览")
print("=" * 65)
print()

# Sort topics by cost (most expensive first)
sorted_topics = sorted(
    [(tid, st) for tid, st in topic_stats.items() if tid > 0 and st["count"] > 0],
    key=lambda x: x[1]["total_cost"],
    reverse=True
)

# "未分类" topic
unclassified = topic_stats.get(0, {})
unc_count = unclassified.get("count", 0)
unc_cost = unclassified.get("total_cost", 0.0)

total_cost = sum(st["total_cost"] for _, st in sorted_topics) + unc_cost
total_sessions = sum(st["count"] for _, st in sorted_topics) + unc_count

# Time range
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

for idx, (tid, st) in enumerate(sorted_topics, 1):
    rule = next((r for r in TOPIC_RULES if r["id"] == tid), None)
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

# ---- 模式 1: 输出主题总览 ----
generate_overview() {
  python3 << 'PYEOF'
import json, os, sys, re
from collections import Counter, defaultdict
sys.path.insert(0, os.environ["TS_SCRIPT_DIR"])
import echolib

# ---------- Cross-environment dispatch helpers ----------
ZCODE_SCRIPT = os.path.join(os.environ["TS_SCRIPT_DIR"], "zcode-adapter.py")

def _run_zcode(args):
    """Run zcode-adapter.py and return stdout."""
    import subprocess as _sp
    try:
        result = _sp.run([sys.executable, ZCODE_SCRIPT] + args,
                       capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except Exception:
        pass
    return ""

def _resolve_agent(path):
    if str(path).startswith("zcode://"):
        return "zcode"
    atype = echolib.detect_agent_type(path)
    return atype if atype in echolib.ADAPTER_REGISTRY else "claude"

def _dispatch_stats(path):
    if str(path).startswith("zcode://"):
        sid = str(path).replace("zcode://", "")
        result = _run_zcode(["session-stats", sid])
        return json.loads(result) if result else _empty_stats()
    agent = _resolve_agent(path)
    fn = echolib.ADAPTER_REGISTRY.get(agent, {}).get("session_stats")
    return fn(path) if fn else echolib.session_stats(path)

def _dispatch_tools(path, limit=30):
    if str(path).startswith("zcode://"):
        sid = str(path).replace("zcode://", "")
        result = _run_zcode(["extract-tools", sid, "--limit", str(limit)])
        return json.loads(result) if result else []
    agent = _resolve_agent(path)
    fn = echolib.ADAPTER_REGISTRY.get(agent, {}).get("extract_tools")
    if fn:
        if agent == "grok":
            from pathlib import Path as _P
            session_dir = str(_P(path).parent) if _P(path).is_file() else path
            return fn(session_dir, errors_only=False, limit=limit)
        return fn(path, errors_only=False, limit=limit)
    return echolib.extract_tools(path, errors_only=False, limit=limit)

def _dispatch_messages(path, role="user", limit=5):
    if str(path).startswith("zcode://"):
        sid = str(path).replace("zcode://", "")
        result = _run_zcode(["extract-messages", sid, "--role", role, "--limit", str(limit)])
        return json.loads(result) if result else []
    agent = _resolve_agent(path)
    fn = echolib.ADAPTER_REGISTRY.get(agent, {}).get("extract_messages")
    if fn:
        return fn(path, role=role, limit=limit, thinking_limit=0)
    return echolib.extract_messages(path, role=role, limit=limit)

def _empty_stats():
    return {
        "slug": "", "model": "", "branch": "",
        "started": "", "ended": "",
        "user_messages": 0, "assistant_messages": 0,
        "tool_calls": 0, "files_edited": 0, "errors": 0,
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_create_tokens": 0,
        "compactions": 0, "summary": "",
        "total_tokens": 0,
    }

def get_all_sessions(limit=500):
    """Cross-environment session listing."""
    all_sessions = echolib.cross_tool_list_sessions(limit=limit, keyword="")
    # Also try ZCode adapter if available
    _zcode_path = os.path.join(os.environ["TS_SCRIPT_DIR"], "zcode-adapter.py")
    if os.path.exists(_zcode_path):
        try:
            import subprocess as _sp
            result = _sp.run([sys.executable, _zcode_path, "list-sessions", "--limit", str(limit)],
                           capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and result.stdout.strip():
                zcode_sessions = json.loads(result.stdout)
                all_sessions.extend(zcode_sessions)
        except Exception:
            pass
    return all_sessions

# ---------- 主题分类映射 ----------
TOPIC_RULES = [
    {
        "id": 1, "name": "投资分析",
        "skill": "taxue-industry",
        "keywords": ["股票", "基金", "ETF", "财报", "PE", "PB", "ROE", "估值",
                      "买入", "卖出", "持有", "加仓", "减仓", "止损", "止盈"],
        "tools": ["eastmoney", "invest-", "financial-report", "stock-analysis",
                  "fund-", "fundfof", "fundscreen", "ttfund"],
        "code_pattern": r"\b[036]\d{5}\b",
        "stock_confirm_kw": ["股票", "基金", "ETF", "财报", "PE", "PB", "ROE", "估值"],
    },
    {
        "id": 2, "name": "内容创作",
        "skill": "taxue-content",
        "keywords": ["文章", "写作", "公众号", "小红书", "知乎", "标题", "排版",
                      "内容", "文案", "稿子", "发布", "配图"],
        "tools": ["content-alchemist", "huajiao-finance-writer", "dbs-content",
                  "wechat-humon-blogger", "taxue-content", "long-form-writing"],
        "code_pattern": None,
    },
    {
        "id": 3, "name": "技能开发",
        "skill": "taxue-skill",
        "keywords": ["skill", "技能", "模板", "触发词", "SKILL.md"],
        "tools": ["skill-creator", "tx-create", "taxue-skill", "taxue-meta",
                  "create-skills"],
        "code_pattern": None,
    },
    {
        "id": 4, "name": "学习",
        "skill": "taxue-learn",
        "keywords": ["学习", "读书", "想学", "入门", "笔记", "阅读", "教程",
                      "课程", "知识", "掌握"],
        "tools": ["taxue-learn", "taxue-weread"],
        "code_pattern": None,
    },
    {
        "id": 5, "name": "商业判断",
        "skill": "taxue-business",
        "keywords": ["项目", "副业", "值不值得", "商业模式", "创业", "变现",
                      "市场", "成本", "收入"],
        "tools": ["taxue-business"],
        "code_pattern": None,
    },
    {
        "id": 6, "name": "职业发展",
        "skill": "taxue-career",
        "keywords": ["简历", "面试", "Offer", "求职", "转行", "跳槽", "职业",
                      "招聘", "工作", "离职"],
        "tools": ["taxue-career", "interview-coach", "taxue-job-search"],
        "code_pattern": None,
    },
    {
        "id": 7, "name": "沟通表达",
        "skill": "taxue-relate",
        "keywords": ["谈判", "吵架", "汇报", "说服", "沟通", "关系", "冲突",
                      "话术", "加薪"],
        "tools": ["taxue-relate", "taxue-speak", "taxue-talk"],
        "code_pattern": None,
    },
    {
        "id": 8, "name": "流量增长",
        "skill": "taxue-traffic",
        "keywords": ["流量", "涨粉", "阅读量", "分发", "推荐", "曝光",
                      "增长", "粉丝"],
        "tools": ["taxue-traffic"],
        "code_pattern": None,
    },
    {
        "id": 9, "name": "系统运维",
        "skill": "taxue-meta",
        "keywords": ["环境", "配置", "报错", "安装", "部署", "升级", "网络",
                      "依赖", "Bug", "修复"],
        "tools": ["sys-doctor", "zcode-guide", "dbs-bridge", "dbs-hook"],
        "code_pattern": None,
    },
]

INPUT_RATE = 3.0
OUTPUT_RATE = 15.0

# 通用工具（不显示在"涉及"中）
GENERIC_TOOLS = frozenset({
    "Agent", "AskUserQuestion", "Bash", "BashOutput", "Edit", "EnterPlanMode",
    "ExitPlanMode", "Grep", "Glob", "LS", "Read", "Write", "Skill",
    "TaskCreate", "WebFetch", "WebSearch", "SendMessage", "TodoWrite",
    "TodoRead", "ReadSessionContext", "ComputerUse", "BrowserUse",
    "TaskOutput", "TaskUpdate", "TaskContinue",
})

def is_relevant_tool(name):
    if not name:
        return False
    if name in GENERIC_TOOLS:
        return False
    if name.startswith(("mcp__", "_", "zcode_")):
        return False
    return True

def estimate_cost(stats):
    inp = stats.get("input_tokens", 0)
    out = stats.get("output_tokens", 0)
    return (inp / 1_000_000 * INPUT_RATE) + (out / 1_000_000 * OUTPUT_RATE)

def classify_session(summary, first_prompt, tools_used_str):
    text = (summary + " " + first_prompt).lower()
    tools_lower = tools_used_str.lower()
    scores = []
    for rule in TOPIC_RULES:
        score = 0
        matched_kw = []
        matched_tools = []
        for kw in rule["keywords"]:
            if kw.lower() in text:
                score += 2
                matched_kw.append(kw)
        for tool in rule["tools"]:
            if tool.lower() in tools_lower:
                score += 3
                matched_tools.append(tool)
        if rule.get("code_pattern") and re.search(rule["code_pattern"], text):
            confirm_kw = rule.get("stock_confirm_kw", [])
            has_confirm = any(kw.lower() in text for kw in confirm_kw)
            if has_confirm:
                score += 3
                matched_kw.append("(股票代码)")
            else:
                score += 1
        if score > 0:
            scores.append((rule["id"], score, matched_kw, matched_tools))
    if not scores:
        return (0, 0, [], [])
    scores.sort(key=lambda x: x[1], reverse=True)
    return scores[0]

def classify_session_with_messages(session_path, stats, first_prompt):
    summary = stats.get("summary", "")
    tools_used = set()
    try:
        for t in _dispatch_tools(session_path, limit=30):
            name = t.get("name", "")
            if name:
                tools_used.add(name)
    except Exception:
        pass
    user_texts = []
    try:
        for m in _dispatch_messages(session_path, role="user", limit=5):
            txt = m.get("text", "")
            if txt and len(txt) > 20:
                user_texts.append(txt[:500])
    except Exception:
        pass
    additional_text = " ".join(user_texts) if user_texts else ""
    combined_text = summary + " " + first_prompt + " " + additional_text
    tools_str = ", ".join(sorted(tools_used))
    topic_id, confidence, kws, tools_matched = classify_session(
        summary, first_prompt + " " + additional_text, tools_str
    )
    stock_codes = set()
    for match in re.finditer(r"\b\d{6}\b", combined_text):
        code = match.group()
        if code.startswith(("0", "3", "6")):
            stock_codes.add(code)
    return {
        "topic_id": topic_id,
        "confidence": confidence,
        "matched_keywords": kws,
        "matched_tools": tools_matched,
        "tools_used": list(tools_used)[:10],
        "stock_codes": list(stock_codes)[:5],
    }

# Apply days filter
DAYS_LIMIT = int(os.environ.get("TS_DAYS", "30"))
since_date = ""
if DAYS_LIMIT > 0:
    from datetime import datetime, timezone, timedelta
    since_date = (datetime.now(timezone.utc) - timedelta(days=DAYS_LIMIT)).strftime("%Y-%m-%d")

# Scan sessions (cross-environment)
all_sessions = get_all_sessions(limit=200)
if since_date:
    all_sessions = [s for s in all_sessions if (s.get("created") if isinstance(s, dict) else s.created) and str((s.get("created") if isinstance(s, dict) else s.created))[:10] >= since_date]

topic_stats = defaultdict(lambda: {
    "count": 0, "total_cost": 0.0, "total_tokens": 0,
    "tools": set(), "start": None, "end": None,
    "stock_codes": set(), "subjects": set(),
})

for meta in all_sessions:
    path = meta.get("full_path", "") if isinstance(meta, dict) else meta.full_path
    if not path or (not str(path).startswith("zcode://") and not os.path.exists(path)):
        continue
    try:
        stats = _dispatch_stats(path)
    except Exception:
        continue
    cost = estimate_cost(stats)
    first_prompt = meta.get("first_prompt", "") if isinstance(meta, dict) else (meta.first_prompt or "")
    classification = classify_session_with_messages(path, stats, first_prompt)
    tid = classification["topic_id"]
    ts = topic_stats[tid]
    ts["count"] += 1
    ts["total_cost"] += cost
    ts["total_tokens"] += stats.get("total_tokens", 0)
    ts["tools"].update(t for t in classification["tools_used"] if is_relevant_tool(t))
    if classification["stock_codes"]:
        ts["stock_codes"].update(classification["stock_codes"])
    if stats.get("started"):
        if not ts["start"] or stats["started"] < ts["start"]:
            ts["start"] = stats["started"]
    if stats.get("ended"):
        if not ts["end"] or stats["ended"] > ts["end"]:
            ts["end"] = stats["ended"]

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

# Determine format
fmt = os.environ.get("TS_FORMAT", "text")

if fmt == "json":
    # JSON output
    output = {
        "total_sessions": total_sessions,
        "total_cost": round(total_cost, 2),
        "topics": []
    }
    for tid, st in sorted_topics:
        rule = next((r for r in TOPIC_RULES if r["id"] == tid), None)
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
    # Text output
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
        rule = next((r for r in TOPIC_RULES if r["id"] == tid), None)
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
import json, os, sys, re
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
export TS_FORMAT="$FORMAT"
export TS_DAYS="$DAYS"

if [[ -n "$SESSION_FILE" ]]; then
  analyze_single_session "$SESSION_FILE"
elif [[ -n "$SHOW_TOPIC" ]]; then
  extract_topic_package "$SHOW_TOPIC"
else
  generate_overview
fi
