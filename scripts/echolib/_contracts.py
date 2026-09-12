"""
_contracts.py — TypedDict contracts for echolib's key data structures.

Every function in the PARSE/INDEX/TREND/DECISION pipeline returns or accepts
one of these shapes.  Defining them in one place means:

1. **Catch field drift at type-check time** — if index-builder.py reads a key
   that session_stats no longer emits, mypy catches it.
2. **Self-documenting API surface** — no more guessing what keys exist.
3. **Cross‑tool consistency** — all adapters must return the same shape.

Usage::

    from echolib._contracts import SessionStats, ToolInfo, MessageInfo

    stats: SessionStats = dispatch_session_stats(path)
    tools: list[ToolInfo] = list(dispatch_extract_tools(path))
"""

from __future__ import annotations

from typing import TypedDict


# ── Session statistics ────────────────────────────────────────────────────

class SessionStats(TypedDict):
    """Standardised stats returned by every environment adapter.

    This is the most important contract — passed from Layer 0 (PARSE) through
    Layer 1 (INDEX), Layer 2 (TREND), and consumed by Layer 3 (DECISION) and
    the reflect report.
    """
    slug: str
    model: str
    branch: str
    started: str
    ended: str
    user_messages: int
    assistant_messages: int
    tool_calls: int
    files_edited: int
    errors: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_create_tokens: int
    compactions: int
    summary: str
    total_tokens: int
    # Derived field: cache_read / (input + cache_read) or cache_read / input
    # Computed by attach_cache_hit_rates() or directly by adapters.
    cache_hit_rate: float | None


# ── Tool / message entries (generator yields) ─────────────────────────────

class ToolInfo(TypedDict):
    """Single tool call with its result, as yielded by ``extract_tools()``."""
    name: str
    timestamp: str
    key_input: str
    status: str
    result_preview: str
    duration: float


class MessageInfo(TypedDict):
    """Single message as yielded by ``extract_messages()``."""
    role: str
    timestamp: str
    text: str


# ── Parsed session (reflect-report / index-builder per-session payload) ────

class ParsedSession(TypedDict):
    """One session as loaded by reflect-report and index-builder."""
    id: str
    agent: str
    family: str
    created: str
    date: str
    hour: int
    weekday: int
    minutes: float
    messages: int
    user_messages: int
    tools: int
    tool_usage: dict[str, int]
    errors: int
    tokens: int
    project: str
    summary: str
    task: str
    topics: list[str]


# ── Trend (Layer 2) ───────────────────────────────────────────────────────

class TrendPeriod(TypedDict):
    """Aggregated stats for one period in a trend comparison."""
    period: str
    session_count: int
    total_turns: int
    total_tool_calls: int
    overall_error_rate: float
    avg_duration_seconds: float | None
    flagged_sessions: int
    tool_usage: dict[str, int]
    tool_error_rate: dict[str, float]


class TrendResult(TypedDict):
    """Full output of ``trend-engine.py period-over-period``."""
    unit: str
    periods_analyzed: int
    direction: TrendDirection
    timeline: list[TrendPeriod]


class TrendDirection(TypedDict):
    """Direction summary for a trend analysis."""
    error_rate_trend: str  # "improving" | "worsening" | "stable"
    first_period: str
    last_period: str
    first_error_rate: float
    last_error_rate: float


# ── Skill-gap (Layer 3) ───────────────────────────────────────────────────

class GapReport(TypedDict):
    """Pattern analysis report from skill-gap-finder."""
    total_sessions: int
    analysis_period: str
    patterns: list[GapPattern]
    proposals: list[GapProposal]


class GapPattern(TypedDict):
    """A recurring pattern found across multiple sessions."""
    type: str  # "error", "retry", "long_conversation", "tool_regression"
    tool: str | None
    frequency: int
    severity: str  # "low" | "medium" | "high"
    example_sessions: list[str]
    description: str


class GapProposal(TypedDict):
    """A proposed improvement to SKILL.md based on detected patterns."""
    problem: str
    evidence_count: int
    suggested_skill_md_addition: str
    matched_skill: dict[str, str]


# ── Scan results (from scan_all_environments_parallel) ────────────────────

class ScanResult(TypedDict):
    """One environment entry from an environment scan."""
    name: str
    env_id: str
    path: str
    exists: bool
    session_count: int | None
    status: str


# ── Adapter registry entry ────────────────────────────────────────────────

class AdapterEntry(TypedDict):
    """An adapter registered in ADAPTER_REGISTRY."""
    name: str
    display_name: str
    list_sessions: callable | None  # noqa: F821
    session_stats: callable | None
    extract_messages: callable | None
    extract_tools: callable | None
    session_path: callable | None
