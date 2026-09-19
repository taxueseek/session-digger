#!/usr/bin/env python3
"""
Atomic analysis engine (rule + stats, no ML deps).

Capabilities:
  segment / topics / sentiment / decisions / profiles / graph /
  ranking / keywords / activity / reciprocity / pipeline

ANALYSIS_MODES 表驱动：一行扩展一种分析组合（对齐 session-digger 表驱动哲学）。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional


POSITIVE_WORDS = {
    "好", "棒", "优秀", "厉害", "完美", "不错", "可以", "同意", "支持", "赞",
    "喜欢", "爱", "开心", "高兴", "快乐", "哈哈", "嘿嘿", "期待", "感谢", "谢谢",
    "辛苦", "加油", "成功", "搞定", "漂亮", "精彩", "牛逼", "nice", "good", "great",
    "awesome", "thanks", "👍", "🎉", "💪", "🔥", "✨", "❤️",
}
NEGATIVE_WORDS = {
    "差", "烂", "糟糕", "烦", "无语", "失望", "难过", "生气", "愤怒", "讨厌",
    "崩溃", "头疼", "不行", "不好", "反对", "拒绝", "问题", "错误", "失败",
    "bug", "故障", "挂了", "垃圾", "bad", "terrible", "hate", "fail", "error",
    "😡", "😤", "😭", "👎",
}
NEGATION = {"不", "没", "没有", "不是", "别", "未", "无", "not", "no", "don't"}
DECISION_HINTS = {
    "结论", "决定", "确定", "定下来", "就这样", "最终", "达成共识", "通过",
    "定了", "方案是", "计划是", "安排", "分工", "负责", "执行", "OK", "ok",
}
DEAL_HINTS = {
    "预算", "报价", "费用", "多少钱", "报价单", "名额", "brief", "Brief", "截止",
    "结算", "商单", "投放", "甲方", "乙方", "合作", "返点", "稿费", "尾款", "定金",
    "加热", "接龙", "投放排期",
}
COMMIT_HINTS = {
    "明天给你", "晚点发", "回头发", "明天发", "下周给", "记得", "答应", "承诺",
    "别忘", "到时候", "我整理好", "发你", "同步你", "尽快",
}
QUESTION_MARKS = ("？", "?")


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    # Chinese chars as unigrams + english words
    chars = re.findall(r"[\u4e00-\u9fff]", text)
    words = re.findall(r"[A-Za-z0-9_]{2,}", text.lower())
    # bigrams for chinese
    bigrams = [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]
    return chars + bigrams + words


def simhash(text: str, bits: int = 64) -> int:
    tokens = _tokenize(text)
    if not tokens:
        return 0
    vector = [0] * bits
    for token in tokens:
        h = hash(token) & ((1 << bits) - 1)
        for i in range(bits):
            vector[i] += 1 if (h & (1 << i)) else -1
    out = 0
    for i, v in enumerate(vector):
        if v > 0:
            out |= 1 << i
    return out


def _popcount(n: int) -> int:
    """Population count, portable across Python 3.8+.

    ``int.bit_count()`` exists only on Python 3.10+. This skill documents
    plain ``python3`` invocations and is routinely run on the macOS system
    interpreter (3.9), so the fallback below is the common path, not a rare
    compatibility branch. Semantics are identical.
    """
    if hasattr(int, "bit_count"):  # Python 3.10+
        return n.bit_count()
    return bin(n).count("1")


def hamming(a: int, b: int) -> int:
    """Hamming distance between two simhash values."""
    return _popcount(a ^ b)


def segment_messages(messages: list[dict], threshold: int = 22) -> list[dict]:
    """C1: topic-boundary segmentation via simhash distance."""
    if not messages:
        return []
    segments = []
    cur: list[dict] = []
    cur_hash = None
    seg_id = 0
    for msg in messages:
        text = msg.get("text") or ""
        h = simhash(text)
        if not cur:
            cur = [msg]
            cur_hash = h
            continue
        dist = hamming(cur_hash or 0, h)
        # also break on large time gaps (> 2h)
        gap = False
        if cur[-1].get("ts") and msg.get("ts"):
            gap = abs(int(msg["ts"]) - int(cur[-1]["ts"])) > 7200
        if dist > threshold or gap:
            segments.append(_pack_segment(seg_id, cur))
            seg_id += 1
            cur = [msg]
            cur_hash = h
        else:
            cur.append(msg)
            # rolling mix
            cur_hash = ((cur_hash or 0) & h) | (((cur_hash or 0) | h) & ((1 << 32) - 1))
    if cur:
        segments.append(_pack_segment(seg_id, cur))
    return segments


def _pack_segment(seg_id: int, msgs: list[dict]) -> dict:
    texts = " ".join((m.get("text") or "")[:80] for m in msgs[:5])
    return {
        "id": f"seg-{seg_id}",
        "messages": msgs,
        "messageCount": len(msgs),
        "topicHint": texts[:60].replace("\n", " "),
    }


def extract_topics(segments: list[dict], max_topics: int = 8) -> list[dict]:
    """C2: lightweight TF-style clustering of segment hints."""
    if not segments:
        return []
    # bag of bigrams per segment（停用词过滤，防「的/了」类虚词霸榜话题标题）
    docs = []
    for seg in segments:
        blob = " ".join((m.get("text") or "") for m in seg.get("messages", []))
        docs.append(Counter(t for t in _tokenize(blob) if len(t) >= 2 and t not in STOPWORDS))

    df = Counter()
    for d in docs:
        for t in d:
            df[t] += 1
    n = len(docs)
    # score terms
    term_scores = Counter()
    for d in docs:
        for t, c in d.items():
            if len(t) < 2:
                continue
            idf = math.log((n + 1) / (df[t] + 1)) + 1
            term_scores[t] += c * idf

    # assign each segment to top overlapping terms cluster
    seed_terms = [t for t, _ in term_scores.most_common(max_topics * 3)]
    clusters: dict[str, list[int]] = defaultdict(list)
    for i, d in enumerate(docs):
        best = None
        best_s = 0
        for t in seed_terms:
            if d.get(t, 0) > best_s:
                best_s = d[t]
                best = t
        clusters[best or f"话题{i+1}"].append(i)

    topics = []
    total_msgs = sum(s.get("messageCount", 0) for s in segments) or 1
    for tid, (title, idxs) in enumerate(sorted(clusters.items(), key=lambda x: -sum(segments[i]["messageCount"] for i in x[1]))):
        if tid >= max_topics:
            break
        msgs = []
        parts = set()
        for i in idxs:
            msgs.extend(segments[i].get("messages", []))
            parts.update(segments[i].get("participants") or [])
        key_msgs = [m.get("text", "")[:100] for m in msgs if m.get("text")][:3]
        topics.append({
            "id": f"topic-{tid}",
            "title": title if not title.startswith("话题") else (key_msgs[0][:20] if key_msgs else title),
            "weight": len(msgs) / total_msgs,
            "messageCount": len(msgs),
            "keyMessages": key_msgs,
            "participants": sorted(parts),
        })
    return topics


def analyze_sentiment(segments: list[dict], granularity: str = "per-segment") -> dict:
    """C3: lexicon sentiment."""
    overall = Counter()
    seg_out = []
    trend = defaultdict(lambda: Counter())

    # 情绪打分统一走模块级 score_text（消除词表逻辑双份维护）

    for seg in segments:
        labels = []
        for m in seg.get("messages", []):
            label, sc = score_text(m.get("text") or "")
            overall[label] += 1
            labels.append(label)
            ts = m.get("ts")
            if ts:
                day = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
                trend[day][label] += 1
            if granularity == "per-message":
                m["sentiment"] = label
        # majority
        if not labels:
            lab = "neutral"
        else:
            lab = Counter(labels).most_common(1)[0][0]
        seg_out.append({"segmentId": seg.get("id"), "label": lab, "score": labels.count("positive") - labels.count("negative")})

    total = sum(overall.values()) or 1
    trend_list = []
    for day in sorted(trend):
        c = trend[day]
        t = sum(c.values()) or 1
        trend_list.append({
            "date": day,
            "score": (c["positive"] - c["negative"]) / t,
            "positive": c["positive"],
            "negative": c["negative"],
            "neutral": c["neutral"],
        })
    return {
        "overall": {
            "positive": overall["positive"] / total,
            "neutral": overall["neutral"] / total,
            "negative": overall["negative"] / total,
            "counts": dict(overall),
        },
        "segments": seg_out,
        "trend": trend_list,
    }


def extract_decisions(segments: list[dict]) -> list[dict]:
    """C6: decision extraction by keyword windows."""
    out = []
    for seg in segments:
        for m in seg.get("messages", []):
            text = m.get("text") or ""
            if any(h in text for h in DECISION_HINTS):
                out.append({
                    "text": text[:200],
                    "sender": m.get("nickname") or m.get("sender"),
                    "timestamp": m.get("timestamp"),
                    "segmentId": seg.get("id"),
                    "confidence": "medium" if len(text) > 8 else "low",
                })
    return out


def build_profiles(messages: list[dict]) -> list[dict]:
    """C4: speaker profiles."""
    by_user: dict[str, list[dict]] = defaultdict(list)
    for m in messages:
        key = m.get("nickname") or m.get("sender") or "未知"
        by_user[key].append(m)

    profiles = []
    for name, msgs in sorted(by_user.items(), key=lambda x: -len(x[1])):
        texts = [m.get("text") or "" for m in msgs]
        joined = " ".join(texts)
        avg_len = sum(len(t) for t in texts) / max(len(texts), 1)
        q_count = sum(1 for t in texts if "?" in t or "？" in t)
        role = "活跃成员"
        if len(msgs) >= max(5, 0.2 * len(messages)):
            role = "核心人物"
        if q_count > len(msgs) * 0.3:
            role = "提问者"
        if avg_len > 40:
            role = "长文输出者"
        # classic quotes
        quotes = sorted([t for t in texts if 6 <= len(t) <= 80], key=len, reverse=True)[:3]
        terms = [t for t, _ in Counter(_tokenize(joined)).most_common(20) if len(t) >= 2][:5]
        profiles.append({
            "nickname": name,
            "messageCount": len(msgs),
            "roleLabel": role,
            "avgLength": round(avg_len, 1),
            "focusTerms": terms,
            "quotes": quotes,
            "digestSummary": f"发言 {len(msgs)} 条，风格偏{'长文' if avg_len > 30 else '短句'}，关注 {('、'.join(terms[:3]) if terms else '综合')}",
        })
    return profiles


# 停用词：关键词/词云层（吸收 welink 词云精华，无外部分词依赖）
STOPWORDS = {
    "的", "了", "是", "在", "我", "你", "他", "她", "们", "就", "都", "也", "和", "与",
    "有", "没", "不", "很", "太", "啊", "呀", "吧", "呢", "吗", "哦", "哈", "嗯",
    "这", "那", "什么", "怎么", "一个", "一下", "可以", "还是", "因为", "所以",
    "the", "a", "an", "to", "of", "and", "is", "it", "for", "in", "on",
}


def speaker_ranking(messages: list[dict], top_n: int = 15) -> list[dict]:
    """发言排行榜（吸收 welink「活跃排行」精华）。"""
    counts = Counter()
    chars = Counter()
    for m in messages:
        name = m.get("nickname") or m.get("sender") or "未知"
        counts[name] += 1
        chars[name] += len(m.get("text") or "")
    total = sum(counts.values()) or 1
    out = []
    for i, (name, n) in enumerate(counts.most_common(top_n), 1):
        out.append({
            "rank": i,
            "nickname": name,
            "messageCount": n,
            "share": round(n / total, 4),
            "charCount": chars[name],
            "avgLength": round(chars[name] / max(n, 1), 1),
        })
    return out


def extract_keywords(messages: list[dict], top_n: int = 30) -> list[dict]:
    """高频关键词/短语（吸收词云精华，bigram+英文词）。"""
    bag = Counter()
    for m in messages:
        text = m.get("text") or ""
        if text.startswith("["):  # placeholder binary
            continue
        for t in _tokenize(text):
            if len(t) < 2:
                continue
            if t in STOPWORDS:
                continue
            if t.isdigit():
                continue
            bag[t] += 1
    return [{"term": t, "count": c} for t, c in bag.most_common(top_n)]


def activity_heatmap(messages: list[dict]) -> dict:
    """时段热力：按小时 + 按星期（吸收「活跃趋势」精华）。"""
    by_hour = Counter()
    by_weekday = Counter()
    by_day = Counter()
    for m in messages:
        ts = m.get("ts")
        if not ts:
            continue
        try:
            dt = datetime.fromtimestamp(int(ts))
        except (TypeError, ValueError, OSError):
            continue
        by_hour[dt.hour] += 1
        by_weekday[dt.weekday()] += 1  # 0=Mon
        by_day[dt.strftime("%Y-%m-%d")] += 1
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    return {
        "byHour": [{"hour": h, "count": by_hour.get(h, 0)} for h in range(24)],
        "byWeekday": [{"weekday": weekdays[i], "count": by_weekday.get(i, 0)} for i in range(7)],
        "byDay": [{"date": d, "count": c} for d, c in sorted(by_day.items())],
        "peakHour": max(range(24), key=lambda h: by_hour.get(h, 0)) if messages else None,
        "peakWeekday": weekdays[max(range(7), key=lambda i: by_weekday.get(i, 0))] if messages else None,
    }


def reciprocity_stats(messages: list[dict]) -> dict:
    """
    双人互动统计（吸收垂直 skill / 关系分析精华）。
    适用于私聊或仅 2 人主导的会话；多人时给出 top2 对。
    """
    ranking = speaker_ranking(messages, top_n=10)
    if len(ranking) < 2:
        return {"pair": None, "note": "发言人不足 2 人，无法做对偶分析", "metrics": {}}
    a, b = ranking[0]["nickname"], ranking[1]["nickname"]
    a_msgs = [m for m in messages if (m.get("nickname") or m.get("sender")) == a]
    b_msgs = [m for m in messages if (m.get("nickname") or m.get("sender")) == b]
    # rough response lag: consecutive different speakers
    sorted_m = sorted([m for m in messages if m.get("ts")], key=lambda x: x["ts"])
    lags_ab, lags_ba = [], []
    for i in range(1, len(sorted_m)):
        prev, cur = sorted_m[i - 1], sorted_m[i]
        pn = prev.get("nickname") or prev.get("sender")
        cn = cur.get("nickname") or cur.get("sender")
        if pn == a and cn == b:
            lags_ab.append(int(cur["ts"]) - int(prev["ts"]))
        elif pn == b and cn == a:
            lags_ba.append(int(cur["ts"]) - int(prev["ts"]))

    def _avg(xs: list[int]) -> Optional[float]:
        return round(sum(xs) / len(xs), 1) if xs else None

    ratio = (ranking[0]["messageCount"] / max(ranking[1]["messageCount"], 1))
    return {
        "pair": [a, b],
        "messageRatio": round(ratio, 2),
        "initiatorGuess": a if ranking[0]["messageCount"] >= ranking[1]["messageCount"] else b,
        "metrics": {
            "a": {"nickname": a, "count": ranking[0]["messageCount"], "avgReplyLagSec": _avg(lags_ba)},
            "b": {"nickname": b, "count": ranking[1]["messageCount"], "avgReplyLagSec": _avg(lags_ab)},
            "turnCount": len(lags_ab) + len(lags_ba),
        },
        "balanceLabel": "较均衡" if 0.6 <= ratio <= 1.7 else ("偏 " + a if ratio > 1.7 else "偏 " + b),
    }


def build_graph(messages: list[dict], min_edge_weight: int = 2) -> dict:
    """C5: relation graph via @mentions and reply_to."""
    nodes_count = Counter()
    nick = {}
    edges = defaultdict(int)
    edge_types = defaultdict(set)
    id_to_sender = {}
    for m in messages:
        s = m.get("nickname") or m.get("sender") or "未知"
        nodes_count[s] += 1
        nick[s] = s
        if m.get("id"):  # 空 id 全部落 None 键互相覆盖，reply_to 会错配
            id_to_sender[m["id"]] = s

    for m in messages:
        s = m.get("nickname") or m.get("sender") or "未知"
        text = m.get("text") or ""
        # @mentions
        for mention in re.findall(r"@([^\s@]{1,20})", text):
            if mention in nodes_count or mention in nick.values():
                edges[(s, mention)] += 1
                edge_types[(s, mention)].add("mention")
        if m.get("reply_to") and m["reply_to"] in id_to_sender:
            t = id_to_sender[m["reply_to"]]
            edges[(s, t)] += 1
            edge_types[(s, t)].add("reply")

    # co-occurrence in short time windows
    sorted_msgs = sorted([m for m in messages if m.get("ts")], key=lambda x: x["ts"])
    for i, m in enumerate(sorted_msgs):
        s = m.get("nickname") or m.get("sender") or "未知"
        for j in range(i + 1, min(i + 6, len(sorted_msgs))):
            if abs(sorted_msgs[j]["ts"] - m["ts"]) > 300:
                break
            t = sorted_msgs[j].get("nickname") or sorted_msgs[j].get("sender") or "未知"
            if s != t:
                edges[(s, t)] += 1
                edge_types[(s, t)].add("co_occurrence")

    nodes = []
    for name, cnt in nodes_count.most_common():
        role = "核心人物" if cnt >= max(5, 0.15 * sum(nodes_count.values())) else "成员"
        nodes.append({"id": name, "nickname": name, "messageCount": cnt, "role": role})
    edge_list = []
    for (a, b), w in edges.items():
        if w < min_edge_weight:
            continue
        edge_list.append({
            "source": a,
            "target": b,
            "weight": w,
            "types": sorted(edge_types[(a, b)]),
        })
    edge_list.sort(key=lambda e: -e["weight"])
    return {"nodes": nodes, "edges": edge_list}


# 一行扩展一种分析组合：mode → 要产出的块
# 吸收：CLI 取数层不进此表；分析层(welink)→ lab；垂直(关系)→ dyad；行动信号(rion hub)→ signals
def score_text(text: str) -> tuple[str, int]:
    """规范情绪打分（analyze_sentiment 与 trends 共用）。返回 (label, 正负净分)。"""
    tokens = _tokenize(text or "")
    pos = neg = 0
    for i, t in enumerate(tokens):
        negated = i > 0 and tokens[i - 1] in NEGATION
        if t in POSITIVE_WORDS:
            if negated:
                neg += 1
            else:
                pos += 1
        if t in NEGATIVE_WORDS:
            if negated:
                pos += 1
            else:
                neg += 1
    label = "positive" if pos > neg else ("negative" if neg > pos else "neutral")
    return label, pos - neg


def _signal_score(text: str) -> int:
    return score_text(text)[1]


def _is_self(m: dict, self_hint: Optional[str]) -> bool:
    if not self_hint:
        return False
    hint = str(self_hint).lower()
    return any(m.get(k) and hint in str(m.get(k)).lower() for k in ("sender", "nickname"))


def extract_signals(messages: list[dict], segments: list[dict], self_hint: Optional[str] = None) -> dict:
    """C10 行动信号层（吸收 rion hub 精华）：商单线索 / 承诺 / 待回复。启发式，附证据与置信度。"""
    deals: list[dict] = []
    commits: list[dict] = []
    for seg in segments:
        for m in seg.get("messages", []):
            text = (m.get("text") or "").strip()
            if len(text) < 4:
                continue
            dh = [h for h in DEAL_HINTS if h in text]
            if dh:
                deals.append({
                    "text": text[:200],
                    "sender": m.get("nickname") or m.get("sender"),
                    "ts": m.get("ts"),
                    "hints": dh[:4],
                    "confidence": "high" if len(dh) >= 2 else "medium",
                })
            ch = [h for h in COMMIT_HINTS if h in text]
            if ch:
                commits.append({
                    "text": text[:200],
                    "sender": m.get("nickname") or m.get("sender"),
                    "ts": m.get("ts"),
                    "side": "self" if _is_self(m, self_hint) else "other",
                    "hints": ch[:3],
                    "confidence": "medium",
                })
    reply_needed = None
    texted = [m for m in messages if (m.get("text") or "").strip()]
    if texted and not _is_self(texted[-1], self_hint):
        last = texted[-1]
        text = (last.get("text") or "").strip()
        marks = []
        if any(q in text for q in QUESTION_MARKS):
            marks.append("含问句")
        if self_hint and self_hint in text and len(text) < 80:
            marks.append("点名")
        if marks:
            reply_needed = {
                "text": text[:200],
                "sender": last.get("nickname") or last.get("sender"),
                "ts": last.get("ts"),
                "marks": marks,
                "confidence": "medium",
            }
    return {"deals": deals, "commitments": commits, "replyNeeded": reply_needed}


def _linreg_predict(window: list[int | float]) -> float:
    """末段线性回归外推下一点（session-digger forecast-engine 精华）。

    均值基线在趋势/爬坡序列上会系统性误报（爬坡 σ 恰过阈值）、
    常数序列上会漏报（σ=0 被跳过），回归基线两者都避免。
    """
    n = len(window)
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(window) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return my
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, window)) / sxx
    return my + slope * (n - mx)


def compare_periods(series: list[dict]) -> dict:
    """环比 + 回归外推 2σ 异常（吸收 session-digger forecast-engine 精华 lite）。

    - compare：最后一个整月 vs 前一月的 messages/activeUsers/sentimentNet 变化
    - anomalies：逐月消息量偏离前 6 个月线性回归预测超过 2σ 的月份
      （残差 σ=0 的退化窗口按 ≥1 条偏差即标记，上限记 99）
    """
    if len(series) < 2:
        return {"months": len(series), "note": "不足两个月，无法环比"}

    def _delta(key: str) -> dict:
        prev, last = series[-2].get(key) or 0, series[-1].get(key) or 0
        pct = round((last - prev) / prev * 100, 1) if prev else None
        return {"prev": prev, "last": last, "delta": last - prev, "pct": pct}

    out: dict[str, Any] = {
        "compare": [series[-2]["month"], series[-1]["month"]],
        "messages": _delta("messages"),
        "activeUsers": _delta("activeUsers"),
        "sentimentNet": _delta("sentimentNet"),
    }
    vals = [m.get("messages") or 0 for m in series]
    anomalies = []
    for i in range(6, len(vals)):
        window = vals[i - 6:i]
        pred = _linreg_predict(window)
        # 残差 σ：各点对其拟合值的偏差
        n = len(window)
        xs = list(range(n))
        mx, my = sum(xs) / n, sum(window) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx == 0:
            resid_sd = 0.0
        else:
            slope = sum((x - mx) * (y - my) for x, y in zip(xs, window)) / sxx
            fitted = [my + slope * (x - mx) for x in xs]
            resid_sd = (sum((y - f) ** 2 for y, f in zip(window, fitted)) / n) ** 0.5
        delta = abs(vals[i] - pred)
        if resid_sd > 1e-9:
            sigma = delta / resid_sd
        else:
            sigma = 99.0 if delta >= 1 else 0.0
        if sigma > 2:
            anomalies.append({
                "month": series[i]["month"],
                "messages": vals[i],
                "baselineMean": round(pred, 1),
                "sigma": round(min(sigma, 99.0), 1),
            })
    out["anomalies"] = anomalies[-6:]
    return out


def trend_series(messages: list[dict]) -> dict:
    """C11 长周期序列（fts 全史解锁）：逐月消息量/活跃人数/情绪净分/月度关键词 Top3。"""
    series: dict[str, dict[str, Any]] = {}
    for m in messages:
        ts = m.get("ts")
        if not ts:
            continue
        try:
            month = datetime.fromtimestamp(int(ts)).strftime("%Y-%m")
        except Exception:
            continue
        slot = series.setdefault(month, {"messages": 0, "users": set(), "score": 0, "tokens": Counter()})
        slot["messages"] += 1
        u = m.get("nickname") or m.get("sender")
        if u:
            slot["users"].add(u)
        slot["score"] += _signal_score(m.get("text") or "")
        for t in _tokenize(m.get("text") or ""):
            if len(t) >= 2:
                slot["tokens"][t] += 1
    months = sorted(series)
    out = []
    for k in months:
        s = series[k]
        out.append({
            "month": k,
            "messages": s["messages"],
            "activeUsers": len(s["users"]),
            "sentimentNet": s["score"],
            "topTokens": [t for t, _ in s["tokens"].most_common(3)],
        })
    return {"series": out, "months": len(out), "periods": compare_periods(out)}


ANALYSIS_MODES: dict[str, set[str]] = {
    "summary": {"topics", "profiles", "ranking"},
    "relation": {"graph", "ranking"},
    "sentiment": {"sentiment", "activity"},
    "decision": {"decisions"},
    "signals": {"signals"},  # rion hub 精华：商单/承诺/待回复（配 --self 识别本人）
    "trends": {"trends"},  # fts 全史解锁：逐月长周期序列（配全量取数）
    "lab": {"topics", "profiles", "ranking", "keywords", "sentiment", "activity"},  # welink 精华
    "dyad": {"ranking", "reciprocity", "sentiment", "keywords"},  # 垂直关系精华
    "full": {"topics", "profiles", "ranking", "keywords", "sentiment", "activity", "decisions", "graph", "reciprocity", "signals", "trends"},
}


SEGMENT_CONSUMERS: set[str] = {"topics", "sentiment", "decisions", "signals"}
"""需要语义分段的块。其余块（profiles/ranking/keywords/activity/graph/reciprocity/trends）
只吃原始 messages，不为它们跑分段——分段是全史分析里最贵的一步。"""


def run_pipeline(messages: list[dict], mode: str = "full", self_hint: Optional[str] = None) -> dict:
    """表驱动分析管道。未知 mode 回退 full。

    分段按需：只有 blocks 里出现 SEGMENT_CONSUMERS 的块才计算并输出 segments。
    trends/relation 这类不吃分段的模式因此省掉整趟 simhash，输出里也不再带
    与结果无关的 segments 明细。
    """
    blocks = ANALYSIS_MODES.get(mode) or ANALYSIS_MODES["full"]
    need_segments = bool(blocks & SEGMENT_CONSUMERS)
    segments = segment_messages(messages) if need_segments else []
    result: dict[str, Any] = {
        "mode": mode if mode in ANALYSIS_MODES else "full",
        "stats": {
            "totalMessages": len(messages),
            "activeUsers": len({m.get("nickname") or m.get("sender") for m in messages}),
            "segmentCount": len(segments),
        },
    }
    if need_segments:
        result["segments"] = [
            {"id": s["id"], "messageCount": s["messageCount"], "topicHint": s["topicHint"]}
            for s in segments
        ]
    if "topics" in blocks:
        result["topics"] = extract_topics(segments)
    if "profiles" in blocks:
        result["profiles"] = build_profiles(messages)
    if "ranking" in blocks:
        result["ranking"] = speaker_ranking(messages)
    if "keywords" in blocks:
        result["keywords"] = extract_keywords(messages)
    if "sentiment" in blocks:
        result["sentiment"] = analyze_sentiment(segments)
    if "activity" in blocks:
        result["activity"] = activity_heatmap(messages)
    if "decisions" in blocks:
        result["decisions"] = extract_decisions(segments)
    if "graph" in blocks:
        result["graph"] = build_graph(messages)
    if "reciprocity" in blocks:
        result["reciprocity"] = reciprocity_stats(messages)
    if "signals" in blocks:
        result["signals"] = extract_signals(messages, segments, self_hint=self_hint)
    if "trends" in blocks:
        result["trends"] = trend_series(messages)
    return result


def main():
    p = argparse.ArgumentParser(description="wechat-digger analyze")
    p.add_argument(
        "command",
        choices=[
            "segment", "topics", "sentiment", "decisions", "profiles", "graph",
            "ranking", "keywords", "activity", "reciprocity", "pipeline",
        ],
    )
    p.add_argument("--input", required=True, help="messages or segments JSON")
    p.add_argument("--output", default="-")
    p.add_argument("--mode", default="full", choices=sorted(ANALYSIS_MODES.keys()))
    p.add_argument("--max-topics", type=int, default=8)
    p.add_argument("--self", dest="self_hint", default=None, help="本人昵称/wxid 片段，用于信号层待回复与承诺方向判定")
    args = p.parse_args()

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    if isinstance(data, dict) and "messages" in data:
        messages = data["messages"]
    elif isinstance(data, list):
        messages = data
    else:
        messages = []

    if args.command == "segment":
        out = segment_messages(messages)
    elif args.command == "topics":
        segs = segment_messages(messages)
        out = extract_topics(segs, max_topics=args.max_topics)
    elif args.command == "sentiment":
        out = analyze_sentiment(segment_messages(messages))
    elif args.command == "decisions":
        out = extract_decisions(segment_messages(messages))
    elif args.command == "profiles":
        out = build_profiles(messages)
    elif args.command == "graph":
        out = build_graph(messages)
    elif args.command == "ranking":
        out = speaker_ranking(messages)
    elif args.command == "keywords":
        out = extract_keywords(messages)
    elif args.command == "activity":
        out = activity_heatmap(messages)
    elif args.command == "reciprocity":
        out = reciprocity_stats(messages)
    else:
        out = run_pipeline(messages, mode=args.mode)

    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.output == "-":
        print(text)
    else:
        Path(args.output).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
