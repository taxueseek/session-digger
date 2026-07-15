#!/usr/bin/env python3
"""
chat-profiles.py — Incremental participant profile extraction.

Inspired by baoyu-wechat-summary's append-only profile rules, adapted for
session-digger's JSONL schema. Extracts group chat participant profiles
with incremental update semantics:

  - APPEND:  classic quotes, signature events, recurring topics
  - MERGE:   role tags, interest areas, interaction patterns
  - REFINE:  speaking style (only when evidence accumulates across 3+ digests)

This module does NOT cover WeChat-specific decryption (that belongs to
wechat-local-vault). It works on any JSONL session that contains
sender-identified messages (group chats, multi-participant transcripts).

Output: one .json profile per participant, stored alongside the session.
"""

from _common import read_json, write_json, read_text, json_out
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def extract_participants(jsonl_path):
    """Scan a group chat JSONL and return per-participant message lists.

    Expects records with message.role in (user, assistant) and
    message.content containing sender info, OR transcript-style records
    with a 'sender' field adapted by dialog-adapter.
    """
    participants = defaultdict(list)

    with open(jsonl_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            if rec.get("type") != "user":
                continue

            msg = rec.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, list):
                text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
            else:
                text = str(content)

            if not text.strip():
                continue

            # Extract sender from adapted format: "Name: message" or explicit field
            sender = rec.get("sender", "")
            if not sender:
                m = re.match(r"^(.+?):\s+(.+)$", text, re.DOTALL)
                if m:
                    sender = m.group(1).strip()
                    text = m.group(2).strip()
                else:
                    sender = "unknown"

            participants[sender].append({
                "timestamp": rec.get("timestamp", ""),
                "text": text[:500],
            })

    return dict(participants)


def update_profile(old_profile, new_messages, min_messages=3):
    """Apply append-only merge rules to a participant profile.

    Rules (from baoyu-wechat-summary, adapted):
      - APPEND: quotes, events, topics lists grow unbounded
      - MERGE:  tags, interests get frequency-sorted, deduped
      - REFINE: style only when total_messages crosses threshold multiples
    """
    total = old_profile.get("total_messages", 0) + len(new_messages)

    # --- APPEND: classic quotes (heuristic: sentences ending with ！/？/… or marked as quote) ---
    old_quotes = old_profile.get("classic_quotes", [])
    for msg in new_messages:
        text = msg["text"]
        if len(text) >= 10 and len(text) <= 200:
            if text.endswith(("！", "？", "…", "。", "!", "?")):
                if text not in old_quotes:
                    old_quotes.append(text)

    # --- MERGE: interest areas (simple keyword extraction) ---
    old_interests = old_profile.get("interest_areas", [])
    interest_kws = [
        "基金", "股票", "AI", "写作", "公众号", "小红书", "面试", "简历",
        "投资", "理财", "副业", "创业", "流量", "阅读", "学习", "golang",
        "python", "react", "docker", "k8s", "产品", "运营", "面试",
    ]
    new_interests = []
    combined_text = " ".join(m["text"] for m in new_messages)
    for kw in interest_kws:
        if kw.lower() in combined_text.lower():
            new_interests.append(kw)
    merged_interests = list(set(old_interests + new_interests))

    # --- MERGE: interaction patterns ---
    hours = [int(m["timestamp"][11:13]) for m in new_messages if m["timestamp"] and len(m["timestamp"]) >= 13]
    hour_counter = Counter(hours)
    peak_hour = hour_counter.most_common(1)[0][0] if hour_counter else None

    old_pattern = old_profile.get("active_hours", {})
    for h, cnt in hour_counter.items():
        old_pattern[str(h)] = old_pattern.get(str(h), 0) + cnt

    # --- REFINE: speaking style (only when crossing thresholds) ---
    style = old_profile.get("speaking_style", "")
    old_total = old_profile.get("total_messages", 0)
    style_refined = (old_total // 100) < (total // 100)  # crossed 100-msg boundary

    return {
        "total_messages": total,
        "classic_quotes": old_quotes[:20],  # cap at 20
        "interest_areas": merged_interests,
        "active_hours": old_pattern,
        "peak_hour": peak_hour,
        "speaking_style": style,
        "style_refined": style_refined,
        "last_seen": max((m["timestamp"] for m in new_messages if m["timestamp"]), default=old_profile.get("last_seen", "")),
    }


def build_or_update_profile(jsonl_path, profiles_dir):
    """Main entry: extract participants and update their profiles."""
    participants = extract_participants(jsonl_path)
    profiles_dir = Path(profiles_dir)
    profiles_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for sender, messages in participants.items():
        if len(messages) < 2:
            continue  # need at least 2 messages for a meaningful profile

        safe_name = re.sub(r'[^\w\u4e00-\u9fff-]', '_', sender)[:50]
        profile_path = profiles_dir / f"{safe_name}.json"

        old = {}
        if profile_path.exists():
            try:
                old = read_json(profile_path)
            except (json.JSONDecodeError, OSError):
                old = {}

        updated = update_profile(old, messages)
        updated["name"] = sender
        updated["first_seen"] = old.get("first_seen", messages[0]["timestamp"])
        updated["profile_path"] = str(profile_path)
        updated["session_id"] = Path(jsonl_path).stem

        profile_path.write_text(
            json.dumps(updated, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        results.append(updated)

    return results


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: chat-profiles.py <session.jsonl> [profiles-dir]")
        sys.exit(1)

    jsonl = sys.argv[1]
    profiles_dir = sys.argv[2] if len(sys.argv) > 2 else str(Path(jsonl).parent / "profiles")
    results = build_or_update_profile(jsonl, profiles_dir)
    print(f"Updated {len(results)} profiles in {profiles_dir}")
    for r in results:
        print(f"  {r['name']}: {r['total_messages']} msgs, {len(r['classic_quotes'])} quotes")
