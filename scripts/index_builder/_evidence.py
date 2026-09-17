"""Per-session user-message evidence: index-time projection, read-time render.

Why a projection instead of reading ``messages_fts``: FTS5 stores ``session_id``
as UNINDEXED, so ``WHERE session_id = ?`` cannot use any index and scans the
whole content table. Measured on the live index (71003 rows, 333 MB): 127 ms per
hit, 1240 ms per miss, and 85% of a 20-result ``sd-recall search``.

The projection is computed once per indexed session and stored in
``sessions.user_evidence_json``, so it travels in the same batched row read the
callers already perform (``quick_stats_from_index`` / ``recent_sessions``) and
costs nothing at search time.

The decision flag is evaluated here, over the FULL message text, so
``--decisions`` keeps full-text recall even though the stored display text is
capped.
"""
from __future__ import annotations

import json
import re

# Bilingual decision-signal patterns. Single source: the builder projects on
# this list, the read layer re-exports it for the file-scan fallback.
DECISION_PATTERNS = [
    r"(?i)\bdecided to\b", r"(?i)\bchose to\b", r"(?i)\bgoing to (use|switch|try|migrate)\b",
    r"(?i)\bwill (use|switch|try|migrate|go with)\b", r"(?i)\binstead of\b",
    r"(?i)\bswitch(ed|ing)? to\b", r"(?i)\buse \w+ over\b", r"(?i)\bmoving to\b",
    r"决定", r"选择", r"改用", r"还是", r"换成", r"放弃", r"尝试",
]

# Display cap per message (unchanged from the previous read-layer slice).
EVIDENCE_TEXT_CAP = 300
DEFAULT_MESSAGES = 15


def project_user_evidence(messages, limit=DEFAULT_MESSAGES, text_cap=EVIDENCE_TEXT_CAP) -> str:
    """JSON projection of the first ``limit`` non-empty USER messages.

    Returns ``[]`` as JSON when there is nothing to store, so a stored value is
    always parseable and "no user turns" is distinguishable from "not computed".
    """
    decision_res = [re.compile(p) for p in DECISION_PATTERNS]
    out = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        if (msg.get("role") or "").upper() != "USER":
            continue
        text = msg.get("text") or ""
        if not text:
            continue
        out.append({
            "ts": msg.get("timestamp") or "",
            "text": text[:text_cap],
            "decision": any(p.search(text) for p in decision_res),
        })
        if len(out) >= limit:
            break
    return json.dumps(out, ensure_ascii=False)


def evidence_from_row(row, decisions=False, limit_msgs=DEFAULT_MESSAGES) -> dict:
    """Evidence dict for one session, from an already-fetched index row.

    Same shape as ``sd-recall.extract_evidence`` (the file-scan path) so the
    two are interchangeable at the call site. ``tool_errors`` comes from the
    row's aggregate column — name → failed-call count, not per-call rows.
    """
    items = []
    raw = (row or {}).get("user_evidence_json") if isinstance(row, dict) else None
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                items = [it for it in parsed if isinstance(it, dict)][:limit_msgs]
        except (ValueError, TypeError):
            items = []

    user_messages = [
        {"role": "USER", "timestamp": it.get("ts") or "", "text": it.get("text") or ""}
        for it in items
    ]
    hits = None
    if decisions:
        hits = [m for it, m in zip(items, user_messages) if it.get("decision")]

    tool_errors = []
    agg_raw = (row or {}).get("tool_errors_json") if isinstance(row, dict) else None
    if agg_raw:
        try:
            agg = json.loads(agg_raw)
        except (ValueError, TypeError):
            agg = {}
        if isinstance(agg, dict):
            tool_errors = [
                {"timestamp": "", "name": name,
                 "result_preview": f"{count} failed call(s) (aggregate)"}
                for name, count in sorted(agg.items(), key=lambda kv: -kv[1])[:5]
            ]

    return {"user_messages": user_messages, "tool_errors": tool_errors, "decisions": hits}
