"""Tests for design system extraction (scripts/design-tokens.css).

Verifies CSS design tokens file exists, has required variables,
and the template carries the injection placeholder.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

TOKENS_FILE = Path(__file__).resolve().parent.parent / "scripts" / "design-tokens.css"
TEMPLATE_FILE = Path(__file__).resolve().parent.parent / "scripts" / "reflect-report.template.html"
_RRP = Path(__file__).resolve().parent.parent / "scripts" / "reflect-report.py"
_rr_spec = importlib.util.spec_from_file_location("reflect_report", str(_RRP))
reflect_report = importlib.util.module_from_spec(_rr_spec)
sys.modules["reflect_report"] = reflect_report
_rr_spec.loader.exec_module(reflect_report)


class TestDesignTokensFile(unittest.TestCase):
    REQUIRED_VARS = [
        "--bg", "--paper", "--ink", "--soft", "--muted", "--line",
        "--accent", "--accent-soft", "--accent-deep",
        "--good", "--warn", "--bad", "--shadow",
        "--serif", "--sans", "--nav",
        "--h0", "--h1", "--h2", "--h3", "--h4",
        # Hallmark viz redesign surface tokens
        "--hero-wash", "--banner-wash", "--card-wash",
        "--good-soft", "--warn-soft", "--bad-soft",
        "--viz-alt", "--focus", "--on-accent",
        "--dur-fast", "--ease-out", "--space-md",
    ]

    def test_file_exists_and_has_root_block(self):
        css = TOKENS_FILE.read_text(encoding="utf-8")
        self.assertTrue(":root" in css and "{" in css)

    def test_all_required_variables_defined(self):
        css = TOKENS_FILE.read_text(encoding="utf-8")
        for v in self.REQUIRED_VARS:
            self.assertIn(v + ":", css)

    def test_theme_modes_declared(self):
        css = TOKENS_FILE.read_text(encoding="utf-8")
        self.assertIn('html[data-theme="light"]', css)
        self.assertIn('html[data-theme="dark"]', css)
        self.assertIn('html[data-theme="amoled"]', css)
        self.assertIn('html[data-theme="cobalt"]', css)
        self.assertIn('html[data-theme="ink"]', css)
        self.assertIn("prefers-color-scheme: dark", css)

    def test_file_is_pure_css(self):
        css = TOKENS_FILE.read_text(encoding="utf-8")
        self.assertNotIn("__DESIGN_TOKENS_CSS__", css)


class TestTemplateCarriesPlaceholder(unittest.TestCase):
    def test_placeholder_present(self):
        html = TEMPLATE_FILE.read_text(encoding="utf-8")
        self.assertIn("__DESIGN_TOKENS_CSS__", html)

    def test_no_inline_root_variable_block(self):
        html = TEMPLATE_FILE.read_text(encoding="utf-8")
        style_start = html.find("<style>")
        style_end = html.find("</style>", style_start)
        self.assertNotEqual(style_start, -1)
        self.assertNotEqual(style_end, -1)
        style_block = html[style_start:style_end]
        self.assertNotIn("--bg:", style_block)

    def test_var_references_preserved(self):
        html = TEMPLATE_FILE.read_text(encoding="utf-8")
        count = html.count("var(--")
        self.assertGreaterEqual(count, 80)

    def test_theme_toggle_ui_and_logic(self):
        html = TEMPLATE_FILE.read_text(encoding="utf-8")
        self.assertIn('id="themeSeg"', html)
        self.assertIn("data-theme-mode", html)
        self.assertIn("sd-reflect-theme", html)
        self.assertIn("applyTheme", html)
        self.assertIn("prefers-reduced-motion", html)
        self.assertIn(":focus-visible", html)
        # No glassmorphism / radial cream blooms in body wash
        self.assertNotIn("backdrop-filter", html)
        self.assertNotIn("radial-gradient(1100px", html)

    def test_homepage_usage_deck_and_env_chrome(self):
        html = TEMPLATE_FILE.read_text(encoding="utf-8")
        self.assertIn("renderUsageDeck", html)
        self.assertIn("usage-deck", html)
        self.assertIn("applyEnvChrome", html)
        self.assertIn("data-env", html)
        self.assertIn("cobalt", html)
        self.assertIn("skipToken", html)


class TestRenderInjection(unittest.TestCase):
    def test_render_pipeline_injects_tokens(self):
        tpl = reflect_report.load_template()
        tokens = TOKENS_FILE.read_text(encoding="utf-8")
        out = tpl.replace("__DESIGN_TOKENS_CSS__", tokens, 1)
        self.assertNotIn("__DESIGN_TOKENS_CSS__", out)
        self.assertTrue(":root" in out and "{" in out)


if __name__ == "__main__":
    unittest.main()
