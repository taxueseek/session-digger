#!/usr/bin/env python3
"""Regression: Grok path contract + signals-first stats + compile hygiene."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))


def test_scripts_with_future_compile():
    """from __future__ must not sit after other imports (class of SyntaxError)."""
    broken = []
    for p in (PLUGIN_ROOT / "scripts").rglob("*.py"):
        src = p.read_text(encoding="utf-8")
        if "from __future__ import" not in src:
            continue
        try:
            compile(src, str(p), "exec")
        except SyntaxError as exc:
            broken.append(f"{p.relative_to(PLUGIN_ROOT)}: {exc.msg}")
    assert not broken, "future-import order broken:\n" + "\n".join(broken)


def test_grok_home_and_cwd_file(monkeypatch):
    """GROK_HOME + group-dir .cwd must resolve sessions (official long-path layout)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "ghome"
        group = root / "sessions" / "slug-hash8"
        group.mkdir(parents=True)
        (group / ".cwd").write_text("/real/project/path\n", encoding="utf-8")
        sid = "019f0000-0000-7000-8000-000000000001"
        sdir = group / sid
        sdir.mkdir()
        (sdir / "summary.json").write_text(
            json.dumps({
                "session_summary": "hello",
                "created_at": "2026-07-16T00:00:00Z",
                "updated_at": "2026-07-16T01:00:00Z",
                "num_messages": 3,
                "current_model_id": "grok-test",
            }),
            encoding="utf-8",
        )
        (sdir / "chat_history.jsonl").write_text(
            json.dumps({"type": "user", "content": "hi"}) + "\n",
            encoding="utf-8",
        )
        (sdir / "signals.json").write_text(
            json.dumps({
                "toolFailureCount": 2,
                "toolCallCount": 9,
                "userMessageCount": 4,
                "assistantMessageCount": 5,
                "compactionCount": 1,
                "agentFilesTouched": 3,
                "primaryModelId": "from-signals",
            }),
            encoding="utf-8",
        )

        monkeypatch.setenv("GROK_HOME", str(root))
        # Re-import helpers constants that bind at import time
        import importlib
        import echolib._helpers as helpers
        import echolib._adapters as adapters
        importlib.reload(helpers)
        # adapters imports GROK_DIR at import — reload chain
        importlib.reload(adapters)

        assert helpers.GROK_DIR == root / "sessions"
        assert adapters._resolve_grok_project_cwd(group) == "/real/project/path"

        listed = adapters.grok_list_sessions(limit=10)
        assert any(e.session_id == sid for e in listed)
        hit = next(e for e in listed if e.session_id == sid)
        assert hit.project_path == "/real/project/path"
        assert Path(hit.full_path) == sdir

        stats = adapters._grok_session_stats(str(sdir))
        assert stats["errors"] == 2
        assert stats["tool_calls"] == 9
        assert stats["user_messages"] == 4
        assert stats["assistant_messages"] == 5
        assert stats["compactions"] == 1
        assert stats["files_edited"] == 3
        # summary model wins when present; signals fills when missing
        assert stats["model"] in ("grok-test", "from-signals")

        found = adapters.grok_session_path("/real/project/path", sid)
        assert found == sdir


def test_grok_stats_fallback_without_signals(tmp_path):
    """When signals.json is absent, events outcome error/failure still count."""
    sdir = tmp_path / "sess"
    sdir.mkdir()
    chat = sdir / "chat_history.jsonl"
    chat.write_text(
        "\n".join([
            json.dumps({"type": "user", "content": "do it"}),
            json.dumps({
                "type": "assistant",
                "tool_calls": [{"id": "1", "name": "run_terminal_command", "arguments": "{}"}],
            }),
            json.dumps({"type": "tool_result", "tool_call_id": "1", "content": "ok"}),
        ]) + "\n",
        encoding="utf-8",
    )
    (sdir / "events.jsonl").write_text(
        "\n".join([
            json.dumps({"type": "tool_started", "tool_name": "run_terminal_command", "ts": "t0"}),
            json.dumps({
                "type": "tool_completed",
                "tool_name": "run_terminal_command",
                "outcome": "failure",
                "ts": "t1",
            }),
        ]) + "\n",
        encoding="utf-8",
    )
    (sdir / "summary.json").write_text("{}", encoding="utf-8")

    sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
    from echolib._adapters import _grok_session_stats

    stats = _grok_session_stats(str(sdir))
    assert stats["errors"] >= 1
    assert stats["tool_calls"] >= 1
    assert stats["user_messages"] >= 1


def _usage_line(input_t, output_t, calls, cache=0, reason=0, models=None):
    """One ACP session/update line with top-level usage (billable source)."""
    total = input_t + output_t
    usage = {
        "inputTokens": input_t,
        "outputTokens": output_t,
        "totalTokens": total,
        "cachedReadTokens": cache,
        "reasoningTokens": reason,
        "modelCalls": calls,
        "numTurns": calls,
    }
    if models:
        usage["modelUsage"] = {
            m: {
                "inputTokens": input_t,
                "outputTokens": output_t,
                "totalTokens": total,
                "cachedReadTokens": cache,
                "reasoningTokens": reason,
                "modelCalls": calls,
            }
            for m in models
        }
    return json.dumps({
        "method": "session/update",
        "params": {"update": {"usage": usage}},
    })


def test_grok_billable_usage_single_run():
    """Single run: last (only) snapshot is the billable total."""
    from echolib._adapters import _grok_aggregate_billable_usage

    snaps = [
        {
            "input": 100, "output": 10, "total": 110, "cache_read": 50,
            "reasoning": 2, "calls": 1, "turns": 1, "models": ["grok-4.5"],
            "by_model": {"grok-4.5": {
                "input": 100, "output": 10, "cache_read": 50, "reasoning": 2, "calls": 1,
            }},
        },
        {
            "input": 300, "output": 40, "total": 340, "cache_read": 200,
            "reasoning": 8, "calls": 3, "turns": 3, "models": ["grok-4.5"],
            "by_model": {"grok-4.5": {
                "input": 300, "output": 40, "cache_read": 200, "reasoning": 8, "calls": 3,
            }},
        },
    ]
    agg = _grok_aggregate_billable_usage(snaps)
    assert agg["input"] == 300
    assert agg["output"] == 40
    assert agg["cache_read"] == 200
    assert agg["calls"] == 3
    assert agg["by_model"]["grok-4.5"]["input_tokens"] == 300
    assert agg["by_model"]["grok-4.5"]["output_tokens"] == 40


def test_grok_billable_usage_multi_run_segments(tmp_path):
    """modelCalls drop starts a new run — sum last-of-each-run (not max, not all)."""
    from echolib._adapters import _grok_session_stats

    sdir = tmp_path / "sess"
    sdir.mkdir()
    (sdir / "chat_history.jsonl").write_text("{}\n", encoding="utf-8")
    (sdir / "summary.json").write_text(
        json.dumps({"current_model_id": "grok-4.5"}), encoding="utf-8"
    )
    (sdir / "signals.json").write_text(
        json.dumps({
            "toolCallCount": 9,
            "userMessageCount": 2,
            "primaryModelId": "grok-4.5",
            # contextTokensUsed is NOT billable — must not leak into input_tokens
            "contextTokensUsed": 999999,
        }),
        encoding="utf-8",
    )
    # Run A: calls 1→3 (take last: in=300,out=40) model grok
    # Run B: calls drops to 1 then 2 (take last: in=80,out=20) model mimo
    # Expected billable: 380 / 60 / cache 250
    (sdir / "updates.jsonl").write_text(
        "\n".join([
            _usage_line(100, 10, 1, cache=40, reason=1, models=["grok-4.5"]),
            _usage_line(300, 40, 3, cache=200, reason=8, models=["grok-4.5"]),
            _usage_line(50, 5, 1, cache=10, reason=0, models=["grok-4.5"]),
            _usage_line(80, 20, 2, cache=50, reason=3, models=["mimo-v2.5"]),
        ]) + "\n",
        encoding="utf-8",
    )

    stats = _grok_session_stats(str(sdir))
    assert stats["input_tokens"] == 300 + 80
    assert stats["output_tokens"] == 40 + 20
    assert stats["cache_read_tokens"] == 200 + 50
    assert stats["total_tokens"] == stats["input_tokens"] + stats["output_tokens"]
    # signals activity still applied
    assert stats["tool_calls"] == 9
    assert stats["user_messages"] == 2
    # must not use contextTokensUsed as input
    assert stats["input_tokens"] != 999999
    assert stats["model"] == "grok-4.5"
    # Run A last → grok 300/40; Run B last → mimo 80/20 (snap3 grok mid-run dropped)
    mu = stats.get("model_usage") or {}
    assert mu["grok-4.5"]["input_tokens"] == 300
    assert mu["mimo-v2.5"]["input_tokens"] == 80
    assert mu["grok-4.5"]["output_tokens"] == 40
    assert mu["mimo-v2.5"]["output_tokens"] == 20
    assert sum(v["input_tokens"] for v in mu.values()) == stats["input_tokens"]
    assert sum(v["output_tokens"] for v in mu.values()) == stats["output_tokens"]


def test_grok_billable_usage_multi_model_same_snapshot(tmp_path):
    """One snapshot can split usage across multiple models; legs must sum to top."""
    from echolib._adapters import _grok_session_stats

    sdir = tmp_path / "sess"
    sdir.mkdir()
    (sdir / "chat_history.jsonl").write_text("{}\n", encoding="utf-8")
    (sdir / "summary.json").write_text("{}", encoding="utf-8")

    # Hand-craft usage where top-level = sum of two modelUsage legs
    usage = {
        "inputTokens": 1000,
        "outputTokens": 100,
        "totalTokens": 1100,
        "cachedReadTokens": 400,
        "reasoningTokens": 10,
        "modelCalls": 5,
        "numTurns": 5,
        "modelUsage": {
            "grok-4.5": {
                "inputTokens": 700, "outputTokens": 60, "totalTokens": 760,
                "cachedReadTokens": 300, "reasoningTokens": 10, "modelCalls": 3,
            },
            "deepseek-v4-pro": {
                "inputTokens": 300, "outputTokens": 40, "totalTokens": 340,
                "cachedReadTokens": 100, "reasoningTokens": 0, "modelCalls": 2,
            },
        },
    }
    (sdir / "updates.jsonl").write_text(
        json.dumps({"method": "session/update", "params": {"update": {"usage": usage}}}) + "\n",
        encoding="utf-8",
    )
    stats = _grok_session_stats(str(sdir))
    assert stats["input_tokens"] == 1000
    assert stats["output_tokens"] == 100
    mu = stats["model_usage"]
    assert mu["grok-4.5"]["input_tokens"] == 700
    assert mu["deepseek-v4-pro"]["input_tokens"] == 300
    assert sum(v["input_tokens"] for v in mu.values()) == stats["input_tokens"]
    assert sum(v["output_tokens"] for v in mu.values()) == stats["output_tokens"]
    assert sum(v["cache_read_tokens"] for v in mu.values()) == stats["cache_read_tokens"]


def test_grok_billable_usage_live_session_if_present():
    """Smoke: real ~/.grok session with updates.jsonl yields non-zero tokens."""
    from echolib._adapters import _grok_session_stats, grok_list_sessions

    home = Path.home() / ".grok" / "sessions"
    if not home.is_dir():
        pytest.skip("no local Grok sessions")
    hit = None
    for entry in grok_list_sessions(limit=30):
        sdir = Path(entry.full_path)
        if sdir.is_file():
            sdir = sdir.parent
        if (sdir / "updates.jsonl").is_file():
            # quick check file mentions usage
            text = (sdir / "updates.jsonl").read_text(encoding="utf-8", errors="replace")[:200000]
            if "inputTokens" in text:
                hit = sdir
                break
    if hit is None:
        pytest.skip("no updates.jsonl with usage on this machine")
    stats = _grok_session_stats(str(hit))
    assert stats["input_tokens"] > 0 or stats["output_tokens"] > 0
    assert stats["total_tokens"] == stats["input_tokens"] + stats["output_tokens"]
