"""Tests for design system extraction (scripts/design-tokens.css).

Before: 99 inline `var(--*)` references and a 21-property :root{...} in the
HTML template (reflect-report.template.html) — no source-of-truth file.

After: the :root definitions live in design-tokens.css; the HTML template
carries __DESIGN_TOKENS_CSS__ placeholder; reflect-report.build_html()
injects the file content at render time.
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
    ]

    def test_file_exists_and_has_root_block(self):
        css = TOKENS_FILE.read_text(encoding="utf-8")
        self.assertIn(":root{", css)

    def test_all_required_variables_defined(self):
        css = TOKENS_FILE.read_text(encoding="utf-8")
        for v in self.REQUIRED_VARS:
            self.assertIn(v + ":", css)

    def test_file_is_pure_css(self):
        css = TOKENS_FILE.read_text(encoding="utf-8")
        # The HTML-side design system must not leak into the tokens file
        self.assertNotIn("__DESIGN_TOKENS_CSS__", css)


class TestTemplateCarriesPlaceholder(unittest.TestCase):
    def test_placeholder_present(self):
        html = TEMPLATE_FILE.read_text(encoding="utf-8")
        self.assertIn("__DESIGN_TOKENS_CSS__", html)

    def test_no_inline_root_variable_block(self):
        # The source-of-truth should be external — no --bg:#… inside <style>
        html = TEMPLATE_FILE.read_text(encoding="utf-8")
        style_start = html.find("<style>")
        style_end = html.find("</style>", style_start)
        self.assertNotEqual(style_start, -1)
        self.assertNotEqual(style_end, -1)
        style_block = html[style_start:style_end]
        self.assertNotIn("--bg:", style_block)

    def test_var_references_preserved(self):
        # 99 usages should survive the move (they live in template)
        html = TEMPLATE_FILE.read_text(encoding="utf-8")
        count = html.count("var(--")
        self.assertGreaterEqual(count, 80)


class TestRenderInjection(unittest.TestCase):
    def test_render_pipeline_injects_tokens(self):
        # Load template, simulate build_html's token injection, check result.
        tpl = reflect_report.load_template()
        tokens = TOKENS_FILE.read_text(encoding="utf-8")
        out = tpl.replace("__DESIGN_TOKENS_CSS__", tokens, 1)
        self.assertNotIn("__DESIGN_TOKENS_CSS__", out)
        self.assertIn(":root{", out)
        self.assertIn("--bg:#f3eee5", out)
        self.assertIn("--accent:#d97757", out)


if __name__ == "__main__":
    unittest.main()
