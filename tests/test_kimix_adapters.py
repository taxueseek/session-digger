"""Kimix CLI adapter quality — sessions, usage, cache metrics, unified log.

Covers the v0.9.19+ Kimix adapter surface:
- kimix_list_sessions (summary.json based, cwd filter)
- kimix_session_stats (updates.jsonl usage → cache_hit_rate;
  unified.jsonl fallback when updates.jsonl has no usage)
- kimix_extract_messages (system / <user_info> noise filtering)
- kimix_cache_metrics (metrics/cache_hit-*.jsonl per-request aggregation)
- kimix_unified_cache_index (logs/unified.jsonl inference_done per sid)

Fixtures are built in temp directories; nothing depends on a real home.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_ROOT))
_ECHOLIB = _ROOT / "echolib" / "__init__.py"
_spec = importlib.util.spec_from_file_location("echolib", str(_ECHOLIB))
echolib = importlib.util.module_from_spec(_spec)
sys.modules["echolib"] = echolib
_spec.loader.exec_module(echolib)

from echolib import _adapters_kimix as kimix  # noqa: E402

SID = "019f0000-0000-7000-8000-000000000001"


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def _make_home(tmp: Path, *, with_updates_usage=True, with_unified=True):
    """Build a fake ~/.kimix layout; returns the session dir path."""
    sess_dir = tmp / "sessions" / "%2FUsers%2Falice" / SID
    sess_dir.mkdir(parents=True)
    summary = {
        "info": {"id": SID, "cwd": "/Users/alice/proj"},
        "session_summary": "kimix test session",
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T01:00:00Z",
        "current_model_id": "deepseek-v4-flash",
        "num_messages": 3,
    }
    (sess_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")

    chat = [
        {"type": "system", "content": "You are Kimix, an unofficial CLI."},
        {"type": "user", "content": [
            {"type": "text", "text": "<user_info>\nOS: macos\n</user_info>\n\n<git_status>clean</git_status>"}
        ]},
        {"type": "user", "content": [{"type": "text", "text": "真正的用户问题"}]},
        {"type": "assistant", "content": "回答",
         "tool_calls": [{"id": "c1", "name": "run_terminal_command",
                         "arguments": "{\"command\":\"ls\"}"}]},
        {"type": "tool_result", "content": "Exit Code: 1\nboom"},
        {"type": "reasoning", "content": [{"type": "text", "text": "think"}]},
    ]
    _write_jsonl(sess_dir / "chat_history.jsonl", chat)

    updates = [
        {"timestamp": 1785500000, "method": "session/update", "params": {
            "sessionId": SID, "update": {"sessionUpdate": "turn_started"}}},
        {"timestamp": 1785500001, "method": "session/update", "params": {
            "sessionId": SID, "update": {"sessionUpdate": "turn_completed", "usage": {
                "inputTokens": 1000, "outputTokens": 100, "totalTokens": 1100,
                "cachedReadTokens": 900, "modelCalls": 1,
                "modelUsage": {"deepseek-v4-flash": {
                    "inputTokens": 1000, "outputTokens": 100,
                    "cachedReadTokens": 900}}}}}},
    ]
    _write_jsonl(sess_dir / "updates.jsonl", updates)

    events = [
        {"type": "turn_started"},
        {"type": "tool_completed", "outcome": "error"},
    ]
    _write_jsonl(sess_dir / "events.jsonl", events)

    # metrics/cache_hit-*.jsonl (0.1.16 per-request source)
    metrics_dir = tmp / "metrics"
    metrics_dir.mkdir(exist_ok=True)
    (metrics_dir / "cache_hit-2026-08-01.jsonl").write_text(
        "\n".join([
            json.dumps({"type": "cache_hit", "ts_ms": 1, "request_id": "r1",
                        "prompt_tokens": 100, "cached_tokens": 90,
                        "cache_hit_percent": 90.0}),
            json.dumps({"type": "process_summary", "pid": 1, "requests": 1,
                        "prompt_tokens": 100, "cached_tokens": 90,
                        "cache_hit_percent": 90.0}),
        ]) + "\n",
        encoding="utf-8",
    )

    # logs/unified.jsonl (0.1.16 per-request + sid source)
    if with_unified:
        log_dir = tmp / "logs"
        log_dir.mkdir(exist_ok=True)
        (log_dir / "unified.jsonl").write_text(
            "\n".join([
                json.dumps({"ts": "2026-08-01T00:00:00Z", "src": "shell", "pid": 7,
                            "lvl": "info", "sid": SID,
                            "msg": "shell.turn.inference_done",
                            "ctx": {"prompt_tokens": 500,
                                    "cached_prompt_tokens": 450,
                                    "completion_tokens": 50,
                                    "reasoning_tokens": 0}}),
                json.dumps({"ts": "2026-08-01T00:00:01Z", "src": "shell", "pid": 7,
                            "lvl": "info", "sid": "other-sid",
                            "msg": "shell.turn.inference_done",
                            "ctx": {"prompt_tokens": 10,
                                    "cached_prompt_tokens": 0,
                                    "completion_tokens": 5}}),
                json.dumps({"ts": "2026-08-01T00:00:02Z", "src": "shell", "pid": 7,
                            "lvl": "info", "sid": SID,
                            "msg": "shell.turn.inference_done",
                            "ctx": {"prompt_tokens": 500,
                                    "cached_prompt_tokens": 450,
                                    "completion_tokens": 50}}),
            ]) + "\n",
            encoding="utf-8",
        )

    return sess_dir


class KimixAdapterTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self._orig_dir = kimix.KIMIX_DIR
        kimix.KIMIX_DIR = Path(self._td.name)
        kimix._UNIFIED_CACHE["mtime"] = None
        kimix._UNIFIED_CACHE["data"] = None

    def tearDown(self):
        kimix.KIMIX_DIR = self._orig_dir
        kimix._UNIFIED_CACHE["mtime"] = None
        kimix._UNIFIED_CACHE["data"] = None

    def test_list_sessions_basic_and_cwd_filter(self):
        _make_home(Path(self._td.name))
        all_sessions = kimix.kimix_list_sessions(limit=50)
        self.assertEqual(len(all_sessions), 1)
        self.assertEqual(all_sessions[0].session_id, SID)
        self.assertEqual(all_sessions[0].project_path, "/Users/alice/proj")

        # cwd 过滤：不匹配的 cwd 返回空
        filtered = kimix.kimix_list_sessions(cwd="/nonexistent", limit=50)
        self.assertEqual(filtered, [])

    def test_session_stats_cache_hit_from_updates(self):
        sess_dir = _make_home(Path(self._td.name))
        stats = kimix.kimix_session_stats(str(sess_dir))
        self.assertEqual(stats["agent"], "kimix")
        self.assertAlmostEqual(stats["cache_hit_rate"], 0.9, places=3)
        self.assertEqual(stats["cache_source"], "updates")
        self.assertEqual(stats["total_tokens"], 1100)

    def test_session_stats_unified_fallback_when_no_usage(self):
        sess_dir = _make_home(Path(self._td.name), with_updates_usage=False)
        # 空 usage 的 updates.jsonl（无 inputTokens/outputTokens 行）
        (sess_dir / "updates.jsonl").write_text(
            json.dumps({"timestamp": 1, "method": "session/update", "params": {
                "sessionId": SID, "update": {"sessionUpdate": "turn_started"}}}) + "\n",
            encoding="utf-8",
        )
        stats = kimix.kimix_session_stats(str(sess_dir))
        self.assertEqual(stats["cache_source"], "unified")
        # unified 两条记录：prompt=1000, cached=900 → 0.9
        self.assertAlmostEqual(stats["cache_hit_rate"], 0.9, places=3)
        self.assertEqual(stats["cache_requests"], 2)
        self.assertEqual(stats["total_tokens"], 1000 + 100)

    def test_extract_messages_filters_noise(self):
        sess_dir = _make_home(Path(self._td.name))
        msgs = list(kimix.kimix_extract_messages(str(sess_dir), role="user"))
        # 只有「真正的用户问题」；<user_info> 包裹的系统上下文被过滤
        texts = [m.get("text", "") for m in msgs]
        self.assertEqual(len(texts), 1)
        self.assertIn("真正的用户问题", texts[0])
        self.assertNotIn("user_info", texts[0])

    def test_cache_metrics_aggregation(self):
        _make_home(Path(self._td.name))
        report = kimix.kimix_cache_metrics()
        total = report["total"]
        self.assertEqual(total["requests"], 1)  # process_summary 不计
        self.assertEqual(total["prompt_tokens"], 100)
        self.assertEqual(total["cached_tokens"], 90)
        self.assertAlmostEqual(total["weighted_hit_rate"], 0.9, places=3)
        self.assertIn("2026-08-01", report["by_date"])

    def test_unified_cache_index_groups_by_sid(self):
        _make_home(Path(self._td.name))
        index = kimix.kimix_unified_cache_index(force=True)
        self.assertEqual(index[SID]["requests"], 2)
        self.assertEqual(index[SID]["prompt_tokens"], 1000)
        self.assertEqual(index[SID]["cached_prompt_tokens"], 900)
        self.assertEqual(index["other-sid"]["requests"], 1)


if __name__ == "__main__":
    unittest.main()
