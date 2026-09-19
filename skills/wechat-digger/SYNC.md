# SYNC — wechat-digger 发布版同步手册

本目录（`skills/wechat-digger/`）是内部完整版的**脱敏发布副本**。内部版独立迭代；每次内部版出新版本，按本手册重跑一遍同步即可。本文件记录：同步基线、拷贝白名单、脱敏对照表、校验清单——四样缺一不可。

> 铁律：本包**永不携带**密钥提取与解密实现（`extract_keys` / `decrypt_all_dbs` / 自带 AES 的 legacy 件），**永不携带**真实会话锚点（群名/wxid/roomid）与本机路径。

## 1. 同步基线

| 发布版条目 | 内部版基线 | 同步日期 | 备注 |
|---|---|---|---|
| v0.0.8.0-pub | 内部 v0.0.8.0 + 09-10 未提交工作区改动（relevance/runtime_contract/wechat_schema + 测试） | 2026-09-12 | 首次随 session-digger 发布 |
| v0.0.9.2-pub | 内部 0.0.9.2（222dcd6…aca6483 九提交） | 2026-09-19 | 全史 FTS5 索引+argo 式优化轮+ai-monthly 画像+HTML+sns/biz 四 op 原生化；公开适配：ai_monthly --me 脱敏、image_dat Crypto 可选化恢复、health tools_ok 契约自适应、test_image_dat/test_core 部分同步 |
| v0.0.8.1-pub | 内部 0.0.8.1 工作区（09-16 状态，含未提交媒体层） | 2026-09-16 | extras 命令族 + image_dat/extra_layers + vault 日期窗；fts_engine 缺口回填与 source_registry._maybe_fill_fts_gap **未同步**（绑定本机 vault 形态，见对照表修订） |

## 2. 拷贝白名单（只拷这些，其余一律不带）

```
scripts/*.py                 ← 全部顶层脚本（wd/analyze/fts_engine/normalize/… 21 个，含 extra_layers/image_dat）
scripts/setup_deps.sh
scripts/acquire/vault_cli.py     ← 只读查询
scripts/acquire/export_chat.py   ← 只读导出
references/architecture.md / signal-rules.md / output-formats.md / error-handling.md / essence-map.md
tests/test_*.py
combo_map.json
```

**永不拷贝**：`scripts/acquire/extract_keys.py`、`decrypt_all_dbs.py`、`list_contacts.py`、`search_sns.py`、`references/key-extraction-assessment.md`、`references/evaluation-*.md`、`references/code-review-*.md`（内部评审，含实测细节）、`__pycache__`/`.venv`/`.pytest_cache`。

同步后手工维护四处（内部版改动波及时跟着改）：

1. `SKILL.md` —— 发布版边界段、触发词、路由表、changelog 顶部加 `-pub` 条目
2. `scripts/acquire/README.md` —— 只读件清单
3. `combo_map.json` —— 删 `keys` / `decrypt` / `refresh` 三行，`doctor`/`acquire-info` 组合里剔除同名引用
4. `scripts/health.py` —— `tools_ok` 只查 `vault_cli`/`export_chat`；`acquire_deps` 只查 zstandard（去 Crypto）；两项检查 detail 注明发布版边界
5. `tests/test_core.py` —— `acquire_bundled` 断言反转（decrypt/keys 应 `assertIsNone`/`assertFalse`）
6. `tests/test_runtime_contract.py` —— `extract_keys` 测试反转成「不得存在」契约（含 decrypt_all_dbs/list_contacts/search_sns）
7. `tests/test_schema_contract.py` —— `AFFECTED` 列表去掉已剔除的 acquire 文件
8. `tests/test_search_fixes.py` —— 全部 live 断言加 `and _LIVE_ANCHORS` 门（文件头 `_LIVE_ANCHORS = os.environ.get("WECHAT_DIGGER_LIVE_ANCHORS") == "1"`），锚点名一律虚构

## 3. 脱敏对照表

| 内部版内容 | 处理 | 发布版形态 |
|---|---|---|
| `extract_keys.py`（抓 key/内存扫描） | 剔除 | 不分发；`keys` 子命令报 stack missing |
| `decrypt_all_dbs.py`（SQLCipher 解密） | 剔除 | 不分发；`decrypt` 子命令报 stack missing |
| `list_contacts.py` / `search_sns.py`（自带 AES 解密） | 剔除 | 不分发 |
| `vault_cli.py` / `export_chat.py` | 保留 | 已核实纯只读（无 AES/Crypto import） |
| `requirements.txt` pycryptodome | 删 | 仅留 zstandard；`setup_deps.sh` 验证行同步改 |
| SKILL.md 触发词「解密微信、抓 key、刷新 vault」 | 删 | 换「数据接入」说明段 |
| SKILL.md「首次全量 vault：keys→capture→decrypt」段 | 删 | 数据接入三来源（自备已解密库/wx-cli/导出文件） |
| `health.py` HARDCODE_PATTERNS 作者名正则 | 删行 | 保留通用 `/Users/<name>/` 路径规则即可 |
| 真实会话锚点：2 个单聊、3 个群聊（原文不入库，grep 真源盘点） | 替换 | 「示例单聊A」「示例客服单聊」「示例群B」「示例项目群」「记账分享2(.0)」 |
| 真实线报群名（名称含广告特征词） | 替换 | 「示例线报群」 |
| 锚点注释里的 `wxid_*` / `*数字@chatroom` | 删/换虚构 id | 改「fts.name2id 域内锚点」描述 |
| changelog/评审文档中的锚点案例细节 | 改写/不拷 | 见第 2 节永不拷贝清单 |
| 「线报」「羊毛」「广告」类别词 | **保留** | 不指向具体会话，非个人数据 |

## 4. 校验清单（每次同步后必跑，全绿才算完）

```bash
cd skills/wechat-digger
# ① 敏感词零命中：先在内部版真源 grep 盘点真实锚点词表（真实群名/单聊名，词表用临时文件，不落本仓库），
#    再在发布版对同一词表复查，必须零命中：
grep -rn -f /tmp/anchor-words.txt . && echo FAIL-1
grep -rInE "wxid_[a-z0-9]{6,}|[0-9]{9,}@chatroom" scripts/ tests/ references/ && echo FAIL-2
grep -rn "$(whoami)\|/Users/" scripts/ tests/ references/ *.md *.json | grep -v -E "re\.compile|Users/<|禁止|HARDCODE" && echo FAIL-3
grep -rn "Crypto\|frida" scripts/acquire/ && echo FAIL-4
# ② 解密栈不在包内
ls scripts/acquire/ | grep -E "extract_keys|decrypt_all|list_contacts|search_sns" && echo FAIL-5
# ③ 测试：无库环境 live 锚点测试自动 skip，其余全绿（Crypto 用例需 .venv 或自动跳过）（基线 196 passed / 21 skipped @ pytest，允许随版本增长；无 Crypto 环境图片解密用例自动跳过）
python3 -m unittest discover -s tests 2>&1 | tail -1
# ④ 冒烟：doctor 在裸环境可跑、如实标注采集栈缺失
python3 scripts/wd.py doctor
```

## 5. 下次同步怎么做

1. 内部版发新版 → 按第 2 节白名单重新拷贝
2. `SKILL.md` changelog 迁移新版条目（跑第 3 节对照表过一遍敏感词）
3. 跑第 4 节校验清单，四个 FAIL 全不出现
4. 本文件第 1 节加一行新基线，`git commit` 单独一个提交（标题带内部版号）
