#!/usr/bin/env python3
"""
topic-classify.py — Shared topic classification + cross-environment dispatch
for topic-scan.sh.

Extracted from topic-scan.sh to eliminate ~400 lines of duplicated Python
heredoc code between the extract_topic_package and generate_overview modes.

Provides:
    - TOPIC_RULES, GENERIC_TOOLS, rate constants
    - is_relevant_tool(), estimate_cost()
    - classify_session(), classify_session_with_messages()
    - get_all_sessions()  (cross-environment + ZCode)
    - dispatch_stats/tools/messages  (echolib + ZCode DB)

Usage from topic-scan.sh heredocs:
    sys.path.insert(0, os.environ["TS_SCRIPT_DIR"])
    import topic_classify as tc
"""

import json
import os
import re
import sys
from collections import Counter

# echolib lives in the same scripts/ directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import echolib

from index_builder._evidence import EVIDENCE_TEXT_CAP
from index_builder._reader import fresh_session_bundles, is_virtual_path


# ---------------------------------------------------------------------------
# 虚拟路径：不是磁盘路径
# ---------------------------------------------------------------------------

def _is_virtual(path):
    """判据的唯一真源在 index_builder._reader.is_virtual_path。"""
    return is_virtual_path(path)


# ---------------------------------------------------------------------------
# 索引优先：一次批量读，替代「每会话解析三遍」
# ---------------------------------------------------------------------------
# 下面三个 dispatch 原本各自走适配器全量解析（stats / tools / messages）。
# 200 个会话实测 7.5 s，而同样的字段在索引里 0.02 s 就能取回——差 300 倍。
# 「索引优先」不该是每个调用方的自觉，而是这一层的前置契约：批量读一次，
# 命中就用，未命中（索引没有、或文件在构建之后又被写过）再落回适配器，
# 与从前逐字节一致。
_BUNDLES: dict = {}
_BUNDLES_ASKED: set = set()


def _bundle(path):
    """这个路径的索引行；索引答不了就 None（调用方落回解析）。"""
    path = str(path)
    if path not in _BUNDLES_ASKED:
        # 未经 prime_index_bundles 直接调用时按需补一次单路径查询，
        # 而不是整轮退化成解析。
        _BUNDLES_ASKED.add(path)
        _BUNDLES.update(fresh_session_bundles([path]))
    return _BUNDLES.get(path)


def prime_index_bundles(paths):
    """批量预热：一轮 topic-scan 只查一次索引。返回命中数。"""
    paths = [str(p) for p in paths if p]
    _BUNDLES_ASKED.update(paths)
    _BUNDLES.update(fresh_session_bundles(paths))
    return len(_BUNDLES)


def _cost_ready(bundle):
    """索引行是否带得起成本估算。

    v5 之前构建的行里 input/output 两腿都是 0（列存在，但从未被写入），拿它
    算成本会得到 $0.00 —— 一个安静的错误答案。分腿缺失时按「索引答不了」
    处理，落回解析；下一次构建填上分腿后，这条路径自动转为走索引。
    """
    if not bundle:
        return False
    legs = (bundle.get("input_tokens") or 0) + (bundle.get("output_tokens") or 0)
    return legs > 0 or not (bundle.get("total_tokens") or 0)


def _stats_from_bundle(b):
    """索引行 → 适配器形状的 stats（键名与 echolib 的 stats 契约一致）。"""
    return {
        "started": b.get("created") or "",
        "ended": b.get("modified") or "",
        "user_messages": b.get("user_messages") or 0,
        "assistant_messages": b.get("assistant_messages") or 0,
        "tool_calls": b.get("tool_calls") or 0,
        "errors": b.get("errors") or 0,
        "total_tokens": b.get("total_tokens") or 0,
        "input_tokens": b.get("input_tokens") or 0,
        "output_tokens": b.get("output_tokens") or 0,
        "cache_read_tokens": b.get("cache_read_tokens") or 0,
        "cache_create_tokens": b.get("cache_write_tokens") or 0,
        "branch": b.get("branch") or "",
        "summary": b.get("summary") or "",
        "model": b.get("model") or "",
        "first_prompt": b.get("first_prompt") or "",
    }


def _user_messages_from_bundle(b, limit):
    """证据投影 → 适配器形状的用户消息。

    投影为了显示把每条文本截到 ``EVIDENCE_TEXT_CAP``（300 字符），而分类要把
    前 500 字符拼起来做关键词匹配。只要有任何一条被截断，这份投影对分类就是
    有损的——返回 None 让调用方去读文件，而不是拿半句话去猜主题。
    """
    try:
        proj = json.loads(b.get("user_evidence_json") or "")
    except (ValueError, TypeError):
        return None
    if not isinstance(proj, list):
        return None
    out = []
    for m in proj[:limit]:  # 与适配器的 limit 窗口对齐
        if not isinstance(m, dict):
            continue
        text = m.get("text") or ""
        if len(text) >= EVIDENCE_TEXT_CAP:
            return None  # 有损：这条被截断过
        out.append({"role": "USER", "timestamp": m.get("ts") or "", "text": text})
    return out


# ---------------------------------------------------------------------------
# ZCode helpers — direct echolib calls (no subprocess)
# ---------------------------------------------------------------------------

def _is_zcode(path):
    return str(path).startswith("zcode://")


def _zcode_sid(path):
    return str(path).replace("zcode://", "")


# ---------------------------------------------------------------------------
# Unified dispatch: echolib adapters + ZCode
# ---------------------------------------------------------------------------

def dispatch_stats(path):
    """Get session stats for any environment."""
    b = _bundle(path)
    if b and _cost_ready(b):
        return _stats_from_bundle(b)
    if _is_zcode(path):
        return echolib.zcode_db_session_stats(_zcode_sid(path))
    return echolib.dispatch_session_stats(path)


def dispatch_tools(path, limit=30):
    """Extract tool calls for any environment."""
    b = _bundle(path)
    if b is not None:
        # tool_usage_json 由构建器逐会话写入；实测 6277 行里没有一行出现
        # 「tool_calls>0 而 usage 为空」，所以空 usage 就是「这个会话没有工具
        # 调用」，可以直接当答案。
        try:
            usage = json.loads(b.get("tool_usage_json") or "{}")
        except (ValueError, TypeError):
            usage = {}
        if isinstance(usage, dict):
            return [{"name": name, "status": "ok"}
                    for name in list(usage)[:limit]]
    if _is_zcode(path):
        return echolib.zcode_db_extract_tools(_zcode_sid(path), limit=limit)
    return echolib.dispatch_extract_tools(path, errors_only=False, limit=limit)


def dispatch_messages(path, role="user", limit=5):
    """Extract messages for any environment."""
    if str(role) == "user":
        b = _bundle(path)
        if b is not None:
            msgs = _user_messages_from_bundle(b, limit)
            if msgs is not None:
                return msgs
    if _is_zcode(path):
        return echolib.zcode_db_extract_messages(_zcode_sid(path), role=role, limit=limit)
    return echolib.dispatch_extract_messages(path, role=role, limit=limit)


# ---------------------------------------------------------------------------
# Session listing
# ---------------------------------------------------------------------------

def get_all_sessions(limit=500):
    """Cross-environment session listing including ZCode."""
    all_sessions = echolib.cross_tool_list_sessions(limit=limit, keyword="")
    # Also include ZCode sessions from SQLite
    try:
        zcode_sessions = echolib.zcode_db_list_sessions(limit=limit)
        all_sessions.extend(zcode_sessions)
    except Exception:
        pass
    # 两个源现在都会给出 zcode 会话：cross 的公平轮转给它约 1/N 的席位（保证它
    # 在总览里不被别的环境挤没），zcode_db_list_sessions 直取最近 limit 个（保证
    # 最近的历史不受席位限制）。两者都要，代价是同一会话出现两次——实测未去重
    # 时 400 条里有 26 条重复，会把这些会话的主题计数与成本翻倍。按路径去重，
    # 保留先出现的那个。
    seen = set()
    deduped = []
    for s in all_sessions:
        path = (s.get("full_path") or s.get("path", "")) if isinstance(s, dict) \
            else (getattr(s, "full_path", "") or getattr(s, "path", ""))
        if path:
            if path in seen:
                continue
            seen.add(path)
        deduped.append(s)
    return deduped


# ---------------------------------------------------------------------------
# Topic classification rules
# ---------------------------------------------------------------------------

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
        "tools": ["content-alchemist", "huajiao-finance-writer",
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
        "tools": ["sys-doctor", "zcode-guide"],
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


def classify_session(summary, first_prompt, tools_used_str):
    """Classify a session into a topic category.

    Returns (topic_id, confidence, matched_keywords, matched_tools).
    """
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
    """Enhanced classification: also scan messages for better detection.

    Returns dict with topic_id, confidence, matched_keywords, matched_tools,
    tools_used, stock_codes.
    """
    summary = stats.get("summary", "")

    # Collect tool names used in session
    tools_used = set()
    try:
        for t in dispatch_tools(session_path, limit=30):
            name = t.get("name", "")
            if name:
                tools_used.add(name)
    except Exception:
        pass

    # Also check user messages for deeper classification
    user_texts = []
    try:
        for m in dispatch_messages(session_path, role="user", limit=5):
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


def topic_rule(topic_id):
    """Look up a TOPIC_RULES entry by id. Returns None if not found."""
    return next((r for r in TOPIC_RULES if r["id"] == topic_id), None)


def scan_and_classify(limit=500, since_date=""):
    """Scan all sessions, classify, and group by topic.

    Returns (topic_sessions, topic_stats) where:
      topic_sessions: dict {topic_id: [session_detail_dict, ...]}
      topic_stats: dict {topic_id: aggregate_stats_dict}

    Each session_detail dict has: path, stats, cost, first_prompt,
    classification, topic_name, skill.
    """
    from collections import defaultdict

    all_sessions = get_all_sessions(limit=limit)
    # 批量预热索引：后面每个会话的 stats/tools/messages 都从这一份 map 里取，
    # 取不到才解析文件。放在过滤之前，因为它与 since_date 无关。
    prime_index_bundles(
        (s.get("full_path") or s.get("path", "")) if isinstance(s, dict)
        else getattr(s, "full_path", "")
        for s in all_sessions
    )
    if since_date:
        all_sessions = [
            s for s in all_sessions
            if (s.get("created") if isinstance(s, dict) else s.created)
            and str(s.get("created") if isinstance(s, dict) else s.created)[:10] >= since_date
        ]

    topic_sessions = defaultdict(list)
    topic_stats = defaultdict(lambda: {
        "count": 0, "total_cost": 0.0, "total_tokens": 0,
        "tools": set(), "start": None, "end": None,
        "stock_codes": set(), "subjects": set(),
    })

    for meta in all_sessions:
        path = meta.get("full_path") or meta.get("path", "") if isinstance(meta, dict) else meta.full_path
        if not path:
            continue
        if not _is_virtual(path) and not os.path.exists(path):
            continue

        try:
            stats = dispatch_stats(path)
        except Exception:
            continue

        cost = estimate_cost(stats)
        first_prompt = (meta.get("first_prompt", "") if isinstance(meta, dict)
                        else (meta.first_prompt or ""))
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

        rule = topic_rule(tid)
        topic_sessions[tid].append({
            "path": path,
            "stats": stats,
            "cost": round(cost, 2),
            "first_prompt": first_prompt[:200],
            "classification": classification,
            "topic_name": rule["name"] if rule else "未分类",
            "skill": rule["skill"] if rule else "taxue-solve",
        })

    return topic_sessions, topic_stats
