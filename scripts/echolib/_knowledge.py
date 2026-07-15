import json
import os
import re
from pathlib import Path

from echolib._claude import (
    _normalize_timestamp,
    extract_messages,
    extract_tools,
)
from echolib._helpers import (
    _iter_jsonl,
)
_CORRECTION_PATTERNS = re.compile(
    r"\b(no[,.]?\s+(?:don'?t|not|stop|wrong|instead))|"
    r"\b(don'?t\s+\w+)|"
    r"\b(stop\s+doing)|"
    r"\b(that'?s\s+(?:wrong|incorrect|not right))",
    re.IGNORECASE
)

_APPROVAL_PATTERNS = re.compile(
    r"\b(perfect|exactly|great|yes[,.]?\s+(?:that'?s|keep|do it)|works|looks good|nice)",
    re.IGNORECASE
)

_IMPERATIVE_PATTERNS = re.compile(
    r"\b(always|never|must|do not|don'?t ever|every time|make sure)",
    re.IGNORECASE
)

_URL_PATTERN = re.compile(r"https?://[^\s\)\"'>]+")

_VALUE_PATTERNS = re.compile(
    r"\b(\w+\s+(?:is|are)\s+(?:better|more important|more valuable|preferable)\s+(?:than|over|to)\s+)|"
    r"\b(prefer\s+\w+\s+(?:over|to|instead of)\s+)|"
    r"\b(prioritize\s+\w+\s+over\s+)|"
    r"\b(\w+\s+(?:matters?|trumps?|outweighs?|beats?)\s+(?:more than\s+)?)|"
    r"\b(choose\s+\w+\s+over\s+)|"
    r"\b((?:the )?most (?:important|valuable|useful|durable)\s+(?:\w+\s+)?(?:is|are)\s+)|"
    r"\b(rather\s+\w+\s+than\s+)|"
    r"\b(\w+\s+>\s+\w+)",
    re.IGNORECASE
)


def extract_knowledge(session_path):
    """
    Two-pass knowledge extraction from a session.

    Pass 1: Scan tool calls for decisions (AskUserQuestion) and errors.
    Pass 2: Scan messages for corrections, patterns, references, values.

    Yields dicts with keys: category, content, timestamp,
                           suggested_destination, suggested_type.
    """
    items = []

    # Pass 1: Tool calls
    for tool in extract_tools(session_path):
        if tool["name"] == "AskUserQuestion":
            items.append({
                "category": "decision",
                "content": "Question: %s | Answer: %s" % (
                    tool.get("key_input", "")[:200],
                    tool.get("result_preview", "")[:200]
                ),
                "timestamp": tool.get("timestamp", ""),
                "suggested_destination": "memory",
                "suggested_type": "project",
            })
        elif tool.get("status") == "error":
            items.append({
                "category": "lesson",
                "content": "Tool %s failed: %s" % (
                    tool["name"],
                    tool.get("result_preview", "")[:200]
                ),
                "timestamp": tool.get("timestamp", ""),
                "suggested_destination": "skip",
                "suggested_type": None,
            })

    # Pass 2: Messages
    prev_assistant_text = ""
    for msg in extract_messages(session_path, role="both"):
        text = msg.get("text", "")
        if not text or len(text) < 5:
            if msg.get("role") == "ASSISTANT":
                prev_assistant_text = text or ""
            continue

        if msg.get("role") == "ASSISTANT":
            prev_assistant_text = text[:500]
            continue

        # User messages below
        if _VALUE_PATTERNS.search(text):
            items.append({
                "category": "value",
                "content": text[:300],
                "timestamp": msg.get("timestamp", ""),
                "suggested_destination": "memory",
                "suggested_type": "value",
            })

        if _CORRECTION_PATTERNS.search(text):
            dest = "claude_md" if _IMPERATIVE_PATTERNS.search(text) else "memory"
            items.append({
                "category": "correction",
                "content": text[:300],
                "timestamp": msg.get("timestamp", ""),
                "suggested_destination": dest,
                "suggested_type": "feedback",
            })

        if _APPROVAL_PATTERNS.search(text) and prev_assistant_text:
            items.append({
                "category": "pattern",
                "content": "Approach approved: %s" % prev_assistant_text[:200],
                "timestamp": msg.get("timestamp", ""),
                "suggested_destination": "memory",
                "suggested_type": "feedback",
            })

        for url in _URL_PATTERN.findall(text):
            items.append({
                "category": "reference",
                "content": "URL mentioned: %s" % url,
                "timestamp": msg.get("timestamp", ""),
                "suggested_destination": "memory",
                "suggested_type": "reference",
            })

    # Deduplicate by content prefix
    seen = set()
    for item in items:
        key = item["content"][:80]
        if key not in seen:
            seen.add(key)
            yield item

def _summary_path_for(session_path):
    """推导摘要文件路径：原始文件同目录下 .summary.jsonl"""
    return session_path + ".summary.jsonl"

def save_analysis_result(session_path, analysis, query_intent,
                         agent_type="claude", source_mtime=None,
                         memory_tier="periodic", excluded=None):
    """
    分析完成后调用，把结果存为摘要（追加模式）。

    同一会话可能被多次分析（不同角度），每次追加一条记录。

    Args:
        session_path: 原始会话文件路径
        analysis: LLM 的分析输出文本
        query_intent: 本次查询意图（如 "投资"、"skill优化"）
        agent_type: 来源环境类型
        source_mtime: 原始文件 mtime（用于新鲜度判断）
        memory_tier: 时效等级（借鉴 taxue-save）
            permanent: 认知规律、思维模型 → 永不遗忘
            periodic:  偏好、阶段性结论 → 7天后不再注入（旧偏好不如没有偏好）
            once:      临时上下文、单次任务 → 24小时后失效
        excluded: 已否决方向列表（借鉴 dbs-save）
            如 ["Rust 不适合因为零依赖是核心优势", "方案C 成本过高"]
    """
    if source_mtime is None:
        source_mtime = os.path.getmtime(session_path)

    summary_path = _summary_path_for(session_path)
    record = {
        "schema": "session-digger-summary/v1",
        "session_id": os.path.basename(session_path).replace(".jsonl", ""),
        "source_path": session_path,
        "source_agent": agent_type,
        "source_mtime": source_mtime,
        "analyzed_at": _time.strftime("%Y-%m-%dT%H:%M:%S"),
        "query_intent": query_intent,
        "analysis": analysis,
        "memory_tier": memory_tier,
        "excluded": excluded or [],
    }

    with open(summary_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return summary_path

_TIER_ONCE_TTL = 86400       # once: 24小时

_TIER_PERIODIC_TTL = 604800  # periodic: 7天

def load_analysis_result(session_path, query_intent=None):
    """
    读取已存储的分析结果。

    Args:
        session_path: 原始会话文件路径
        query_intent: 可选，按意图过滤（子串匹配）

    Returns:
        list[dict]: 匹配的分析记录列表。空列表表示无摘要或已过期。

    过滤规则（三重过滤）：
        1. 新鲜度：原始文件 mtime > 摘要记录的 source_mtime → 跳过
        2. 时效分层：
           permanent → 永不因时间过期
           periodic  → 超过 7 天不再注入（旧偏好不如没有偏好）
           once      → 超过 24 小时不再注入
        3. 意图过滤：query_intent 子串匹配
    """
    summary_path = _summary_path_for(session_path)
    if not os.path.exists(summary_path):
        return []

    raw_mtime = os.path.getmtime(session_path)
    now = _time.time()
    results = []

    with open(summary_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            # 过滤 1: 新鲜度检查 — 原始文件更新过则跳过
            if rec.get("source_mtime", 0) < raw_mtime:
                continue

            # 过滤 2: 时效分层 — 基于分析时间，非会话时间
            tier = rec.get("memory_tier", "periodic")
            analyzed_at = rec.get("analyzed_at", "")
            if analyzed_at:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(analyzed_at)
                    rec_age = now - dt.timestamp()
                except (ValueError, OSError):
                    rec_age = now - rec.get("source_mtime", now)
            else:
                rec_age = now - rec.get("source_mtime", now)
            if tier == "once" and rec_age > _TIER_ONCE_TTL:
                continue
            elif tier == "periodic" and rec_age > _TIER_PERIODIC_TTL:
                continue
            # permanent 不过滤

            # 过滤 3: 意图过滤
            if query_intent:
                stored_intent = rec.get("query_intent", "")
                if query_intent.lower() not in stored_intent.lower():
                    continue

            results.append(rec)

    return results

def has_fresh_summary(session_path):
    """快速判断是否存在新鲜摘要（不读取内容，仅 mtime 对比）"""
    summary_path = _summary_path_for(session_path)
    if not os.path.exists(summary_path):
        return False
    return os.path.getmtime(summary_path) >= os.path.getmtime(session_path)

def build_summary_index(scopes=None):
    """
    扫描所有环境，构建已分析会话的归档索引。

    Args:
        scopes: 可选，限定扫描的环境列表。默认全部。

    Returns:
        dict: 归档索引 JSON 结构
    """
    import glob

    index = {
        "schema": "session-digger-archive-index/v1",
        "generated_at": _time.strftime("%Y-%m-%dT%H:%M:%S"),
        "analyzed_sessions": [],
        "stats": {"total": 0, "by_agent": {}, "by_intent": {}},
    }

    search_paths = [
        (os.path.expanduser("~/.claude/projects"), "claude"),
        (os.path.expanduser("~/.grok/sessions"), "grok"),
        (os.path.expanduser("~/.kimi-code/sessions"), "kimi_code"),
        (os.path.expanduser("~/.codex/sessions"), "codex"),
    ]

    if scopes:
        search_paths = [(p, a) for p, a in search_paths if a in scopes]

    for base_path, agent_type in search_paths:
        if not os.path.exists(base_path):
            continue

        for summary_file in glob.glob(
            os.path.join(base_path, "**", "*.summary.jsonl"), recursive=True
        ):
            with open(summary_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    source_path = rec.get("source_path", "")
                    fresh = False
                    if source_path and os.path.exists(source_path):
                        fresh = os.path.getmtime(summary_file) >=                                 os.path.getmtime(source_path)

                    entry = {
                        "session_id": rec.get("session_id", ""),
                        "source_agent": rec.get("source_agent", agent_type),
                        "source_path": source_path,
                        "summary_path": summary_file,
                        "analyzed_at": rec.get("analyzed_at", ""),
                        "query_intent": rec.get("query_intent", ""),
                        "is_fresh": fresh,
                    }
                    index["analyzed_sessions"].append(entry)
                    index["stats"]["total"] += 1

                    agent = entry["source_agent"]
                    index["stats"]["by_agent"][agent] =                         index["stats"]["by_agent"].get(agent, 0) + 1

                    intent = entry["query_intent"]
                    if intent:
                        index["stats"]["by_intent"][intent] =                             index["stats"]["by_intent"].get(intent, 0) + 1

    return index

def save_summary_index(index, index_path=None):
    """保存归档索引到文件"""
    if index_path is None:
        index_path = os.path.expanduser(
            "~/.claude/.session-digger-archive-index.json"
        )
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    return index_path
