"""CJK-aware tokenization bridging for FTS5 unicode61.

unicode61 treats a contiguous run of CJK characters as ONE token, so Chinese
substring queries never match ("迁移" cannot hit "数据库迁移完成"). SQLite's
trigram tokenizer does not help either: MATCH on a 2-character CJK word
(the most common Chinese word length) still returns 0 rows.

Fix: split every CJK run into per-character tokens on WRITE, and rebuild the
same per-character phrase on QUERY. unicode61 then treats each character as a
token and the phrase operator restores contiguity. English/digit runs keep
default unicode61 behavior and get a prefix star on query for partial matches.

Contract: WRITE side must use split_cjk(), QUERY side must use
build_match_query(), any code that DISPLAYS text read back from messages_fts
must run it through uncjk() first, and any OVERLAP/SIMILARITY heuristic must
tokenize with tokenize() instead of str.split(). Asymmetric use silently breaks
recall or turns the heuristic into noise — keep the four functions together.
"""
import re

# CJK ideographs (basic + Ext A/Compat + Ext B+), kana, hangul syllables —
# the contiguous runs unicode61 would otherwise swallow whole.
_CJK_CLASS = (
    "\u3040-\u30ff"     # kana
    "\u3400-\u4dbf"     # ideograph ext A
    "\u4e00-\u9fff"     # ideographs
    "\uf900-\ufaff"     # compat ideographs
    "\uac00-\ud7af"     # hangul syllables
    "\U00020000-\U0002fa1f"  # ideograph ext B..F
)
_CJK_RUN = re.compile(f"[{_CJK_CLASS}]+")
_TOKEN = re.compile(f"[{_CJK_CLASS}]+|[A-Za-z0-9_]+")

# 切分边界：CJK|CJK 之外，还必须切 ASCII字母数字|CJK 与 CJK|ASCII，
# 以及 符号/emoji|任何 token 字符。实测两个坑（SQLite 3.53 unicode61）：
# 「2红包」→「2红 包」数字+CJK 粘成 token；「红包🧧可」→ emoji 并入 token
# （unicode61 只认空白与部分标点为分隔，So 类符号不是）——两者都让
# phrase "红 包" 断裂漏召回。符号两侧插空格后，无论符号被归为
# 分隔符还是 token 字符，CJK 相邻性都与原文一致。
_SPLIT = re.compile(
    f"(?<=[{_CJK_CLASS}])(?=[{_CJK_CLASS}])"
    f"|(?<=[A-Za-z0-9])(?=[{_CJK_CLASS}])"
    f"|(?<=[{_CJK_CLASS}])(?=[A-Za-z0-9])"
    f"|(?<=[{_CJK_CLASS}A-Za-z0-9])(?=[^\\s{_CJK_CLASS}A-Za-z0-9])"
    f"|(?<=[^\\s{_CJK_CLASS}A-Za-z0-9])(?=[{_CJK_CLASS}A-Za-z0-9])"
)


def split_cjk(text: str) -> str:
    """Insert a space between every CJK character (write-side transform).

    同时在 CJK 与 ASCII 字母数字的粘连处插空格，保证 unicode61 下
    CJK 单字 token 的相邻性 = 原文的连续子串语义。
    """
    if not text:
        return text
    return _SPLIT.sub(" ", text)


def uncjk(text: str) -> str:
    """Inverse of split_cjk for display: collapse split_cjk-inserted spaces.

    split_cjk 在 CJK|CJK 与 ASCII|CJK 两侧都插了空格，逆变换两侧都要收；
    纯 ASCII run 之间的空格（"use git"）原样保留。
    """
    if not text:
        return text
    return re.sub(
        f"(?<=[{_CJK_CLASS}]) +(?=[{_CJK_CLASS}A-Za-z0-9])"
        f"|(?<=[A-Za-z0-9]) +(?=[{_CJK_CLASS}])"
        f"|(?<=[{_CJK_CLASS}A-Za-z0-9]) +(?=[^\\s{_CJK_CLASS}A-Za-z0-9])"
        f"|(?<=[^\\s{_CJK_CLASS}A-Za-z0-9]) +(?=[{_CJK_CLASS}A-Za-z0-9])",
        "",
        text,
    )


def tokenize(text: str) -> list[str]:
    """Lowercased tokens: one per CJK character, one per ASCII/digit run.

    Analysis-side companion to the write/query pair above. Overlap, similarity
    and keyword heuristics must use this rather than ``str.split()``: a
    whitespace split returns an entire Chinese sentence as a single token, so
    any two distinct Chinese messages score as completely dissimilar.

    Not yet the only tokenizer of this shape. ``scripts/topic-segmenter.py``
    carries ``simple_tokenize``, a regex equivalent written before this
    existed, and the two are **not identical**: on ``a-b_c/d`` this returns
    ``['a', 'b_c', 'd']`` (underscore joins a run) while that one returns
    ``['a', 'b', 'c', 'd']``. Everywhere else they agree, and they agree on the
    CJK behaviour the heuristics actually depend on. Consolidating them would
    change the topic boundaries ``/topics`` reports, which needs a measurement
    first — so until then, a change to the token definition has two homes.
    """
    if not text:
        return []
    return [t.lower() for t in _TOKEN.findall(split_cjk(text))]


def build_match_query(keyword: str, max_terms: int = 12):
    """Rewrite a user keyword into a safe FTS5 MATCH expression.

    - CJK run  → per-character phrase:  "迁移" → "迁 移"
    - ASCII    → prefix query:          Postgre → "Postgre"*
    - everything else (punctuation, FTS5 operators like AND/NEAR, quotes,
      colons) is dropped, which also closes the query-injection hole.

    Returns None when nothing searchable remains.
    """
    if not keyword or not keyword.strip():
        return None
    parts = _TOKEN.findall(keyword)
    if len(parts) > max_terms:
        # 静默截断会让长查询按更短查询匹配（命中超集、结论翻转）——宁可回退 LIKE
        return None
    out = []
    for part in parts:
        if _CJK_RUN.fullmatch(part):
            out.append('"' + " ".join(part) + '"')
        else:
            out.append(f'"{part}"*')
    return " ".join(out) if out else None
