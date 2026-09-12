#!/usr/bin/env python3
"""Relevance profile: keeps business-opportunity scanning aligned with what
the user actually cares about.

Why this exists
---------------
``followups`` scored every signal with one formula —
``priority = min(5, 2 + len(hints) - 1)`` — which is context-free. Measured
on the real vault, 54-60% of the resulting 607 items were e-commerce
promotions ("京东待评价", "可复美面膜 拍1 https://u.jd.com/..."). Group-level
noise filtering (``chat_quality``) removes the bulk of them, but a
well-behaved group can still carry an off-topic promotion, and no threshold
can know that *this* user does not pursue 信用卡开卡 or 话费券 deals while
*another* user does.

Two mechanisms, deliberately separate:

1. **Topic relevance** — declarative include/exclude keyword lists. A signal
   matching an excluded topic is dropped at ingest; a signal matching an
   included topic is promoted. This is user-authored policy, not inference.
2. **Source trust** — per-chat multipliers learned from human triage
   (``confirmed`` / ``false_positive``) rather than hard-coded.

Design note: an empty profile is a no-op. With no config file present, the
scoring path is exactly what it was before, so introducing this module
cannot change behavior for anyone who has not opted in. That property is
asserted by the tests.

Profile file (optional): ``~/.config/wechat-digger/relevance.json``
    {
      "include": ["AI", "投资", "合作", "内容"],
      "exclude": ["信用卡", "话费", "优惠券", "试吃"],
      "chat_trust": {"12345678901@chatroom": -5},
      "boost": 2
    }
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Optional

DEFAULT_PROFILE_PATH = Path("~/.config/wechat-digger/relevance.json").expanduser()
ENV_PROFILE = "WECHAT_RELEVANCE_PROFILE"

# Priority bounds the state machine uses. Kept here so the profile module and
# followups cannot drift.
PRIORITY_MIN = 0
PRIORITY_MAX = 5

# A signal matching an excluded topic is suppressed outright rather than
# merely down-ranked: measured evidence showed these are pure noise for this
# user (电商/开卡/话费 promotions), and leaving them creates review burden.
EXCLUDED_MARKER = "excluded"


class RelevanceProfile:
    """Topic + source policy for opportunity signals.

    Immutable from the caller's perspective: construct one, pass it around.
    """

    def __init__(
        self,
        include: Optional[Iterable[str]] = None,
        exclude: Optional[Iterable[str]] = None,
        chat_trust: Optional[dict[str, int]] = None,
        boost: int = 2,
        source_path: Optional[Path] = None,
    ) -> None:
        self.include = tuple(include or ())
        self.exclude = tuple(exclude or ())
        self.chat_trust = dict(chat_trust or {})
        self.boost = int(boost)
        self.source_path = source_path

    # ── construction ────────────────────────────────────────────────

    @property
    def is_empty(self) -> bool:
        """True when no policy is configured; callers can skip all work."""
        return not (self.include or self.exclude or self.chat_trust)

    @classmethod
    def from_dict(cls, raw: Any, source_path: Optional[Path] = None) -> "RelevanceProfile":
        """Build from parsed JSON, tolerating malformed shapes.

        A bad profile must never break scanning — an unreadable policy is
        equivalent to no policy, which is the safe default.
        """
        if not isinstance(raw, dict):
            return cls(source_path=source_path)

        def _str_list(value: Any) -> tuple[str, ...]:
            if not isinstance(value, (list, tuple)):
                return ()
            out = []
            for item in value:
                if isinstance(item, str) and item.strip():
                    out.append(item.strip())
            return tuple(out)

        trust: dict[str, int] = {}
        raw_trust = raw.get("chat_trust")
        if isinstance(raw_trust, dict):
            for key, value in raw_trust.items():
                if isinstance(key, str) and isinstance(value, (int, float)):
                    trust[key] = int(value)

        boost = raw.get("boost", 2)
        if not isinstance(boost, (int, float)):
            boost = 2

        return cls(
            include=_str_list(raw.get("include")),
            exclude=_str_list(raw.get("exclude")),
            chat_trust=trust,
            boost=int(boost),
            source_path=source_path,
        )

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "RelevanceProfile":
        """Load from disk; missing or invalid file yields an empty profile."""
        target = path
        if target is None:
            env = os.environ.get(ENV_PROFILE)
            target = Path(env).expanduser() if env else DEFAULT_PROFILE_PATH
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return cls(source_path=target)
        return cls.from_dict(raw, source_path=target)

    # ── evaluation ──────────────────────────────────────────────────

    def match_excluded(self, text: str) -> Optional[str]:
        """Return the excluded term that matched, else None."""
        lowered = (text or "").lower()
        for term in self.exclude:
            if term.lower() in lowered:
                return term
        return None

    def match_included(self, text: str) -> Optional[str]:
        """Return the included term that matched, else None."""
        lowered = (text or "").lower()
        for term in self.include:
            if term.lower() in lowered:
                return term
        return None

    def adjust(
        self,
        priority: int,
        text: str,
        chat_id: str = "",
    ) -> tuple[int, list[str]]:
        """Apply topic and source policy to a base priority.

        Returns ``(priority, notes)``. When the text matches an excluded
        topic the priority is pushed to ``PRIORITY_MIN`` and a marker note is
        added; callers decide whether to drop the signal entirely.
        """
        if self.is_empty:
            return priority, []

        notes: list[str] = []
        value = priority

        if self.match_excluded(text):
            value = PRIORITY_MIN
            notes.append(f"{EXCLUDED_MARKER}:{self.match_excluded(text)}")
            return value, notes

        matched = self.match_included(text)
        if matched:
            value += self.boost
            notes.append(f"topic:{matched}")

        trust = self.chat_trust.get(chat_id)
        if trust:
            value += trust
            notes.append(f"trust:{trust:+d}")

        value = max(PRIORITY_MIN, min(PRIORITY_MAX, value))
        return value, notes

    def is_excluded_text(self, text: str) -> bool:
        return self.match_excluded(text) is not None

    def describe(self) -> dict:
        """Compact summary for doctor/CLI output."""
        return {
            "path": str(self.source_path) if self.source_path else None,
            "active": not self.is_empty,
            "include": list(self.include),
            "exclude": list(self.exclude),
            "chat_trust": dict(self.chat_trust),
            "boost": self.boost,
        }
