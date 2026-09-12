#!/usr/bin/env python3
"""WeChat 自带 message_fts 全文检索引擎（解密副本）+ 数据覆盖审计。

覆盖 2022-02-28 → 现在（含 message_1..4 归档分片的文本内容，1.75M+ 条）。
WCDB 的 MMFtsTokenizer 在纯 SQLite 里不可用，因此走 FTS5 content 表的
LIKE 检索（c0=原文, c1=local_id, c2=sort_seq, c4=session_id, c5=sender_id, c6=create_time 秒）。
"""
from __future__ import annotations

import datetime
import os
import sqlite3
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
                self._fts_user = {r: u for r, u in con.execute("SELECT rowid, username FROM name2id")}
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
            for u, r, n in con.execute("SELECT username, remark, nick_name FROM contact"):
                if u:
                    self._display[u] = r or n or u
            con.close()
            self._loaded = True
        except Exception:
            self._loaded = False  # 列缺失等异常时回退逐条查询

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
            row = con.execute(
                "SELECT COALESCE(NULLIF(remark,''), NULLIF(nick_name,''), username) FROM contact WHERE username=?",
                (username,),
            ).fetchone()
            name = row[0] if row else username
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
    """chat 名 → session_id 列表；chat 为空返回 None（不过滤）。"""
    if not chat:
        return None
    low = chat.lower()
    return sorted(
        sid for sid, uname in book._fts_user.items()
        if low in uname.lower() or low in book.display(uname).lower()
    )


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


def fts_search(
    keyword: str,
    chat: Optional[str] = None,
    since: Optional[Any] = None,
    until: Optional[Any] = None,
    limit: int = 50,
    with_meta: bool = False,
) -> Any:
    """全史文本检索。chat 支持群名/备注/username 模糊匹配。

    with_meta=True 时返回 {"hits": [...], "total": N, "has_more": bool, "limit": limit}；
    total 是关键词+全部过滤条件（含 chat）下的全量命中数，has_more=True 表示被 limit 截断。
    """
    empty = {"hits": [], "total": 0, "has_more": False, "limit": limit}
    fts_path = fts_db_path()
    if not fts_path or not keyword:
        return dict(empty) if with_meta else []
    since, until = _as_epoch(since), _as_epoch(until)
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
        if len(hits) >= limit:
            break
    if with_meta:
        return {"hits": hits, "total": total, "has_more": total > len(hits), "limit": limit}
    return hits


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

    一次扫描取回窗口段全部会话的文本消息（升序），调用方按会话分组。
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
        total, gmin, gmax = 0, None, None
        months: dict[str, int] = {}
        for t in tables:
            try:
                c, mn, mx = con.execute(f"SELECT COUNT(*), MIN({ts_col}), MAX({ts_col}) FROM {t}").fetchone()
            except Exception:
                continue
            total += c
            gmin = mn if gmin is None or (mn and mn < gmin) else gmin
            gmax = mx if gmax is None or (mx and mx > gmax) else gmax
            for m, n in con.execute(
                f"SELECT strftime('%Y-%m', {ts_col}, 'unixepoch', 'localtime') m, COUNT(*) FROM {t} GROUP BY m"
            ):
                months[m] = months.get(m, 0) + n
        return total, gmin, gmax, months

    fmt = lambda x: datetime.datetime.fromtimestamp(x).strftime("%Y-%m-%d") if x else "-"
    msg_db = vault_msg_db_path()
    if msg_db:
        con = _connect_ro(msg_db)
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?",
            (message_table_like_pattern(),),
        )]
        total, gmin, gmax, months = _span_scan(con, tables, "create_time")
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
        ftotal, fmin, fmax, _ = _span_scan(fcon, [t + "_content" for t in _FTS_TABLES], "c6")
        fcon.close()
        out["fts"] = {
            "messages": ftotal, "span": [fmt(fmin), fmt(fmax)],
            "note": "全史文本检索层（含归档分片文本，仅文本消息）",
        }
    else:
        out["fts"] = {"error": "message_fts.db 解密副本不存在"}
    out["archives"] = {
        "shards": ["message_1.db", "message_2.db", "message_3.db", "message_4.db"],
        "status": "encrypted（密钥逐库派生、未提取；文本内容与发送者已经 fts 层可查）",
    }
    return out
