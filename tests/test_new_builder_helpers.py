"""Tests for the pure-helper functions recently added to index_builder/_builder.py.

These were authored in the previous refactor but shipped with no assertions.
Now each function gets a golden-file-style test that locks in the
post-transfer contract.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_BUILDER = _SCRIPTS / "index_builder" / "_builder.py"

# Pre-load echolib package (parent dir: scripts/) so `import echolib` resolves.
echolib_spec = importlib.util.spec_from_file_location(
    "echolib", str(_SCRIPTS / "echolib" / "__init__.py"))
echolib_mod = importlib.util.module_from_spec(echolib_spec)
sys.modules["echolib"] = echolib_mod
echolib_spec.loader.exec_module(echolib_mod)

# Pre-load index_builder package (__init__ only re-exports _schema + _builder).
_pkg = _SCRIPTS / "index_builder"
ib_init = importlib.util.spec_from_file_location(
    "index_builder", str(_pkg / "__init__.py"),
    submodule_search_locations=[str(_pkg)])
ib_mod = importlib.util.module_from_spec(ib_init)
sys.modules["index_builder"] = ib_mod
ib_init.loader.exec_module(ib_mod)

_spec = importlib.util.spec_from_file_location(
    "index_builder._builder", str(_BUILDER),
    submodule_search_locations=[])
builder = importlib.util.module_from_spec(_spec)
sys.modules["index_builder._builder"] = builder
_spec.loader.exec_module(builder)


class TestCleanModelName(unittest.TestCase):
    def test_passthrough_plain(self):
        self.assertEqual(builder._clean_model_name("claude-sonnet-4-5"), "claude-sonnet-4-5")

    def test_strip_path_prefix(self):
        self.assertEqual(builder._clean_model_name("uuid/LongCat-2.0"), "LongCat-2.0")

    def test_drop_bracket_suffix(self):
        self.assertEqual(builder._clean_model_name("deepseek-v4-flash[1M]"), "deepseek-v4-flash")

    def test_http_prefix_preserved(self):
        # comma-strip must NOT strip leading "https://…" URLs
        self.assertEqual(builder._clean_model_name("https://api.example.com/Claude"),
                         "https://api.example.com/Claude")

    def test_none_and_empty(self):
        self.assertEqual(builder._clean_model_name(""), "")
        self.assertEqual(builder._clean_model_name(None), "")

    def test_strip_whitespace(self):
        # bare "LongCat" aliases to canonical LongCat-2.0 (Grok short names)
        self.assertEqual(builder._clean_model_name("  LongCat "), "LongCat-2.0")

    def test_alias_deepseek_flash(self):
        self.assertEqual(builder._clean_model_name("deepseek-flash"), "deepseek-v4-flash")

    def test_alias_longcat(self):
        self.assertEqual(builder._clean_model_name("longcat"), "LongCat-2.0")


class TestIsUsefulModel(unittest.TestCase):
    def test_generic_models_flse(self):
        for m in ["claude", "grok", "kimi", "codex", "zcode", "openai-custom", ""]:
            self.assertFalse(builder._is_useful_model(m), m)

    def test_specific_model_true(self):
        for m in ["LongCat-2.0", "claude-sonnet-4-5", "deepseek-v4-flash"]:
            self.assertTrue(builder._is_useful_model(m), m)

    def test_short_rejected(self):
        self.assertFalse(builder._is_useful_model("ab"))


class TestFirstUserPrompt(unittest.TestCase):
    def test_skips_system_ok_hi(self):
        messages = [
            {"role": "system", "text": "System: whatever"},
            {"role": "user", "text": "ok"},
            {"role": "human", "text": "[Request interrupted]"},
            {"role": "user", "text": "修复 auth bug 模块"},
        ]
        self.assertEqual(builder._first_user_prompt_from_messages(messages),
                         "修复 auth bug 模块")

    def test_empty_input(self):
        self.assertEqual(builder._first_user_prompt_from_messages([]), "")
        self.assertEqual(builder._first_user_prompt_from_messages(None), "")

    def test_no_user_role_returns_empty(self):
        messages = [{"role": "assistant", "text": "I'm thinking"}]
        self.assertEqual(builder._first_user_prompt_from_messages(messages), "")

    def test_truncates_long_prompt(self):
        messages = [{"role": "user", "text": "x" * 500}]
        self.assertEqual(len(builder._first_user_prompt_from_messages(messages)), 200)


class TestTextFromMessageBlob(unittest.TestCase):
    def test_string_passthrough(self):
        self.assertEqual(builder._text_from_message_blob(" hello "), "hello")

    def test_dict_text_key(self):
        self.assertEqual(builder._text_from_message_blob({"text": "hi"}), "hi")

    def test_list_of_dict_blocks(self):
        msg = {"content": [
            {"type": "thinking", "thinking": "ignore"},
            {"type": "text", "text": "hello"},
            {"type": "input_text", "text": "world"},
        ]}
        self.assertEqual(builder._text_from_message_blob(msg), "hello world")

    def test_empty_input(self):
        self.assertEqual(builder._text_from_message_blob(None), "")
        self.assertEqual(builder._text_from_message_blob({}), "")


if __name__ == "__main__":
    unittest.main()
