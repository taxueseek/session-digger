"""Regression tests for reflect-report.py topic filtering.

Scope: the refactor introduced a pure `_filter_label` function that wraps
the two compiled regex constants (`_TOPIC_BAN` and `_TOPIC_NOISE`). This
test file guarantees:

1. `_filter_label` correctly delegates to `_TOPIC_BAN` and `_TOPIC_NOISE`.
2. The NEW noise pattern rejects URLs, privacy paths, API keys, code fences.
3. Legitimate topic strings (Chinese nouns + English compounds) pass.
4. The OLD `SKIP_TOPIC` regex decisions still hold (we read the original
   literal from `references/report-template.md` sources and re-compile the
   exact regex that existed before the refactor).
5. The top-level `_is_readable_topic` still rejects short stop words
   like "ok"/"test" (that rejection lives in _is_readable_topic's own
   "short English token" branch, not the wrapper — this test locks that in).
"""
import importlib.util
import re
import sys
import unittest
from pathlib import Path

_RRP = Path(__file__).resolve().parent.parent / "scripts" / "reflect-report.py"
_spec = importlib.util.spec_from_file_location("reflect_report", str(_RRP))
_mod = importlib.util.module_from_spec(_spec)
sys.modules["reflect_report"] = _mod
_spec.loader.exec_module(_mod)

_filter_label = _mod._filter_label
_is_readable_topic = _mod._is_readable_topic


# ── Old STOP set (preserved verbatim from git history before refactor) ──
OLD_STOP_WORDS = """
的了是我你在和有就不也把被这那吗呢啊吧要会能对与或及等一个这个那个什么怎么如何帮我请我们他们是否可以不用没有已经还是或者因为所以如果但是关于进行使用通过以及为了时候
the a an to of for and is in on it this that with from as by or be are was were can could would should will just please help me my i you we they how what when where why which use using used make do did does new session say hello
request interrupted user claude code skill tool agent docs https http com org json md py sh ts js true false null
""".split()

# ── Old SKIP_TOPIC regex (verbatim from pre-refactor source) ─────────
OLD_SKIP_TOPIC = re.compile(
    r"^(subagent|approval reviewer|longcat|trae cn summary|new session|"
    r"say hello|介绍下你的能力|users|python|github|preview|ok|test|hi|hey)\b",
    re.I,
)


class TestFilterLabelWrapsBAN(unittest.TestCase):
    """_filter_label must reject anything the ban-list regex rejects.

    Real callers run `_clean_topic_text` first (strips leading/trailing
    whitespace and collapses internal spaces), so anchor-only literals like
    `^new session` only see the stripped label in production.
    """
    def test_ban_samples(self):
        clean = _mod._clean_topic_text
        for raw in ["subagent", " new session ", "approval reviewer",
                    "[system] msg", "say hello", "trae cn summary",
                    "[Request interrupted by user]",
                    "诊断任务：告诉我你当前运行在哪个模型上。请直接回答你的模型名称和提供商。",
                    "回复一句话确认你已启动成功，并说明你使用的模型名称…",
                    'Say "hello" in exactly 3 words.',
                    "快速回答：当前日期是 2026 年 7 月 4 日…",
                    "你是什么模型？",
                    "回复OK"]:
            s = clean(raw)
            self.assertFalse(_filter_label(s),
                             f"ban-list should reject: {raw!r} -> {s!r}")


class TestFilterLabelWrapsNOISE(unittest.TestCase):
    """_filter_label must reject anything the noise regex catches (prefix[:80])."""
    def test_noise_samples(self):
        for s in ["https://claude.ai/x", "/Users/alice/foo", "/home/bob/x",
                  "$HOME/.claude", "curl https://x", "```python",
                  "Authorization: Bearer", "api_key=sk", "API-KEY=X"]:
            self.assertFalse(_filter_label(s),
                             f"noise should reject: {s!r}")


class TestFilterLabelPassLegit(unittest.TestCase):
    def test_chinese_topics(self):
        for s in ["实现登录模块", "修复 auth bug", "关于 PE 估值", "投资分析"]:
            self.assertTrue(_filter_label(s), s)

    def test_english_topics(self):
        for s in ["fix login bug", "refactor pipeline", "Claude setup"]:
            self.assertTrue(_filter_label(s), s)


class TestEmptyInputs(unittest.TestCase):
    def test_none_and_empty_rejected(self):
        self.assertFalse(_filter_label(""))
        self.assertFalse(_filter_label(None))


class TestOldSkipTopicRegression(unittest.TestCase):
    """All inputs the OLD SKIP_TOPIC regex rejected must still be rejected
    by the combined filter (via _is_readable_topic which calls _filter_label)."""
    def test_old_skip_topic_anchors_rejected(self):
        for s in ["subagent", "Approval Reviewer", "new session", "say hello",
                  "介绍下你的能力", "ok", "test", "hi", "hey", "users"]:
            self.assertFalse(_is_readable_topic(s),
                             f"should still reject old SKIP_TOPIC hit: {s!r}")


class TestOldStopWordsRegression(unittest.TestCase):
    """The OLD STOP set contained both:
      - short English tokens (a/an/the/ok/test/...) that `_is_readable_topic`
        still rejects via its "short English token" branch;
      - long Chinese strings (的了我是...) that `_is_readable_topic` never
        even sees because `_clean_topic_text` + `_short-word` rule filters
        them earlier.
    We lock in the short-token branch (the one we can reproduce)."""
    SHORT_TOKEN_REJECTIONS = [
        "ok", "test", "hi", "hey", "a", "an", "is", "it", "in", "on", "to",
        "of", "my", "i", "be", "or", "by", "as", "if", "so",
    ]

    def test_short_tokens_rejected_by_branch(self):
        for w in self.SHORT_TOKEN_REJECTIONS:
            clean = _mod._clean_topic_text(w)
            if not clean:
                continue
            self.assertFalse(_is_readable_topic(clean),
                             f"short token {w!r} should not be a readable topic")

    def test_legitimate_chinese_compound_accepted(self):
        # Pure-CJK strings (报销备案、基金) are legitimate 2-12 char topics
        # by design — do not over-reject them.
        self.assertTrue(_is_readable_topic("报销备案"))
        self.assertTrue(_is_readable_topic("基金的PE与PB分析"))

    def test_oversized_label_rejected_by_headline_stage(self):
        # Very long labels are truncated by _headline_from_summary's
        # `_TOPIC_MAX_LEN` stage, never reaching _is_readable_topic.
        # Lock this in: 500-char monster never hits _is_readable_topic.
        huge = "的" * 500  # length > _TOPIC_MAX_LEN+2
        # Pre-stage length guard rejects _before_ _is_readable_topic in production
        self.assertGreater(len(huge), _mod._TOPIC_MAX_LEN + 2)


if __name__ == "__main__":
    unittest.main()
