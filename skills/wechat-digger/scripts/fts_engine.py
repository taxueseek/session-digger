#!/usr/bin/env python3
"""WeChat 自带 message_fts 全文检索引擎（解密副本）+ 数据覆盖审计。

覆盖 2022-02-28 → 现在（含归档期文本与链接 XML，1.77M+ 条）。
WCDB 的 MMFtsTokenizer 在纯 SQLite 里不可用，因此走 FTS5 content 表的
LIKE 检索（c0=原文, c1=local_id, c2=sort_seq, c4=session_id, c5=sender_id, c6=create_time 秒）。
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from wechat_schema import message_table_like_pattern

_FTS_TABLES = ("message_fts_v4_0", "message_fts_v4_1", "message_fts_v4_2", "message_fts_v4_3")


def _connect_ro(path: Any) -> sqlite3.Connection:
    """Open a vault database read-only, tolerating WAL-mode files.

    Plain ``mode=ro`` fails on a WAL-mode database that has no ``-shm``
    sidecar: SQLite must create shared memory to read it and refuses when it
    cannot. That is exactly the state of the vault's ``contact.db``, so the
    connection raised inside ``_NameBook`` and every chat/contact name
    silently degraded to its raw ``@chatroom`` id. Consequences measured
    downstream:

    * ``chat_quality``'s group-name signal (25-40 points, the strongest
      single noise indicator) never fired — a group literally named
      "示例线报群" scored 0 on its name.
    * Display names were unreadable everywhere names appear.

    ``immutable=1`` tells SQLite the file cannot change. That is true for
    these decrypted point-in-time vault copies, so it skips the WAL/shm
    machinery. It is a **fallback only**: when the plain read-only open
    succeeds we keep its stronger consistency guarantees, because a file
    that genuinely has pending WAL content must still be read through it.
    """
    last: Optional[sqlite3.Error] = None
    for suffix in ("?mode=ro", "?mode=ro&immutable=1"):
        con = sqlite3.connect(f"file:{path}{suffix}", uri=True)
        try:
            con.execute("SELECT 1")
            return con
        except sqlite3.Error as exc:
            con.close()
            last = exc
    raise sqlite3.OperationalError(f"cannot open read-only: {path} ({last})")


def fts_db_path() -> Optional[Path]:
    """定位解密的 message_fts.db。env WECHAT_FTS_DB 可覆盖。"""
    env = os.environ.get("WECHAT_FTS_DB")
    if env and Path(env).exists():
        return Path(env)
    root = os.environ.get("WECHAT_VAULT_ROOT")
    candidates = []
    if root:
        candidates.append(Path(root) / "decrypted" / "current" / "message" / "message_fts.db")
    base = Path.home() / "Library" / "Application Support" / "wechat-local-vault" / "decrypted"
    if base.exists():
        for d in sorted(base.glob("*/message/message_fts.db"), key=lambda p: p.stat().st_mtime, reverse=True):
            candidates.append(d)
    for p in candidates:
        if p.exists():
            return p
    return None


def vault_rich_start_ts() -> int:
    """message_0 富文本层起始（本机实测 2026-02-05）。用于 history 缺口补 FTS。"""
    return int(datetime.datetime(2026, 2, 5).timestamp())


def vault_msg_db_path() -> Optional[Path]:
    """定位解密的 message_0.db。"""
    env = os.environ.get("WECHAT_VAULT_ROOT")
    if env:
        p = Path(env) / "decrypted" / "current" / "message" / "message_0.db"
        return p if p.exists() else None
    base = Path.home() / "Library" / "Application Support" / "wechat-local-vault" / "decrypted"
    if base.exists():
        for d in sorted(base.glob("*/message/message_0.db"), key=lambda p: p.stat().st_mtime, reverse=True):
            return d
    return None


def _escape_like(keyword: str) -> str:
    """LIKE 通配符转义（配 ESCAPE '\\' 使用），防 %/_ 注入。"""
    return keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class _NameBook:
    """fts 域 id → username → 显示名（单一真源）。

    编号体系实测锚点（2026-09-06，本机库；c5 映射 300/300 与 Msg 表真值交叉验证）：
    - c4=session_id 与 c5=sender_id **同属** fts 库自带 name2id 域
      （username 单列、rowid 即序号）。
      session.db Name2Id(629行) / contact.db name2id(17391行) / message_0.db Name2Id(3527行)
      均为异体系，跨域直映射=无名+随机错名，禁止使用。
    - c5 曾在 v0.0.5.1 被误判「不可靠」：2715≠2837 是拿 c5 与 Msg.real_sender_id
      做跨域比较（不同域数值天然不等）。恢复 c5→fts.name2id 映射后，
      归档层（2022→2026-02）发送者身份全部可解析，且无需逐条反查 message_0.db。
    """

    def __init__(self, vault_current: Path):
        self._fts_user: dict[int, str] = {}
        self._display: dict[str, str] = {}
        self._loaded = False
        self._cdb: Optional[Path] = None
        cdb = vault_current / "contact" / "contact.db"
        if cdb.exists():
            self._cdb = cdb
        fdb = vault_current / "message" / "message_fts.db"
        try:
            if fdb.exists():
                con = _connect_ro(fdb)
                try:
                    self._fts_user = {r: u for r, u in con.execute("SELECT rowid, username FROM name2id")}
                finally:
                    con.close()
        except Exception:
            self._fts_user = {}
        if self._cdb:
            self._load_display_names()

    def _load_display_names(self) -> None:
        """contact 显示名一次性预载（remark > nick_name > username）。

        _resolve_sids 会对 name2id 全量 7888 项做 display 匹配，
        逐条开连接查询=数千次无索引扫描（实测 19s），必须批量。
        """
        try:
            con = _connect_ro(self._cdb)
            try:
                for u, r, n in con.execute("SELECT username, remark, nick_name FROM contact"):
                    if u:
                        self._display[u] = r or n or u
            finally:
                con.close()
            self._loaded = True
        except Exception:
            self._loaded = False  # 列缺失等异常时回退逐条查询

    def _build_pinyin(self) -> None:
        """显示名 → (全拼, 首字母串)，供拼音检索。pypinyin 缺失时静默跳过。

        只对名称层（<2 万项）做，消息正文不转拼音——找群/找人按「cg」
        「chengguan」命中「城管」是高频路径，正文拼音索引是另一档成本。
        """
        if getattr(self, "_pinyin", None) is not None or not self._loaded:
            return
        # 先查 index meta 持久化缓存：pypinyin 全量转换 ~2s（2 万名称），
        # CLI 每次新进程都重付会让拼音路径比 LIKE 还慢。缓存键带 contact 行数，
        # 联系人变化后自动重建。任何持久化失败都静默降级为现场构建。
        try:
            from paths import default_index_path
            ip = default_index_path()
            if ip.exists():
                _c = sqlite3.connect(str(ip))
                try:
                    row = _c.execute(
                        "SELECT value FROM index_meta WHERE key='pinyin_cache'"
                    ).fetchone()
                    if row:
                        payload = json.loads(row[0])
                        if payload.get("contact_count") == len(self._display):
                            self._pinyin = payload["py"]
                            return
                finally:
                    _c.close()
        except Exception:
            pass
        try:
            from pypinyin import lazy_pinyin, Style
        except Exception:
            self._pinyin = {}
            return
        py: dict[str, tuple[str, str]] = {}
        for name in self._display.values():
            if not name or name in py:
                continue
            try:
                full = "".join(lazy_pinyin(name)).lower()
                init = "".join(lazy_pinyin(name, style=Style.FIRST_LETTER)).lower()
                py[name] = (full, init)
            except Exception:
                continue
        self._pinyin = py
        try:
            from index_builder import _meta_set
            from paths import default_index_path
            ip2 = default_index_path()
            if ip2.exists():
                _c2 = sqlite3.connect(str(ip2))
                try:
                    _meta_set(_c2, "pinyin_cache", json.dumps(
                        {"contact_count": len(self._display), "py": py}, ensure_ascii=False))
                    _c2.commit()
                finally:
                    _c2.close()
        except Exception:
            pass

    def pinyin_match(self, query: str) -> list[str]:
        """返回拼音可匹配 query 的显示名集合。query 需为纯 ASCII。"""
        self._build_pinyin()
        out = []
        q = query.lower()
        for name, (full, init) in getattr(self, "_pinyin", {}).items():
            if q in full or (len(q) >= 2 and q in init):
                out.append(name)
        return out

    def display(self, username: str) -> str:
        if not username:
            return ""
        if username in self._display:
            return self._display[username]
        if getattr(self, "_loaded", False):
            # 批量预载成功：缺失=不在联系人表（群成员/openim 等），显示名即 username
            self._display[username] = username
            return username
        name = username
        try:
            con = _connect_ro(self._cdb)
            try:
                row = con.execute(
                    "SELECT COALESCE(NULLIF(remark,''), NULLIF(nick_name,''), username) FROM contact WHERE username=?",
                    (username,),
                ).fetchone()
                name = row[0] if row else username
            finally:
                con.close()
        except Exception:
            name = username
        self._display[username] = name
        return name

    @staticmethod
    def chat_type(username: str) -> str:
        if username.endswith("@chatroom"):
            return "group"
        if username.startswith("gh_"):
            return "official"
        if username.endswith("@openim"):
            return "openim"
        if username.startswith("wxid_wi_"):
            return "wework"
        return "single"

    def chat(self, sid: int) -> tuple[str, str]:
        """session_id → (username, display)。"""
        uname = self._fts_user.get(sid, "")
        return uname, self.display(uname) if uname else f"session:{sid}"

    def sender(self, sender_id: Any) -> str:
        """c5 → fts name2id → 显示名；查不到显式 u:<id>，不编造。"""
        try:
            sid = int(sender_id)
        except (TypeError, ValueError):
            return ""
        uname = self._fts_user.get(sid)
        if uname:
            return self.display(uname)
        return f"u:{sender_id}" if sender_id else ""


def _resolve_sids(book: "_NameBook", chat: Optional[str]) -> Optional[list[int]]:
    """chat 名 → session_id 列表；chat 为空返回 None（不过滤）。

    精确匹配优先：一个群名恰好等于某个会话的 username 或显示名时，只返回它。
    否则退回子串匹配（保留模糊查找）。没有这层优先，「理财日记2」会把
    「理财日记2.0」一并匹配进来——两个群的记录被静默混在一份结果里，
    而调用方看到的只是 count 变大，无从察觉串群。
    """
    if not chat:
        return None
    low = chat.lower()
    exact: list[int] = []
    fuzzy: list[int] = []
    for sid, uname in book._fts_user.items():
        disp = book.display(uname)
        if low == uname.lower() or low == disp.lower():
            exact.append(sid)
        elif low in uname.lower() or low in disp.lower():
            fuzzy.append(sid)
    if not exact and not fuzzy and chat.isascii():
        # 拼音兜底：「cg」/「chengguan」命中「城管…」——名称层转拼音后做包含匹配
        names = set(book.pinyin_match(chat))
        if names:
            for sid, uname in book._fts_user.items():
                if book.display(uname) in names:
                    fuzzy.append(sid)
    return sorted(exact or fuzzy)


def _fts_where(keyword: Optional[str], since: Optional[int], until: Optional[int], sids: Optional[list[int]]) -> tuple[str, list[Any]]:
    """统一构建 WHERE 子句。chat 过滤下推 SQL，保证 count 与 hits 同口径。

    keyword 为空时只做时间/会话过滤（fts_history 全量取数用）。
    """
    where = "1=1"
    params: list[Any] = []
    if keyword:
        where += " AND c0 LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(keyword)}%")
    if since:
        where += " AND c6 >= ?"
        params.append(since)
    if until:
        where += " AND c6 <= ?"
        params.append(until)
    if sids is not None:
        where += " AND c4 IN (%s)" % ",".join("?" * len(sids))
        params.extend(sids)
    return where, params


def _fullhist_search(
    keyword: str,
    chat: Optional[str],
    since: Optional[int],
    until: Optional[int],
    limit: int,
    with_meta: bool,
    rank: str = "time",
) -> Optional[Any]:
    """全史 FTS5 索引路径（毫秒级 MATCH 替代 900ms LIKE 全扫）。

    返回 None = 索引不可用/未建/任何异常，调用方回退 LIKE 全扫路径——
    正确性永远优先于速度，索引层只做加速器不做语义变更方。
    兜底异常不静默蒸发：持久化进 index meta（fullhist_last_error），
    doctor 跨进程可读——进程内全局变量跨不了进程边界，是冗余状态。
    """
    try:
        from index_builder import connect, _meta_get, _sync_incremental, fts_rowid_bounds
        from cjk import build_match_query
        from paths import default_index_path

        idx_path = default_index_path()
        if not idx_path.exists():
            return None
        con = sqlite3.connect(str(idx_path))
        con.row_factory = sqlite3.Row
        try:
            if _meta_get(con, "full_history") != "1":
                return None
            # 新鲜度：rowid 上界比对（0.3ms/表）。有新消息 → rowid 级增量同步；
            # 同步失败（锁/磁盘）不阻断——返回 None 走 LIKE 慢路径，结果照样正确。
            cur = fts_rowid_bounds()
            if cur is None:
                return None
            rec_raw = _meta_get(con, "fts_rowid_bounds")
            rec = json.loads(rec_raw) if rec_raw else {}
            if cur != rec:
                _sync_incremental(con, rec, cur)
            mq = build_match_query(keyword)
            if mq is None:
                return None
            # 语义分叉守卫：索引 MATCH 与 LIKE 的边界语义不同，分叉场景回退 LIKE
            # ①含空白：MATCH=隐式 AND，LIKE=含空格字面子串（「红包 二」索引1/LIKE0）
            # ②ASCII 词：前缀星只匹配 token 前缀，「SQL」查不到「PostgreSQL」中的子串
            if re.search(r"\s", keyword) or any(p.isascii() for p in re.findall(r"[A-Za-z0-9_]+", keyword)):
                return None
            # 列限定：messages_fts 有三列（text_cjk/nickname/chat_name），
            # 裸 MATCH 会把群名/昵称命中的消息也计入 total（LIKE 路径只搜正文）——
            # 实测「红包」索引 36832 vs LIKE 36817，多出的全是群名命中。
            where = "messages_fts MATCH ?"
            params: list[Any] = [f"text_cjk: {mq}"]
            if since:
                where += " AND m.ts >= ?"
                params.append(since)
            if until:
                where += " AND m.ts <= ?"
                params.append(until)
            if chat:
                # 与 LIKE 路径同源解析（NameBook sid 域），保证两路径 chat 过滤同口径。
                # 索引 chat_id 列存的是 username（NameBook.chat(sid)[0]），
                # 不是 sid 数字——直接拿 sid 匹配字符串列恒空，必须先转 uname。
                book = _NameBook(fts_db_path().parent.parent)
                sids = _resolve_sids(book, chat)
                if sids is not None and not sids:
                    return dict(hits=[], total=0, has_more=False, limit=limit) if with_meta else []
                if sids:
                    unames = [book._fts_user.get(s, "") for s in sids]
                    unames = [u for u in unames if u]
                    if not unames:
                        return dict(hits=[], total=0, has_more=False, limit=limit) if with_meta else []
                    where += f" AND m.chat_id IN ({','.join('?' * len(unames))})"
                    params.extend(unames)
            order = "ORDER BY bm25(messages_fts), m.ts DESC" if rank == "bm25" else "ORDER BY m.ts DESC"
            base_sql = f"""
              FROM messages_fts f
              JOIN messages m ON m.rowid = f.rowid
              WHERE {where}
            """
            total = con.execute(f"SELECT COUNT(*) {base_sql}", params).fetchone()[0]
            hits: list[dict] = []
            if limit:
                rows = con.execute(
                    f"SELECT m.id, m.chat_id, m.chat_name, m.sender, m.ts, m.text {base_sql} {order} LIMIT ?",
                    [*params, limit],
                ).fetchall()
                for r in rows:
                    sender = r["sender"] or ""
                    hits.append({
                        "id": r["id"],
                        "chat_id": r["chat_id"],
                        "chat_name": r["chat_name"] or r["chat_id"],
                        "chat_type": _NameBook.chat_type(r["chat_id"] or ""),
                        "sender": sender,
                        "sender_resolved": bool(sender and not sender.startswith("u:")),
                        "nickname": "",
                        "ts": r["ts"] or 0,
                        "msg_type": "text",
                        "text": r["text"] or "",
                        "source": "fts-index",
                    })
            out = {"hits": hits, "total": total, "has_more": total > len(hits), "limit": limit}
            if not hits and total == 0:
                out["suggestions"] = _fts_suggestions(con, keyword)
            return out if with_meta else hits
        finally:
            con.close()
    except Exception as e:
        _persist_fullhist_error(f"{type(e).__name__}: {e}")
        return None


def _persist_fullhist_error(message: str) -> None:
    """兜底异常落 index meta（doctor 跨进程可读）；持久化自身失败静默。"""
    try:
        from index_builder import _meta_set
        from paths import default_index_path
        ip = default_index_path()
        if not ip.exists():
            return
        ec = sqlite3.connect(str(ip))
        try:
            _meta_set(ec, "fullhist_last_error", message)
            ec.commit()
        finally:
            ec.close()
    except Exception:
        pass


def _fts_suggestions(con: sqlite3.Connection, keyword: str, max_n: int = 5) -> list[str]:
    """0 命中时的「你是不是要搜」。ASCII 词走 fts5vocab 词表前缀匹配。

    中文词不做——倒排里中文是单字 token，词表近似无意义；SymSpell 全量
    纠错需要词频词典构建，收益/复杂度比不足，留作后续。
    """
    out: list[str] = []
    try:
        q = keyword.strip()
        if q and q.isascii() and len(q) >= 3:
            con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.vocab_probe USING fts5vocab(messages_fts, row)")
            for (term,) in con.execute(
                "SELECT term FROM temp.vocab_probe WHERE term LIKE ? LIMIT ?", (f"{q[:4]}%", max_n)
            ):
                if term != q.lower():
                    out.append(term)
    except Exception:
        return []
    return out


def fts_search(
    keyword: str,
    chat: Optional[str] = None,
    since: Optional[Any] = None,
    until: Optional[Any] = None,
    limit: int = 50,
    with_meta: bool = False,
    rank: str = "time",
) -> Any:
    """全史文本检索。chat 支持群名/备注/username 模糊匹配。

    with_meta=True 时返回 {"hits": [...], "total": N, "has_more": bool, "limit": limit}；
    total 是关键词+全部过滤条件（含 chat）下的全量命中数，has_more=True 表示被 limit 截断。
    rank="bm25" 时按相关度排序（默认时间序，向后兼容）。
    """
    empty = {"hits": [], "total": 0, "has_more": False, "limit": limit}
    fts_path = fts_db_path()
    if not fts_path or not keyword:
        return dict(empty) if with_meta else []
    since, until = _as_epoch(since), _as_epoch(until)
    # 全史索引优先；索引不可用时才落到 LIKE 全扫（行为与历史版本完全一致）
    indexed = _fullhist_search(keyword, chat, since, until, limit, with_meta, rank=rank)
    if indexed is not None:
        return indexed
    book = _NameBook(fts_path.parent.parent)
    sids = _resolve_sids(book, chat)
    if sids is not None and not sids:
        return dict(empty) if with_meta else []
    where, params = _fts_where(keyword, since, until, sids)
    cap = max(limit * 3, 150)

    def _scan(table: str) -> list[tuple]:
        con = _connect_ro(fts_path)
        try:
            # COUNT(*) OVER()：窗口函数在 LIMIT 之前计算，单次扫描同时得到全量命中数
            return con.execute(
                f"SELECT c0, c4, c5, c6, COUNT(*) OVER() FROM {table}_content "
                f"WHERE {where} ORDER BY c6 DESC LIMIT ?",
                [*params, cap],
            ).fetchall()
        finally:
            con.close()

    with ThreadPoolExecutor(max_workers=len(_FTS_TABLES)) as pool:
        results = [f.result() for f in [pool.submit(_scan, t) for t in _FTS_TABLES]]

    total = 0
    rows: list[tuple] = []
    for rs in results:
        if rs:
            total += rs[0][4]
        rows.extend(rs)
    rows.sort(key=lambda r: r[3] or 0, reverse=True)

    hits: list[dict] = []
    for c0, sid, c5, ct, _cnt in rows:
        # 先判后加：limit=0 是合法请求（只要 total 不要正文），
        # 原「先 append 再比较」在 limit=0 时会多吐一条。
        if len(hits) >= limit:
            break
        uname, display = book.chat(sid)
        sender = book.sender(c5)
        hits.append({
            "id": f"fts:{sid}:{ct}",
            "chat_id": uname or f"session:{sid}",
            "chat_name": display,
            "chat_type": book.chat_type(uname) if uname else "unknown",
            "sender": sender,
            "sender_resolved": bool(sender and not sender.startswith("u:")),
            "nickname": "",
            "ts": ct or 0,
            "msg_type": "text",
            "text": c0 or "",
            "source": "fts",
        })
    if with_meta:
        return {"hits": hits, "total": total, "has_more": total > len(hits), "limit": limit}
    return hits


def _fullhist_group(
    keyword: str,
    chat: Optional[str],
    since: Optional[int],
    until: Optional[int],
    top: int,
) -> Optional[dict]:
    """聚合视图的索引路径：GROUP BY 下推 SQL，毫秒级替代 898ms LIKE 聚合。

    语义对齐 fts_group（LIKE 路径）：count/first_ts/last_ts 同源、
    chat 过滤先于 top-N 截断、text_cjk 列限定保 total 口径与 search 一致。
    任何异常返回 None 回退 LIKE 路径。
    """
    try:
        from index_builder import connect, _meta_get
        from cjk import build_match_query
        from paths import default_index_path

        idx_path = default_index_path()
        if not idx_path.exists():
            return None
        con = sqlite3.connect(str(idx_path))
        con.row_factory = sqlite3.Row
        try:
            if _meta_get(con, "full_history") != "1":
                return None
            # 与 search 同一道新鲜度闸门：有新消息先增量同步，防止聚合漏最新
            cur = fts_rowid_bounds()
            if cur is None:
                return None
            rec_raw = _meta_get(con, "fts_rowid_bounds")
            rec = json.loads(rec_raw) if rec_raw else {}
            if cur != rec:
                from index_builder import _sync_incremental
                _sync_incremental(con, rec, cur)
            mq = build_match_query(keyword)
            if mq is None:
                return None
            where = "messages_fts MATCH ?"
            params: list[Any] = [f"text_cjk: {mq}"]
            if since:
                where += " AND m.ts >= ?"
                params.append(since)
            if until:
                where += " AND m.ts <= ?"
                params.append(until)
            if chat:
                book = _NameBook(fts_db_path().parent.parent)
                sids = _resolve_sids(book, chat)
                if sids is not None and not sids:
                    return {"groups": [], "total_hits": 0, "total_groups": 0, "truncated": False}
                if sids:
                    unames = [book._fts_user.get(s, "") for s in sids]
                    unames = [u for u in unames if u]
                    if not unames:
                        return {"groups": [], "total_hits": 0, "total_groups": 0, "truncated": False}
                    where += f" AND m.chat_id IN ({','.join('?' * len(unames))})"
                    params.extend(unames)
            base_sql = f"""
              FROM messages_fts f
              JOIN messages m ON m.rowid = f.rowid
              WHERE {where}
            """
            total_hits = con.execute(f"SELECT COUNT(*) {base_sql}", params).fetchone()[0]
            total_groups = con.execute(
                f"SELECT COUNT(DISTINCT m.chat_id) {base_sql}", params).fetchone()[0]
            rows = con.execute(f"""
                SELECT m.chat_id, MAX(m.chat_name) AS chat_name,
                       COUNT(*) AS cnt, MIN(m.ts) AS mn, MAX(m.ts) AS mx
                {base_sql}
                GROUP BY m.chat_id
                ORDER BY cnt DESC
                LIMIT ?""", [*params, top]).fetchall()
            fmt = lambda x: datetime.datetime.fromtimestamp(x).strftime("%Y-%m-%d") if x else "-"
            groups = [{
                "chat_id": r["chat_id"],
                "chat_name": r["chat_name"] or r["chat_id"],
                "chat_type": _NameBook.chat_type(r["chat_id"] or ""),
                "count": r["cnt"],
                "first_ts": fmt(r["mn"]),
                "last_ts": fmt(r["mx"]),
            } for r in rows]
            return {"groups": groups, "total_hits": total_hits,
                    "total_groups": total_groups, "truncated": total_groups > top}
        finally:
            con.close()
    except Exception as e:
        _persist_fullhist_error(f"group/{type(e).__name__}: {e}")
        return None


def fts_group(
    keyword: str,
    chat: Optional[str] = None,
    since: Optional[Any] = None,
    until: Optional[Any] = None,
    top: int = 50,
) -> dict:
    """关键词命中按会话聚合（诊断/盘点主视图）。

    返回 {"groups": [{chat_id, chat_name, chat_type, count, first_ts, last_ts}],
          "total_hits": N, "total_groups": M, "truncated": bool}。
    chat 过滤在 SQL 层先于 top-N 截断，聚合视图同时暴露子串误命中
    （羊毛群噪声一眼可见）与真实热点会话。
    """
    empty = {"groups": [], "total_hits": 0, "total_groups": 0, "truncated": False}
    fts_path = fts_db_path()
    if not fts_path or not keyword:
        return dict(empty)
    since, until = _as_epoch(since), _as_epoch(until)
    # 索引优先（GROUP BY 下推，毫秒级）；不可用回退 LIKE 全扫聚合
    grouped = _fullhist_group(keyword, chat, since, until, top)
    if grouped is not None:
        return grouped
    book = _NameBook(fts_path.parent.parent)
    sids = _resolve_sids(book, chat)
    if sids is not None and not sids:
        return dict(empty)
    where, params = _fts_where(keyword, since, until, sids)

    merged: dict[int, list[int]] = {}

    def _agg(table: str) -> dict[int, list[int]]:
        con = _connect_ro(fts_path)
        try:
            out: dict[int, list[int]] = {}
            for sid, n, mn, mx in con.execute(
                f"SELECT c4, COUNT(*), MIN(c6), MAX(c6) FROM {table}_content WHERE {where} GROUP BY c4",
                params,
            ):
                out[sid] = [n, mn, mx]
            return out
        finally:
            con.close()

    with ThreadPoolExecutor(max_workers=len(_FTS_TABLES)) as pool:
        for fut in [pool.submit(_agg, t) for t in _FTS_TABLES]:
            for sid, (n, mn, mx) in fut.result().items():
                cur = merged.get(sid)
                if cur:
                    cur[0] += n
                    cur[1] = min(cur[1], mn)
                    cur[2] = max(cur[2], mx)
                else:
                    merged[sid] = [n, mn, mx]

    fmt = lambda x: datetime.datetime.fromtimestamp(x).strftime("%Y-%m-%d") if x else "-"
    groups = []
    for sid, (n, mn, mx) in sorted(merged.items(), key=lambda kv: -kv[1][0])[:top]:
        uname, display = book.chat(sid)
        groups.append({
            "chat_id": uname or f"session:{sid}",
            "chat_name": display,
            "chat_type": book.chat_type(uname) if uname else "unknown",
            "count": n,
            "first_ts": fmt(mn),
            "last_ts": fmt(mx),
        })
    total_groups = len(merged)
    return {
        "groups": groups,
        "total_hits": sum(v[0] for v in merged.values()),
        "total_groups": total_groups,
        "truncated": total_groups > len(groups),
    }


def _rows_to_messages(rows: list[tuple], book: "_NameBook") -> list[dict]:
    """fts 行 (c0,c4,c5,c6) → CanonicalMessage 形态（fts_history/fts_recent 共用）。"""
    msgs: list[dict] = []
    for c0, sid, c5, ct in rows:
        uname, display = book.chat(sid)
        sender = book.sender(c5)
        msgs.append({
            "id": f"fts:{sid}:{ct}",
            "chat_id": uname or f"session:{sid}",
            "chat_name": display,
            "chat_type": book.chat_type(uname) if uname else "unknown",
            "sender": sender,
            "sender_resolved": bool(sender and not sender.startswith("u:")),
            "nickname": "",
            "ts": ct or 0,
            "msg_type": "text",
            "text": c0 or "",
            "source": "fts",
        })
    return msgs


def fts_history(
    chat: str,
    since: Optional[Any] = None,
    until: Optional[Any] = None,
    limit: int = 50000,
) -> list[dict]:
    """单会话全史文本消息（2022-02→今，仅文本）。

    analyze/trends 的 fts 取数入口：vault 富文本层只有 2026-02 后，
    全周期分析（--all-time / 归档段）走这里。消息按时间升序，
    每表各取最早 limit 条后全局归并，保证截断时保留最早段。
    """
    fts_path = fts_db_path()
    if not fts_path or not chat:
        return []
    since, until = _as_epoch(since), _as_epoch(until)
    book = _NameBook(fts_path.parent.parent)
    sids = _resolve_sids(book, chat)
    if sids is not None and not sids:
        return []
    where, params = _fts_where(None, since, until, sids)

    def _scan(table: str) -> list[tuple]:
        con = _connect_ro(fts_path)
        try:
            return con.execute(
                f"SELECT c0, c4, c5, c6 FROM {table}_content WHERE {where} ORDER BY c6 ASC LIMIT ?",
                [*params, limit],
            ).fetchall()
        finally:
            con.close()

    with ThreadPoolExecutor(max_workers=len(_FTS_TABLES)) as pool:
        results = [f.result() for f in [pool.submit(_scan, t) for t in _FTS_TABLES]]

    rows = [r for rs in results for r in rs]
    rows.sort(key=lambda r: r[3] or 0)
    return _rows_to_messages(rows[:limit], book)


def fts_recent(
    since: Optional[Any] = None,
    until: Optional[Any] = None,
    limit: int = 200000,
) -> list[dict]:
    """跨会话窗口内全部文本消息（followups 扫描入口）。

    一次扫描取回窗口段全部会话的文本消息，返回时按时间升序（调用方按会话分组）。

    截断方向：超过 limit 时保留窗口内**最新**的一段。此前每表 `ORDER BY c6 ASC`
    再整体切前 limit 条，留下的是窗口最早的 20 万条——`--window 365` 实测覆盖
    2025-09-18→2026-03-08，最近半年整段丢失且无任何提示，分层/汇总/商机扫描
    因此系统性偏向旧数据。先按降序取最新，归并截断后再翻回升序。
    """
    fts_path = fts_db_path()
    if not fts_path:
        return []
    since, until = _as_epoch(since), _as_epoch(until)
    book = _NameBook(fts_path.parent.parent)
    where, params = _fts_where(None, since, until, None)

    def _scan(table: str) -> list[tuple]:
        con = _connect_ro(fts_path)
        try:
            return con.execute(
                f"SELECT c0, c4, c5, c6 FROM {table}_content WHERE {where} ORDER BY c6 DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        finally:
            con.close()

    with ThreadPoolExecutor(max_workers=len(_FTS_TABLES)) as pool:
        results = [f.result() for f in [pool.submit(_scan, t) for t in _FTS_TABLES]]

    rows = [r for rs in results for r in rs]
    rows.sort(key=lambda r: r[3] or 0, reverse=True)
    # 截断必须可见：此前悄悄丢掉窗口内一部分消息（方向还错了，丢的是最新段），
    # 分层/汇总/商机扫描据此下的结论无从察觉覆盖不全。
    truncated = len(rows) > limit or any(len(rs) >= limit for rs in results)
    rows = rows[:limit]
    rows.sort(key=lambda r: r[3] or 0)
    if truncated:
        print(
            f"[wx] 警告：窗口内文本消息超过 {limit} 条上限，只保留最新 {limit} 条；"
            "分层/汇总/商机类结论请缩小 --window 或调高上限后复算。",
            file=sys.stderr,
        )
    return _rows_to_messages(rows, book)


def _as_epoch(v: Optional[Any]) -> Optional[int]:
    """'YYYY-MM-DD' → epoch 秒；int 原样返回。"""
    if v is None:
        return None
    if isinstance(v, int):
        return v
    try:
        return int(datetime.datetime.strptime(str(v)[:10], "%Y-%m-%d").timestamp())
    except ValueError:
        return None


def coverage_data() -> dict[str, Any]:
    """vault 富文本层 + fts 全史文本层的跨度/总量/月度空洞审计（数据层）。"""
    out: dict[str, Any] = {"generated_at": datetime.datetime.now().isoformat(timespec="seconds")}

    def _span_scan(con: sqlite3.Connection, tables: list[str], ts_col: str) -> tuple[int, Any, Any, dict[str, int]]:
        """一次 GROUP BY 扫描同时得到总量、全局跨度、月度分布。

        此前每表跑两条全扫（COUNT/MIN/MAX 一条 + GROUP BY 一条），
        而 GROUP BY 的聚合里带上 MIN/MAX 后，全局跨度就是各月极值的极值、
        总量就是各月计数之和——同一条语句能给出全部四个值，扫描量减半。
        vault 层有 251 张会话表、fts 层 4 张百万行表，这一处是 coverage 的主要成本。
        """
        total, gmin, gmax = 0, None, None
        months: dict[str, int] = {}
        for t in tables:
            try:
                rows = con.execute(
                    f"SELECT strftime('%Y-%m', {ts_col}, 'unixepoch', 'localtime') m, "
                    f"COUNT(*), MIN({ts_col}), MAX({ts_col}) FROM {t} GROUP BY m"
                )
                for m, n, mn, mx in rows:
                    if m:
                        months[m] = months.get(m, 0) + n
                    total += n
                    if mn is not None and (gmin is None or mn < gmin):
                        gmin = mn
                    if mx is not None and (gmax is None or mx > gmax):
                        gmax = mx
            except Exception:
                continue
        return total, gmin, gmax, months

    fmt = lambda x: datetime.datetime.fromtimestamp(x).strftime("%Y-%m-%d") if x else "-"
    msg_db = vault_msg_db_path()
    if msg_db:
        con = _connect_ro(msg_db)
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?",
                (message_table_like_pattern(),),
            )]
            total, gmin, gmax, months = _span_scan(con, tables, "create_time")
        finally:
            con.close()
        holes = []
        if months:
            cur = datetime.datetime.strptime(min(months), "%Y-%m")
            end = datetime.datetime.strptime(max(months), "%Y-%m")
            while cur <= end:
                k = cur.strftime("%Y-%m")
                if months.get(k, 0) == 0:
                    holes.append(k)
                cur = (cur.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        out["vault"] = {
            "chats": len(tables), "messages": total, "span": [fmt(gmin), fmt(gmax)],
            "monthly_holes": holes,
            "note": "富文本全量层（含非文本消息），message_0 解密副本",
        }
    else:
        out["vault"] = {"error": "message_0.db 解密副本不存在，先跑 wd.py refresh"}

    fdb = fts_db_path()
    if fdb:
        fcon = _connect_ro(fdb)
        # c6 只存在于 content 影子表；虚拟表列名是 create_time
        try:
            ftotal, fmin, fmax, fmonths = _span_scan(fcon, [t + "_content" for t in _FTS_TABLES], "c6")
        finally:
            fcon.close()
        thin = sorted(m for m, n in fmonths.items() if m and n < 5000)
        out["fts"] = {
            "messages": ftotal, "span": [fmt(fmin), fmt(fmax)],
            "thin_months": thin,
            "note": "全史检索层。以 type=1 文本为主，另含 type=49 链接/文件 XML；不含图片/语音本体。",
        }
    else:
        out["fts"] = {"error": "message_fts.db 解密副本不存在"}

    extra: dict[str, Any] = {}
    vault_root = None
    if msg_db:
        vault_root = msg_db.parent.parent
    else:
        cand = Path.home() / "Library" / "Application Support" / "wechat-local-vault" / "decrypted" / "current"
        if cand.exists():
            vault_root = cand
    if vault_root is None:
        out["archives"] = {
            "shards": ["message_1.db", "message_2.db", "message_3.db", "message_4.db", "message_resource.db"],
            "status": "encrypted（passphrase 未缓存；keys.json 里归档钥是 message_0 克隆，校验失败）",
        }
        return out
    voice = vault_root / "message" / "media_0.db"
    if voice.exists():
        vcon = _connect_ro(voice)
        try:
            n, mn, mx = vcon.execute(
                "SELECT COUNT(*), MIN(create_time), MAX(create_time) FROM VoiceInfo"
            ).fetchone()
            extra["voice"] = {"rows": n, "span": [fmt(mn), fmt(mx)], "note": "media_0.VoiceInfo，含 voice_data 本体"}
        except Exception as exc:
            extra["voice"] = {"error": str(exc)}
        vcon.close()
    fav = vault_root / "favorite" / "favorite.db"
    if fav.exists():
        favcon = _connect_ro(fav)
        try:
            n, mn, mx = favcon.execute(
                "SELECT COUNT(*), MIN(update_time), MAX(update_time) FROM fav_db_item"
            ).fetchone()
            extra["favorites"] = {"rows": n, "span": [fmt(mn), fmt(mx)]}
        except Exception as exc:
            extra["favorites"] = {"error": str(exc)}
        favcon.close()
    biz = vault_root / "message" / "biz_message_0.db"
    if biz.exists():
        bcon = _connect_ro(biz)
        try:
            tabs = [r[0] for r in bcon.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?",
                (message_table_like_pattern(),),
            )]
            total, gmin, gmax, _m = _span_scan(bcon, tabs, "create_time")
            extra["biz"] = {"chats": len(tabs), "messages": total, "span": [fmt(gmin), fmt(gmax)]}
        except Exception as exc:
            extra["biz"] = {"error": str(exc)}
        bcon.close()
    if extra:
        out["extra_layers"] = extra

    out["archives"] = {
        "shards": ["message_1.db", "message_2.db", "message_3.db", "message_4.db", "message_resource.db"],
        "status": (
            "encrypted：keys.json 里 message_1..4 是 message_0 派生钥的克隆，"
            "HMAC/page1 校验失败；message_resource 有独立钥但仍校验失败。"
            "要解开需 WCDB passphrase（PBKDF2 password），不是再拷一份 enc_key。"
        ),
    }
    return out
