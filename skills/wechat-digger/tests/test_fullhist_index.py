"""全史 FTS5 索引层：CJK 边界切分、total 口径、增量同步、新鲜度回退。

锚点来自 2026-09-19 修复的三个实测 bug：
1. 「2红包」数字+CJK 粘连 → phrase 断裂（漏 3.5% 命中）
2. 「红包🧧」emoji 并入 token（unicode61 的 So 类符号不是分隔符）
3. 裸 MATCH 搜三列把群名命中计入 total（+15，LIKE 路径只搜正文）
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from cjk import split_cjk, uncjk, build_match_query  # noqa: E402


class TestCjkBoundary(unittest.TestCase):
    """split_cjk 的 token 边界契约：CJK 相邻性 == 原文连续子串。"""

    def test_ascii_cjk_boundary_is_split(self):
        # 数字粘连：'2红包' 必须 → '2 红 包'，否则 unicode61 合成 token「2红」
        self.assertEqual(split_cjk("2红包"), "2 红 包")

    def test_emoji_boundary_is_split(self):
        # emoji 在 SQLite 3.53 unicode61 下并入 token（实测），
        # 「红包🧧可」必须切成 红|包|🧧|可
        out = split_cjk("红包🧧可叠加")
        self.assertIn("红 包", out)
        self.assertNotIn("包🧧", out)

    def test_roundtrip_ascii_cjk(self):
        for s in ["能用2红包", "无门槛红包🧧可叠加", "PostgreSQL迁移", "纯中文"]:
            self.assertEqual(uncjk(split_cjk(s)), s)

    def test_ascii_run_spaces_preserved(self):
        self.assertEqual(split_cjk("use git"), "use git")

    def test_match_query_drops_operators(self):
        # FTS5 操作符/引号/列注入必须被丢弃（注入洞）
        for evil in ['"红" OR "包"', "text_cjk: (x)", "NEAR(a b)", 'a; DROP']:
            mq = build_match_query(evil)
            if mq is not None:
                # 安全不变量：每个 term 都以引号开头（字面词/短语），裸操作符与列注入全被丢弃
                for part in mq.split():
                    self.assertTrue(part.startswith('"'), f"{evil!r} → {mq!r} 有裸 term {part!r}")

    def test_match_query_cjk_phrase(self):
        self.assertEqual(build_match_query("简历"), '"简 历"')

    def test_match_query_ascii_prefix(self):
        self.assertEqual(build_match_query("Postgre"), '"Postgre"*')


class _FtsFixture:
    """构建最小 message_fts 形状的解密副本（4 分片表 + name2id）。"""

    def __init__(self, root: Path):
        msg_dir = root / "decrypted" / "current" / "message"
        msg_dir.mkdir(parents=True)
        self.db = msg_dir / "message_fts.db"
        con = sqlite3.connect(self.db)
        con.executescript("""
            CREATE TABLE name2id (rowid INTEGER PRIMARY KEY, username TEXT);
            CREATE TABLE message_fts_v4_0_content (rowid INTEGER PRIMARY KEY, c0 TEXT, c4 INT, c5 INT, c6 INT);
            CREATE TABLE message_fts_v4_1_content (rowid INTEGER PRIMARY KEY, c0 TEXT, c4 INT, c5 INT, c6 INT);
            CREATE TABLE message_fts_v4_2_content (rowid INTEGER PRIMARY KEY, c0 TEXT, c4 INT, c5 INT, c6 INT);
            CREATE TABLE message_fts_v4_3_content (rowid INTEGER PRIMARY KEY, c0 TEXT, c4 INT, c5 INT, c6 INT);
        """)
        con.executemany("INSERT INTO name2id VALUES (?,?)", [(1, "10086@chatroom"), (2, "wxid_hr")])
        rows = [
            # 群1：正文含数字/emoji 粘连的「红包」——三个边界 bug 的回归样本
            (1, "能用2红包", 1, 1, 1700000000),
            (2, "红包🧧可叠加", 1, 1, 1700000100),
            (3, "群名不该计入", 1, 1, 1700000200),
            (4, "另一会话红包", 2, 2, 1700000300),
        ]
        con.executemany("INSERT INTO message_fts_v4_0_content VALUES (?,?,?,?,?)", rows)
        con.execute("INSERT INTO message_fts_v4_1_content VALUES (1, '占位', 1, 1, 1700000000)")
        con.commit()
        con.close()


class TestFullhistIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        _FtsFixture(root)
        self._env = {k: os.environ.get(k) for k in ("WECHAT_FTS_DB", "WECHAT_VAULT_ROOT", "WECHAT_DIGGER_DATA", "WECHAT_PRIVATE_VAULT")}
        # 两个取路径函数都在调用时读 env——只需隔离 env，不需要 reload 模块
        os.environ["WECHAT_VAULT_ROOT"] = str(root)
        os.environ["WECHAT_DIGGER_DATA"] = str(root / "data")
        # 双保险：WECHAT_FTS_DB 直指 fixture，杜绝回落到真库（真库路径是最后的 glob 兜底）
        os.environ["WECHAT_FTS_DB"] = str(root / "decrypted" / "current" / "message" / "message_fts.db")
        import importlib
        self.fts_engine = importlib.import_module("fts_engine")
        self.index_builder = importlib.import_module("index_builder")
        # 每个用例独立 fixture + 全量重建，用例间零共享状态
        conn = self.index_builder.connect(self.index_builder.default_index_path())
        try:
            r = self.index_builder.rebuild_from_fts(conn)
            assert r.get("ok"), r
        finally:
            conn.close()

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_total_counts_body_only(self):
        """total 只算正文命中：群名列命中不计（锚：LIKE 口径 36817 vs 裸 MATCH 36832）。"""
        r = self.fts_engine.fts_search("红包", with_meta=True)
        # 正文含「红包」3 条（群名列含红包的行不计），数字/emoji 粘连行必须召回
        self.assertEqual(r["total"], 3)
        self.assertTrue(all(h["source"] == "fts-index" for h in r["hits"]))

    def test_incremental_sync(self):
        """新消息（rowid 增长）→ 下次查询自动增量同步，total 增长且不重建全库。"""
        r1 = self.fts_engine.fts_search("红包", with_meta=True)
        self.assertEqual(r1["total"], 3)
        con = sqlite3.connect(self.fts_engine.fts_db_path())
        con.execute("INSERT INTO message_fts_v4_0_content VALUES (1001, '又来一个红包', 1, 1, 1700000400)")
        con.commit()
        con.close()
        r2 = self.fts_engine.fts_search("红包", with_meta=True)
        self.assertEqual(r2["total"], 4)
        self.assertEqual(r2["hits"][0]["text"], "又来一个红包")

    def test_fallback_on_broken_index(self):
        """索引损坏 → 静默回退 LIKE 慢路径，结果照样正确（正确性优先于速度）。"""
        idx = Path(os.environ["WECHAT_DIGGER_DATA"]) / "index" / "messages.db"
        idx.parent.mkdir(parents=True, exist_ok=True)
        idx.write_text("not a database")
        r = self.fts_engine.fts_search("红包", with_meta=True)
        self.assertEqual(r["total"], 3)  # LIKE 路径同口径
        self.assertTrue(all(h["source"] == "fts" for h in r["hits"]))

    def test_chat_filter_same_semantics(self):
        """chat 过滤两路径同口径（锚：群内 289 的类型错位修复）。"""
        r = self.fts_engine.fts_search("红包", chat="10086", with_meta=True)
        # fixture: 3 行正文含红包，其中 2 行属 10086@chatroom、1 行属 wxid_hr（chat 过滤排除）
        self.assertEqual(r["total"], 2)
        self.assertTrue(all(h["chat_id"] == "10086@chatroom" for h in r["hits"]))
        # 对照：不过滤时 3 行全见（chat 语义与 LIKE 路径同源）
        r_all = self.fts_engine.fts_search("红包", with_meta=True)
        self.assertEqual(r_all["total"], 3)


class TestRowidTimeOrdering(unittest.TestCase):
    """rowid 时间序不变量（v0.0.9.3）：快路径的正确性地基。

    倒排按 rowid 有序，只有排序键 = rowid 时 LIMIT 才能早停。因此 rebuild
    按 ts 升序灌数、增量同步维持同序；一旦被乱序写入破坏，必须自动降级。
    这组用例锁定「对齐正确」与「破坏后降级且仍正确」两侧。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        _FtsFixture(root)
        self._env = {k: os.environ.get(k) for k in ("WECHAT_FTS_DB", "WECHAT_VAULT_ROOT", "WECHAT_DIGGER_DATA", "WECHAT_PRIVATE_VAULT")}
        os.environ["WECHAT_VAULT_ROOT"] = str(root)
        os.environ["WECHAT_DIGGER_DATA"] = str(root / "data")
        os.environ["WECHAT_FTS_DB"] = str(root / "decrypted" / "current" / "message" / "message_fts.db")
        import importlib
        self.fts_engine = importlib.import_module("fts_engine")
        self.index_builder = importlib.import_module("index_builder")
        conn = self.index_builder.connect(self.index_builder.default_index_path())
        try:
            r = self.index_builder.rebuild_from_fts(conn)
            assert r.get("ok"), r
        finally:
            self.conn = conn

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _non_monotonic(self) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM (SELECT rowid, ts, LAG(ts) OVER (ORDER BY rowid) p "
            "FROM messages) WHERE p IS NOT NULL AND ts < p"
        ).fetchone()
        return cur[0]

    def test_rebuild_orders_rowid_by_time(self):
        """rebuild 后 rowid 与 ts 同序，且置了快路径标记。

        fixture 的源数据 ts 递增，故重建后 rowid 必须严格等于 (1,2,3,...)。
        """
        self.assertEqual(self.index_builder._meta_get(self.conn, "rowid_time_ordered"), "1")
        self.assertEqual(self._non_monotonic(), 0)
        rows = self.conn.execute("SELECT rowid, ts FROM messages ORDER BY rowid").fetchall()
        tss = [r["ts"] for r in rows]
        self.assertEqual(tss, sorted(tss), "rowid 顺序必须等于 ts 升序")

    def test_rebuild_reorders_shuffled_source(self):
        """源库乱序（rowid 序 ≠ ts 序）→ 重建后必须按 ts 重排，不能照搬。

        这是同序不变量的关键：微信 4 个分片各自的 rowid 是分片内插入序，
        拼接后全局并非时间序。照搬会让 ORDER BY rowid 返回错误的时间序。
        """
        path = self.fts_engine.fts_db_path()
        con = sqlite3.connect(path)
        # 清掉 v4_1 的「占位」行，只留本用例要断言的乱序样本
        con.execute("DELETE FROM message_fts_v4_0_content")
        con.execute("DELETE FROM message_fts_v4_1_content")
        # 故意让 rowid 序与 ts 序相反
        con.executemany(
            "INSERT INTO message_fts_v4_0_content VALUES (?,?,?,?,?)",
            [(1, "红包早", 1, 1, 1700000000), (2, "红包晚", 1, 1, 1700000900)],
        )
        con.commit()
        con.close()
        conn = self.index_builder.connect(self.index_builder.default_index_path())
        try:
            r = self.index_builder.rebuild_from_fts(conn)
            assert r.get("ok"), r
            rows = [tuple(x) for x in conn.execute(
                "SELECT rowid, ts, text FROM messages ORDER BY rowid")]
            self.assertEqual([x[2] for x in rows], ["红包早", "红包晚"])
            self.assertEqual([x[1] for x in rows], sorted(x[1] for x in rows))
        finally:
            conn.close()

    def test_incremental_keeps_ordering(self):
        """追加更晚的消息 → 同序保持，标记仍为 1。"""
        con = sqlite3.connect(self.fts_engine.fts_db_path())
        con.execute("INSERT INTO message_fts_v4_0_content VALUES (1001, '又来一个红包', 1, 1, 1700009999)")
        con.commit()
        con.close()
        r = self.fts_engine.fts_search("红包", with_meta=True)
        self.assertEqual(r["total"], 4)
        self.assertEqual(self.index_builder._meta_get(self.conn, "rowid_time_ordered"), "1")
        self.assertEqual(self._non_monotonic(), 0)

    def test_out_of_order_write_degrades_and_stays_correct(self):
        """乱序写入（ts 更早）→ 自动降级为 ORDER BY ts，结果仍严格时间倒序。

        降级是安全底线：宁慢勿错。若这里失败，说明快路径在乱序数据上
        会静默返回错误的时间序。
        """
        con = sqlite3.connect(self.fts_engine.fts_db_path())
        con.execute("INSERT INTO message_fts_v4_0_content VALUES (1002, '陈年红包', 1, 1, 1600000000)")
        con.commit()
        con.close()
        r = self.fts_engine.fts_search("红包", with_meta=True)
        self.assertEqual(r["total"], 4)
        self.assertEqual(self.index_builder._meta_get(self.conn, "rowid_time_ordered"), "0",
                         "同序被破坏后必须自动降级")
        tss = [h["ts"] for h in r["hits"]]
        self.assertEqual(tss, sorted(tss, reverse=True), "降级后仍须严格时间倒序")
        self.assertEqual(r["hits"][0]["ts"], 1700000300, "首条应是时间最新的一条")

    def test_time_filter_matches_join_semantics(self):
        """时间过滤走 rowid 界改写后，总数必须与 JOIN ts 口径一致。"""
        all_out = self.fts_engine.fts_search("红包", with_meta=True)
        truth = self.conn.execute(
            "SELECT COUNT(*) FROM messages_fts f JOIN messages m ON m.rowid = f.rowid "
            "WHERE messages_fts MATCH ? AND m.ts >= ?", ("text_cjk: \"红 包\"", 1700000200)
        ).fetchone()[0]
        out = self.fts_engine.fts_search("红包", since=1700000200, with_meta=True)
        self.assertEqual(out["total"], truth)
        self.assertLessEqual(out["total"], all_out["total"])


if __name__ == "__main__":
    unittest.main()
