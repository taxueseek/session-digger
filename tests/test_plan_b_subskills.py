"""Regression tests for Plan B sub-skill optimization & wechat-digger upgrades."""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
SCRIPTS_DIR = REPO_ROOT / "scripts"
WD_DIR = SKILLS_DIR / "wechat-digger"
WD_SCRIPTS = WD_DIR / "scripts"

for _p in (SCRIPTS_DIR, SKILLS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load_mod(name: str, path: Path, *, prepend_sys_path: list[Path] | None = None):
    added = []
    for p in reversed(prepend_sys_path or []):
        sp = str(p)
        if sp not in sys.path:
            sys.path.insert(0, sp)
            added.append(sp)
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestCoreSubskillsPlanB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.common_paths = _load_mod("sd_common_paths_test", SKILLS_DIR / "common_paths.py")
        cls.intent_mod = _load_mod(
            "sd_intent_classify_test",
            SKILLS_DIR / "deep-analysis" / "scripts" / "intent-classify.py",
        )
        cls.erc_mod = _load_mod(
            "sd_error_root_cause_test",
            SKILLS_DIR / "deep-analysis" / "scripts" / "error-root-cause.py",
        )

    def test_digger_root_prefers_repo_checkout(self):
        self.assertEqual(self.common_paths.digger_root().resolve(), REPO_ROOT.resolve())

    def test_resolve_session_ref_supports_virtual_uri_and_index(self):
        self.assertEqual(
            self.common_paths.resolve_session_ref("dimcode://sess_abc123"),
            "dimcode://sess_abc123",
        )
        self.assertIsNone(self.common_paths.resolve_session_jsonl("dimcode://sess_abc123"))

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.db"
            con = sqlite3.connect(str(db))
            con.execute(
                "CREATE TABLE sessions (id TEXT PRIMARY KEY, agent TEXT, jsonl_path TEXT, first_prompt TEXT)"
            )
            con.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                ("zcode_db:sess_999", "zcode_db", "zcode://sess_999", "fix sql lock"),
            )
            con.commit()
            con.close()

            orig_fn = self.common_paths.index_db_path
            self.common_paths.index_db_path = lambda: db
            try:
                self.assertEqual(self.common_paths.resolve_session_ref("sess_999"), "zcode://sess_999")
            finally:
                self.common_paths.index_db_path = orig_fn

    def test_intent_word_boundaries_and_debugging_reclassification(self):
        res_pad = self.intent_mod.classify_text("check padding in csv")
        self.assertNotEqual(res_pad["intent"], "DEVELOPMENT")
        self.assertEqual(res_pad["intent"], "ANALYSIS")

        res_prefix = self.intent_mod.classify_text("update prefix config")
        self.assertNotEqual(res_prefix["intent"], "DEBUGGING")
        self.assertEqual(res_prefix["intent"], "OPTIMIZATION")

        res_fix_zh = self.intent_mod.classify_text("请修复 sqlite database is locked 报错")
        self.assertEqual(res_fix_zh["intent"], "DEBUGGING")

        res_fix_en = self.intent_mod.classify_text("fix import bug in module")
        self.assertEqual(res_fix_en["intent"], "DEBUGGING")

        res_action = self.intent_mod.classify_text("请分析一下这个报错问题，最终目的是提升稳定性、优化性能")
        self.assertEqual(res_action["intent"], "OPTIMIZATION")

    def test_intent_multi_message_fallback(self):
        res = self.intent_mod.classify_messages(["hi", "/analyze", "帮我对比这两个方案的优缺点"])
        self.assertEqual(res["intent"], "COMPARISON")
        self.assertGreater(res["confidence"], 0.0)

    def test_error_root_cause_new_categories(self):
        cases = [
            ("sqlite3.OperationalError: database is locked", "SQLITE_LOCK_OR_CORRUPT"),
            ("fatal: Unable to create '.git/index.lock': File exists.", "GIT_ERROR"),
            ("json.decoder.JSONDecodeError: Expecting value: line 1 column 1", "PARSE_OR_FORMAT_ERROR"),
            ("Blocked by pre-tool privacy hook: absolute path not allowed", "POLICY_OR_TOOL_BLOCKED"),
        ]
        for msg, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(self.erc_mod.classify_error(msg), expected)

    def test_diagnose_session_single_pass_claude_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "sess.jsonl"
            records = [
                {
                    "type": "user",
                    "uuid": "u1",
                    "timestamp": "2026-11-27T10:00:00Z",
                    "message": {"role": "user", "content": "请修复 database is locked 问题"},
                },
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "timestamp": "2026-11-27T10:00:01Z",
                    "message": {
                        "role": "assistant",
                        "model": "claude-sonnet-4-5",
                        "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "sqlite3 db"}}],
                    },
                },
                {
                    "type": "user",
                    "uuid": "u2",
                    "timestamp": "2026-11-27T10:00:02Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "sqlite3.OperationalError: database is locked"}],
                    },
                },
            ]
            p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
            diag = self.intent_mod.diagnose_session(str(p))
            self.assertEqual(diag["intent"]["intent"], "DEBUGGING")
            self.assertEqual(diag["error_summary"]["total_errors"], 1)
            self.assertEqual(diag["error_summary"]["by_type"].get("SQLITE_LOCK_OR_CORRUPT"), 1)


class TestWechatDiggerPlanB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if str(WD_SCRIPTS) not in sys.path:
            sys.path.append(str(WD_SCRIPTS))
        cls.wd_paths = _load_mod("paths", WD_SCRIPTS / "paths.py")
        cls.wd_schema = _load_mod("wechat_schema", WD_SCRIPTS / "wechat_schema.py")
        cls.wd_fts = _load_mod("fts_engine", WD_SCRIPTS / "fts_engine.py")
        cls.wd_bridge = _load_mod("acquire_bridge", WD_SCRIPTS / "acquire_bridge.py")
        cls.wd_analyze = _load_mod("analyze", WD_SCRIPTS / "analyze.py")

    def test_ssot_dedup_and_dead_code_removed(self):
        self.assertFalse((WD_SCRIPTS / "wd_index.py").exists())
        self.assertFalse((WD_SCRIPTS / "vault_cli.py").exists())
        self.assertFalse((WD_SCRIPTS / "export_chat.py").exists())
        canonical_vcli = WD_SCRIPTS / "acquire" / "vault_cli.py"
        self.assertTrue(canonical_vcli.is_file())
        self.assertTrue((WD_SCRIPTS / "acquire" / "export_chat.py").is_file())

        text = canonical_vcli.read_text(encoding="utf-8")
        for sub in ("sns-feed", "sns-search", "sns-notifications", "biz-articles"):
            self.assertIn(f'"{sub}"', text)
            self.assertIn(f"vault-{sub}", self.wd_bridge.ACQUIRE_OPS)

    def test_dynamic_vault_rich_start_ts_and_namebook_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            dec_cur = Path(tmp) / "decrypted" / "current"
            (dec_cur / "message").mkdir(parents=True)
            (dec_cur / "contact").mkdir(parents=True)

            msg_db = dec_cur / "message" / "message_0.db"
            con = sqlite3.connect(str(msg_db))
            con.execute("CREATE TABLE Msg_abc123 (local_id INTEGER, sort_seq INTEGER, create_time INTEGER)")
            con.execute("INSERT INTO Msg_abc123 VALUES (1, 100, 1740000000)")
            con.execute("INSERT INTO Msg_abc123 VALUES (2, 200, 1745000000)")
            con.commit()
            con.close()

            fts_db = dec_cur / "message" / "message_fts.db"
            fcon = sqlite3.connect(str(fts_db))
            fcon.execute("CREATE TABLE name2id (username TEXT)")
            fcon.execute("INSERT INTO name2id VALUES ('wxid_test1')")
            fcon.commit()
            fcon.close()

            cdb = dec_cur / "contact" / "contact.db"
            ccon = sqlite3.connect(str(cdb))
            ccon.execute("CREATE TABLE contact (username TEXT, remark TEXT, nick_name TEXT)")
            ccon.execute("INSERT INTO contact VALUES ('wxid_test1', '测试备注', '测试昵称')")
            ccon.commit()
            ccon.close()

            old_vault = os.environ.get("WECHAT_PRIVATE_VAULT")
            old_fts = os.environ.get("WECHAT_FTS_DB")
            old_vroot = os.environ.get("WECHAT_VAULT_ROOT")
            os.environ["WECHAT_PRIVATE_VAULT"] = tmp
            os.environ.pop("WECHAT_FTS_DB", None)
            os.environ.pop("WECHAT_VAULT_ROOT", None)
            try:
                self.assertEqual(self.wd_fts.vault_msg_db_path(), msg_db)
                self.assertEqual(self.wd_fts.fts_db_path(), fts_db)
                self.assertEqual(self.wd_fts.vault_rich_start_ts(), 1740000000)

                b1 = self.wd_fts._get_namebook(dec_cur)
                b2 = self.wd_fts._get_namebook(dec_cur)
                self.assertIs(b1, b2)
                self.assertEqual(b1.display("wxid_test1"), "测试备注")
            finally:
                if old_vault is None:
                    os.environ.pop("WECHAT_PRIVATE_VAULT", None)
                else:
                    os.environ["WECHAT_PRIVATE_VAULT"] = old_vault
                if old_fts is not None:
                    os.environ["WECHAT_FTS_DB"] = old_fts
                if old_vroot is not None:
                    os.environ["WECHAT_VAULT_ROOT"] = old_vroot

    def test_acquire_bridge_inprocess_matches_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            dec = Path(tmp) / "dec"
            (dec / "message").mkdir(parents=True)
            con = sqlite3.connect(str(dec / "message" / "message_0.db"))
            con.execute("CREATE TABLE Name2Id (user_name TEXT)")
            con.commit()
            con.close()

            argv = ["--decrypted-dir", str(dec), "status", "--format", "json"]
            res_in = self.wd_bridge.run_tool("vault_cli", argv)
            old_sub = os.environ.get("WECHAT_ACQUIRE_SUBPROCESS")
            os.environ["WECHAT_ACQUIRE_SUBPROCESS"] = "1"
            try:
                res_sub = self.wd_bridge.run_tool("vault_cli", argv)
            finally:
                if old_sub is None:
                    os.environ.pop("WECHAT_ACQUIRE_SUBPROCESS", None)
                else:
                    os.environ["WECHAT_ACQUIRE_SUBPROCESS"] = old_sub

            self.assertEqual(res_in.returncode, 0)
            self.assertEqual(res_in.returncode, res_sub.returncode)
            self.assertEqual(json.loads(res_in.stdout), json.loads(res_sub.stdout))

    def test_analyze_simhash_exact_parity(self):
        def ref_simhash(text: str, bits: int = 64) -> int:
            tokens = self.wd_analyze._tokenize(text)
            if not tokens:
                return 0
            vec = [0] * bits
            for tok in tokens:
                h = hash(tok) & ((1 << bits) - 1)
                for i in range(bits):
                    vec[i] += 1 if (h & (1 << i)) else -1
            out = 0
            for i, v in enumerate(vec):
                if v > 0:
                    out |= 1 << i
            return out

        for sample in (
            "",
            "测试中文分词与英文 word_123 混合句子",
            "重复 重复 重复 hello hello world padding csv prefix",
        ):
            with self.subTest(sample=sample):
                self.assertEqual(self.wd_analyze.simhash(sample), ref_simhash(sample))


if __name__ == "__main__":
    unittest.main()
