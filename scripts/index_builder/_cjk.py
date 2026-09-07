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
build_match_query(), and any code that DISPLAYS text read back from
messages_fts must run it through uncjk() first. Asymmetric use silently
breaks recall — keep the three functions together.
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


def split_cjk(text: str) -> str:
    """Insert a space between every CJK character (write-side transform)."""
    if not text:
        return text
    return _CJK_RUN.sub(lambda m: " ".join(m.group(0)), text)


def uncjk(text: str) -> str:
    """Inverse of split_cjk for display: collapse spaces between CJK chars.

    Spaces between non-CJK runs (e.g. "use git") are preserved.
    """
    if not text:
        return text
    return re.sub(f"(?<=[{_CJK_CLASS}]) +(?=[{_CJK_CLASS}])", "", text)


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
    parts = _TOKEN.findall(keyword)[:max_terms]
    out = []
    for part in parts:
        if _CJK_RUN.fullmatch(part):
            out.append('"' + " ".join(part) + '"')
        else:
            out.append(f'"{part}"*')
    return " ".join(out) if out else None
