"""Kigi CLI — universal discovery (no dedicated adapter).

Covers the v0.9.14 requirement: .kigi is NOT adapted (no ADAPTER_REGISTRY
entry), but its grok-style sessions are discovered by the universal
SchemaProbe path:

- KNOWN_UNADAPTED registers ~/.kigi/sessions with adapter defaulting to
  "universal"
- _scan_via_adapter scopes universal listing to the env root and derives
  unique session ids from the path (session uuid), not the file stem
- universal_list_sessions filters grok-family auxiliary files
  (updates.jsonl / events.jsonl / rewind_points.jsonl …) so they never
  become fake "sessions"
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_ROOT))

from index_builder._builder import _scan_via_adapter  # noqa: E402
from echolib._adapters import universal_list_sessions  # noqa: E402
from echolib._registry_data import KNOWN_UNADAPTED  # noqa: E402


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def _make_kigi_home(tmp: Path):
    """Grok-style ~/.kigi/sessions/{project}/{uuid}/ layout with noise files."""
    root = tmp / "kigi-home" / "sessions"
    for i, project in enumerate(("%2Ftmp", "%2FUsers%2Falice")):
        for j in range(2):
            sess = root / project / f"019f{uuid_hex(i, j)}-0000-7000-8000-00000000000{j}"
            sess.mkdir(parents=True, exist_ok=True)
            _write_jsonl(sess / "chat_history.jsonl", [
                {"type": "user", "content": f"<user_query>kigi 提问 {i}-{j}</user_query>"},
                {"type": "assistant", "content": f"kigi 回答 {i}-{j}"},
            ])
            _write_jsonl(sess / "updates.jsonl", [{"type": "usage", "usage": {}}])
            _write_jsonl(sess / "events.jsonl", [{"type": "tool_completed"}])
            _write_jsonl(sess / "rewind_points.jsonl", [{"type": "rewind"}])
    return root


def uuid_hex(i: int, j: int) -> str:
    return f"0000000{i}{j}"


class TestKigiUniversalDiscovery(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._root = _make_kigi_home(Path(self._td.name))

    def tearDown(self):
        self._td.cleanup()

    def test_kigi_registered_as_unadapted_universal(self):
        info = KNOWN_UNADAPTED["kigi"]
        self.assertEqual(info["root"], "~/.kigi/sessions/")
        # 无专用适配器：adapter 默认 universal
        self.assertEqual(info.get("adapter", "universal"), "universal")

    def test_scan_via_adapter_kigi_only_chat_history_unique_ids(self):
        out = _scan_via_adapter("universal", "kigi", home_dir=str(self._root))
        # 4 个会话，每个只落 1 条 chat_history.jsonl（噪音文件被过滤）
        self.assertEqual(len(out), 4)
        agents = {a for _, _, a in out}
        self.assertEqual(agents, {"universal"})
        paths = [p for _, p, _ in out]
        self.assertTrue(all(p.endswith("chat_history.jsonl") for p in paths))
        # sid 按会话 uuid 派生，全部唯一
        sids = [s for s, _, _ in out]
        self.assertEqual(len(set(sids)), 4)
        self.assertTrue(all("0000000" in s for s in sids))

    def test_universal_list_sessions_filters_noise(self):
        s = universal_list_sessions(home_dir=str(self._root), env_name="kigi", limit=100)
        names = {Path(x.full_path).name for x in s}
        self.assertEqual(names, {"chat_history.jsonl"})
        self.assertEqual(len(s), 4)


if __name__ == "__main__":
    unittest.main()
