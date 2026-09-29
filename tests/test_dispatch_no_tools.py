#!/usr/bin/env python3
"""``--no-tools`` must mean the same thing in every environment.

Why this gate exists
--------------------
``sd-recall messages --no-tools`` reached only the claude default
implementation: ``dispatch_extract_messages`` called the registry adapter
without the flag, so the other 16 environments returned tool summaries as
if the flag had not been said — kimi emits one standalone ASSISTANT
message per tool call, entire text ``[TOOL: name]``, and those messages
flowed straight through. The user asked to omit tools and got them.

The dispatch now post-filters the registry path. These pins hold the
dispatch-level contract with a stub adapter (real kimi fixtures would test
the adapter, which is not the line that was broken):

  * registry path + no_tools=True  → ``[TOOL: …]``-only messages gone,
    mixed messages keep their non-tool lines;
  * registry path + no_tools=False → byte-identical passthrough;
  * claude keeps its native path (no_tools reaches the implementation,
    which is cheaper than a post-filter).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import echolib  # noqa: E402
from echolib import _adapters  # noqa: E402


class _StubAdapter:
    """A registry adapter whose messages look like kimi's: tool-call
    payloads come out as standalone ASSISTANT messages, whole text
    ``[TOOL: name]``, interleaved with real text."""

    NAME = "stub-no-tools-probe"

    @classmethod
    def install(cls):
        _adapters.ADAPTER_REGISTRY[cls.NAME] = {
            "extract_messages": cls._extract,
        }

    @classmethod
    def uninstall(cls):
        _adapters.ADAPTER_REGISTRY.pop(cls.NAME, None)

    @staticmethod
    def _extract(path, role="both", limit=0, thinking_limit=0):
        yield {"role": "USER", "timestamp": "2026-09-29T10:00:00", "text": "问题"}
        yield {"role": "ASSISTANT", "timestamp": "2026-09-29T10:00:01",
               "text": "[TOOL: Read] /tmp/x.py"}
        yield {"role": "ASSISTANT", "timestamp": "2026-09-29T10:00:02",
               "text": "[THINKING] 思考\n[TOOL: Bash] ls"}
        yield {"role": "ASSISTANT", "timestamp": "2026-09-29T10:00:03",
               "text": "结论：可以"}

    @staticmethod
    def resolve(path):
        return cls.NAME


class DispatchNoToolsTest(unittest.TestCase):
    def setUp(self):
        _StubAdapter.install()
        self._orig_resolve = _adapters.dispatch_resolve_agent
        _adapters.dispatch_resolve_agent = lambda path: _StubAdapter.NAME

    def tearDown(self):
        _adapters.dispatch_resolve_agent = self._orig_resolve
        _StubAdapter.uninstall()

    def _texts(self, no_tools):
        msgs = _adapters.dispatch_extract_messages("stub://x", no_tools=no_tools)
        return list(msgs)

    def test_no_tools_drops_tool_only_messages(self):
        texts = [m["text"] for m in self._texts(no_tools=True)]
        self.assertNotIn("[TOOL: Read] /tmp/x.py", texts)
        self.assertEqual(texts, ["问题", "[THINKING] 思考", "结论：可以"])

    def test_no_tools_keeps_non_tool_lines_of_mixed_messages(self):
        texts = [m["text"] for m in self._texts(no_tools=True)]
        self.assertIn("[THINKING] 思考", texts,
                      "thinking is thinking_limit's jurisdiction, not no_tools'")

    def test_no_tools_false_is_byte_identical_passthrough(self):
        self.assertEqual(self._texts(no_tools=False),
                         list(_StubAdapter._extract("stub://x")))

    def test_claude_native_path_still_receives_the_flag(self):
        # The claude default implementation must keep receiving no_tools
        # directly — the post-filter is for adapters that cannot.
        import inspect
        src = inspect.getsource(_adapters.dispatch_extract_messages)
        self.assertIn("no_tools=no_tools", src)


if __name__ == "__main__":
    unittest.main()
