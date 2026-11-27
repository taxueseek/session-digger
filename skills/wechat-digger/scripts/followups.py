#!/usr/bin/env python3
"""商机/承诺跟进状态机（吸收 wechat-intelligence-hub opportunity_store 精华 lite）。

与 hub 的差异：JSON 存储（非 sqlite）、候选 14 天过期、feedback 学习、
triage 分流、reinforcement 强化；状态集收敛为 new/open/waiting/stale/won/lost/ignored。
待回复/等待对方不持久化——每次扫描从消息尾迹现算（hub 日报同语义）。

数据入口为 fts 全史文本层（followups 只看文本启发式，富类型不扫）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from analyze import COMMIT_HINTS, DEAL_HINTS, _is_self
from relevance import RelevanceProfile  # noqa: F401  (type + runtime use)

OPEN_STATUSES = {"new", "open", "waiting", "paused"}
CLOSED_STATUSES = {"won", "lost"}
INACTIVE_STATUSES = {"ignored", "stale"}
ALL_STATUSES = OPEN_STATUSES | CLOSED_STATUSES | INACTIVE_STATUSES
TRIAGE_DECISIONS = {
    "pursue": "open",
    "wait": "waiting",
    "pause": "paused",
    "ignore": "ignored",
    "won": "won",
    "lost": "lost",
}
FEEDBACK_VERDICTS = {"confirmed", "false_positive", "ignore", "low_priority"}
CANDIDATE_EXPIRY_DAYS = 14
EVIDENCE_CAP = 5
# 对方的短应答（"好的/嗯嗯"）只关闭待回复，不构成对我方承诺的兑现（hub 任务状态语义）
ACK_WORDS = {"好的", "好", "嗯", "嗯嗯", "行", "ok", "OK", "收到", "哦", "好嘞", "好滴"}
QUESTION_MARKS = ("？", "?")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _expiry(signal_ts: int | None, at: str, days: int = CANDIDATE_EXPIRY_DAYS) -> str:
    base = (
        datetime.fromtimestamp(signal_ts)
        if signal_ts
        else datetime.fromisoformat(at)
    )
    return (base + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _signal_key(chat_id: str, kind: str, text: str) -> str:
    digest = hashlib.md5(text.strip()[:80].encode("utf-8")).hexdigest()[:8]
    return f"{chat_id}|{kind}|{digest}"


# ── 状态存储（JSON） ─────────────────────────────────────────

def load_state(path: Path) -> dict:
    """读取状态；文件存在但不可解析时返回带 `_corrupt` 标记的空状态。

    此前任何异常都静默返回空状态，随后 scan 路径无条件 save_state 覆写——
    一个被截断的 followups.json 会让商机/承诺/反馈全部无声消失（`--list`
    显示 0 条，像「从来没有商机」），且没有备份、没有告警。标记让调用方
    有机会先备份原文件再决定是否继续。
    """
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(state, dict) and isinstance(state.get("items"), list):
                return state
            reason = "结构不符（缺 items 列表）"
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
        empty = {"version": 1, "items": [], "feedback": [], "nextId": 1}
        empty["_corrupt"] = reason
        return empty
    return {"version": 1, "items": [], "feedback": [], "nextId": 1}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _latest_verdict(state: dict, *targets: str) -> str:
    want = {t for t in targets if t}
    for fb in reversed(state.get("feedback", [])):
        if fb.get("target") in want:
            return str(fb.get("verdict") or "")
    return ""


def add_feedback(state: dict, target: str, verdict: str, note: str = "") -> None:
    if verdict not in FEEDBACK_VERDICTS:
        raise ValueError(f"不支持的反馈结论：{verdict}（可用：{'/'.join(sorted(FEEDBACK_VERDICTS))}）")
    if not target.strip():
        raise ValueError("反馈对象不能为空")
    state.setdefault("feedback", []).append(
        {"target": target.strip(), "verdict": verdict, "note": note, "at": _now()}
    )


def triage(state: dict, item_id: int, decision: str, note: str = "") -> Optional[dict]:
    """分流决定 → 状态迁移 + 反馈学习（pursue/wait/won 视为确认）。"""
    if decision not in TRIAGE_DECISIONS:
        raise ValueError(f"不支持的分流决定：{decision}（可用：{'/'.join(TRIAGE_DECISIONS)}）")
    item = next((it for it in state["items"] if it.get("id") == item_id), None)
    if not item:
        return None
    item["status"] = TRIAGE_DECISIONS[decision]
    item["reviewedAt"] = _now()
    if note:
        item.setdefault("notes", []).append(f"[{_now()}] {note}")
    if decision in ("pursue", "wait", "won"):
        add_feedback(state, f"item:{item_id}", "confirmed", f"triage:{decision}")
    elif decision == "ignore":
        add_feedback(state, f"item:{item_id}", "ignore", f"triage:{decision}")
        add_feedback(state, f"chat:{item.get('chat_id')}", "ignore", f"triage:{decision}")
    return item


def expire_stale(state: dict, at: Optional[str] = None) -> int:
    """候选过期：new 状态超过 expiry → stale（保留证据，不进今日行动）。"""
    at = at or _now()
    n = 0
    for it in state["items"]:
        if it.get("status") == "new" and it.get("expires") and it["expires"] <= at:
            it["status"] = "stale"
            n += 1
    return n


def sync_signals(
    state: dict,
    chat_name: str,
    chat_id: str,
    signals: list[dict],
    profile: "Optional[RelevanceProfile]" = None,
) -> dict:
    """信号并入状态机：新建候选 / 强化既有项 / 过期复活 / 反馈豁免。

    signals: [{kind, text, sender, ts, hints}]——同一 key 重复出现记 reinforcement。
    profile: 可选用户相关性策略；未配置时行为与引入前逐字一致。
    返回 {created, updated, skipped, filtered}。
    """
    stats = {"created": 0, "updated": 0, "skipped": 0, "filtered": 0}
    expire_stale(state)
    chat_verdict = _latest_verdict(state, f"chat:{chat_id}")
    for sig in signals:
        key = _signal_key(chat_id, sig["kind"], sig["text"])
        verdict = _latest_verdict(state, f"key:{key}", f"chat:{chat_id}") or chat_verdict
        if verdict in ("false_positive", "ignore"):
            stats["skipped"] += 1
            continue
        confidence = "high" if len(sig.get("hints") or []) >= 2 else "medium"
        priority = min(5, 2 + max(0, len(sig.get("hints") or []) - 1))
        if verdict == "low_priority":
            priority = min(priority, 1)
        if verdict == "confirmed":
            priority = max(priority, 5)

        # 用户相关性策略：命中排除主题的信号不入库。
        # 人工 confirmed 豁免——用户已明确确认的信号不该被策略再次拦下。
        if profile is not None and verdict != "confirmed":
            if profile.is_excluded_text(sig["text"]):
                stats["filtered"] += 1
                continue
            priority, _notes = profile.adjust(priority, sig["text"], chat_id)

        # 同 key 取最近一条（含已关闭——关闭后有新信号则新开一条）
        existing = None
        for it in reversed(state["items"]):
            if it.get("key") == key:
                existing = it
                break

        if existing is None or existing.get("status") in CLOSED_STATUSES:
            item = {
                "id": state.get("nextId", 1),
                "key": key,
                "chat": chat_name,
                "chat_id": chat_id,
                "kind": sig["kind"],
                "title": sig["text"].strip()[:40],
                "evidence": [{"ts": sig.get("ts"), "text": sig["text"][:200], "sender": sig.get("sender")}],
                "status": "new",
                "confidence": "confirmed" if verdict == "confirmed" else confidence,
                "priority": priority,
                "reinforcement": 1,
                "firstSeen": _now(),
                "lastSignal": sig.get("ts"),
                "expires": "" if verdict == "confirmed" else _expiry(sig.get("ts"), _now()),
                "notes": [],
            }
            state["nextId"] = item["id"] + 1
            state["items"].append(item)
            stats["created"] += 1
            continue

        has_new = (sig.get("ts") or 0) > (existing.get("lastSignal") or 0)
        if not has_new:
            stats["skipped"] += 1
            continue
        existing["lastSignal"] = sig.get("ts")
        existing["reinforcement"] = int(existing.get("reinforcement") or 1) + 1
        if priority > int(existing.get("priority") or 0):
            existing["priority"] = priority
        rank = {"low": 0, "medium": 1, "high": 2, "confirmed": 3}
        if rank.get(confidence, 1) > rank.get(existing.get("confidence") or "medium", 1):
            existing["confidence"] = confidence
        ev = existing.setdefault("evidence", [])
        ev.append({"ts": sig.get("ts"), "text": sig["text"][:200], "sender": sig.get("sender")})
        del ev[:-EVIDENCE_CAP]
        if verdict == "confirmed":
            existing["confidence"] = "confirmed"
            existing["expires"] = ""
        if existing.get("status") == "stale":
            existing["status"] = "new"
            existing["expires"] = _expiry(sig.get("ts"), _now())
        elif existing.get("status") == "new" and verdict != "confirmed":
            existing["expires"] = _expiry(sig.get("ts"), _now())
        stats["updated"] += 1
    return stats


# ── 信号提取（消息尾迹现算，不持久化） ───────────────────────

def scan_messages(msgs: list[dict], self_hint: Optional[str] = None) -> dict:
    """单会话文本消息 → {signals, toReply, waiting}。

    signals 进状态机（商机/承诺）；toReply/waiting 是尾迹现算任务：
    - 待回复：最后一条来自对方且含问句/点名
    - 等待对方：最后一条来自我方且含问句（含对方短应答关闭待回复的情况）
    """
    signals: list[dict] = []
    for m in msgs:
        text = (m.get("text") or "").strip()
        if len(text) < 4:
            continue
        dh = [h for h in DEAL_HINTS if h in text]
        if dh:
            signals.append({
                "kind": "deal", "text": text, "sender": m.get("sender"),
                "ts": m.get("ts"), "hints": dh,
            })
        ch = [h for h in COMMIT_HINTS if h in text]
        if ch:
            side = None
            if self_hint:
                side = "self" if _is_self(m, self_hint) else "other"
            signals.append({
                "kind": "commit", "text": text, "sender": m.get("sender"),
                "ts": m.get("ts"), "hints": ch, "side": side,
            })

    texted = [m for m in msgs if (m.get("text") or "").strip()]
    to_reply = waiting = None
    if texted:
        last = texted[-1]
        text = last["text"].strip()
        last_is_self = bool(self_hint) and _is_self(last, self_hint)
        if not last_is_self:
            prev_self = self_hint and len(texted) > 1 and _is_self(texted[-2], self_hint)
            if prev_self and text.lower() in ACK_WORDS:
                # 对方短应答：待回复关闭，转为等待对方
                waiting = {"text": texted[-2]["text"].strip()[:200], "sender": texted[-2].get("sender"), "ts": texted[-2].get("ts"), "note": "我方已交付，对方已应答，等下一动作"}
            elif any(q in text for q in QUESTION_MARKS):
                to_reply = {"text": text[:200], "sender": last.get("sender"), "ts": last.get("ts"), "note": "对方消息含问句"}
            elif self_hint and self_hint in text and len(text) < 80:
                to_reply = {"text": text[:200], "sender": last.get("sender"), "ts": last.get("ts"), "note": "对方点名我方"}
        else:
            if any(q in text for q in QUESTION_MARKS):
                waiting = {"text": text[:200], "sender": last.get("sender"), "ts": last.get("ts"), "note": "我方提问在等对方回复"}
    return {"signals": signals, "toReply": to_reply, "waiting": waiting}


# ── 今日行动与复联 ───────────────────────────────────────────

def today_actions(state: dict, limit: int = 20) -> list[dict]:
    """今日行动清单：开放项按 到期跟进 > 优先级 > 置信度 > 最新信号 排序。"""
    today = datetime.now().strftime("%Y-%m-%d")
    open_items = [
        it for it in state["items"]
        if it.get("status") in OPEN_STATUSES
        and _latest_verdict(state, f"key:{it.get('key')}", f"chat:{it.get('chat_id')}") not in ("false_positive", "ignore")
    ]

    def _due(it: dict) -> int:
        return 0 if (it.get("followUp") or "")[:10] <= today else 1

    conf_rank = {"confirmed": 3, "high": 2, "medium": 1, "low": 0}
    open_items.sort(
        key=lambda it: (
            _due(it),
            -int(it.get("priority") or 0),
            -conf_rank.get(it.get("confidence") or "medium", 1),
            -(it.get("lastSignal") or 0),
        )
    )
    return open_items[:limit]


def reactivations(state: dict, msgs_by_chat: dict[str, list[dict]], self_hint: Optional[str], idle_days: int = 14) -> list[dict]:
    """复联提醒：窗口内沉默超 idle_days 的私聊（两个方向都提醒）。

    - 对方最后发言：球在我方（未回复）或双方都断了——考虑复联
    - 我方最后发言：对方长期无回应——确认再推一把还是归档
    """
    out = []
    now = datetime.now().timestamp()
    for chat_id, msgs in msgs_by_chat.items():
        texted = [m for m in msgs if (m.get("text") or "").strip()]
        if not texted:
            continue
        last = texted[-1]
        idle = now - (last.get("ts") or 0)
        if idle < idle_days * 86400:
            continue
        last_is_self = bool(self_hint) and _is_self(last, self_hint)
        name = last.get("chat_name") or chat_id
        out.append({
            "chat_id": chat_id,
            "chat": name,
            "idleDays": round(idle / 86400, 1),
            "lastText": (last.get("text") or "")[:120],
            "lastTs": last.get("ts"),
            "lastBySelf": last_is_self,
            "note": (
                "我方最后发言后长期无回应——确认再推一把还是归档"
                if last_is_self
                else "对方最后发言后长期未续——考虑复联"
            ),
        })
    out.sort(key=lambda x: -x["idleDays"])
    return out[:20]


def open_count(state: dict) -> dict:
    by_status: dict[str, int] = {}
    for it in state["items"]:
        by_status[it.get("status") or "?"] = by_status.get(it.get("status") or "?", 0) + 1
    return by_status
