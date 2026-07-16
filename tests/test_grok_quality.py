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
