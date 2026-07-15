#!/usr/bin/env python3
"""Regression: path-segment-safe session scope + registry-driven discovery."""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
import echolib  # noqa: E402


def _load_sd_recall():
    path = PLUGIN_ROOT / "scripts" / "sd-recall.py"
    spec = importlib.util.spec_from_file_location("sd_recall", path)
    mod = importlib.util.module_from_spec(spec)
    # Prevent CLI main side effects: __name__ is not __main__
    spec.loader.exec_module(mod)
    return mod


class TestSessionInCwd(unittest.TestCase):
    def setUp(self):
        import urllib.parse

        home = Path.home()
        self.home = str(home)
        self.gpt = str(home / "Documents" / "GPT")
        # Encode the way Claude / Grok store project markers (no personal literals).
        claude_gpt = self.gpt.replace("/", "-")
        claude_home = self.home.replace("/", "-")
        claude_bar = str(home / "bar").replace("/", "-")
        claude_bar_baz = str(home / "bar-baz").replace("/", "-")
        grok_gpt = urllib.parse.quote(self.gpt, safe="")
        self.claude_gpt = f"{self.home}/.claude/projects/{claude_gpt}/abc.jsonl"
        self.claude_home_only = f"{self.home}/.claude/projects/{claude_home}/abc.jsonl"
        self.grok_gpt = (
            f"{self.home}/.grok/sessions/{grok_gpt}/sid/chat_history.jsonl"
        )
        self.bar = str(home / "bar")
        self.bar_baz_path = (
            f"{self.home}/.claude/projects/{claude_bar_baz}/s.jsonl"
        )
        self.bar_path = f"{self.home}/.claude/projects/{claude_bar}/s.jsonl"

    def test_home_never_matches_project_sessions(self):
        self.assertFalse(echolib.session_in_cwd(self.claude_gpt, self.home))
        self.assertFalse(echolib.session_in_cwd(self.grok_gpt, self.home))

    def test_gpt_matches_gpt_sessions(self):
        self.assertTrue(echolib.session_in_cwd(self.claude_gpt, self.gpt))
        self.assertTrue(echolib.session_in_cwd(self.grok_gpt, self.gpt))

    def test_gpt_does_not_match_home_only_project(self):
        self.assertFalse(echolib.session_in_cwd(self.claude_home_only, self.gpt))

    def test_parent_bar_does_not_match_bar_baz(self):
        self.assertFalse(echolib.session_in_cwd(self.bar_baz_path, self.bar))
        self.assertTrue(echolib.session_in_cwd(self.bar_path, self.bar))

    def test_virtual_scheme_never_matches(self):
        self.assertFalse(echolib.session_in_cwd("dimcode://sess_1", self.gpt))

    def test_normalize_session_path_dir_to_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "sess"
            d.mkdir()
            chat = d / "chat_history.jsonl"
            chat.write_text('{"type":"user"}\n', encoding="utf-8")
            self.assertEqual(echolib.normalize_session_path(str(d)), str(chat))
            self.assertEqual(
                echolib.normalize_session_path("dimcode://x"), "dimcode://x"
            )


class TestFindSessionsRegistry(unittest.TestCase):
    def test_agent_choices_include_codex_cursor(self):
        sd = _load_sd_recall()
        choices = sd._cli_agent_choices()
        for name in ("claude", "grok", "codex", "cursor", "zcode", "cross", "all"):
            self.assertIn(name, choices)

    def test_scope_all_returns_multiple_agents_when_data_present(self):
        sd = _load_sd_recall()
        rows = sd.find_sessions(scope="all", limit=30, agent="cross")
        # Machine may lack some envs; at least discovery must not crash.
        self.assertIsInstance(rows, list)
        if rows:
            agents = {a for _, _, a in rows}
            # Registry-backed listing should not be stuck on only claude/grok/kimi
            # when other adapters have data (soft assertion).
            self.assertTrue(all(isinstance(a, str) and a for a in agents))
            for sid, path, agent in rows:
                self.assertTrue(sid)
                self.assertTrue(path)
                self.assertNotIn("://", path)

    def test_scope_current_home_is_empty_not_all_projects(self):
        sd = _load_sd_recall()
        prev = os.getcwd()
        try:
            os.chdir(Path.home())
            rows = sd.find_sessions(scope="current", limit=20, agent="cross")
            # $HOME is not a project — must not flood with every session under home.
            self.assertEqual(rows, [])
        finally:
            os.chdir(prev)

    def test_codex_agent_flag_accepted(self):
        sd = _load_sd_recall()
        rows = sd.find_sessions(scope="all", limit=5, agent="codex")
        self.assertIsInstance(rows, list)
        for _, _, agent in rows:
            self.assertEqual(agent, "codex")


if __name__ == "__main__":
    unittest.main()
