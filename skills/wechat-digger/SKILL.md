---
name: wechat-digger
description: |
  微信本地数据识别与分析（session-digger 子技能）。索引、搜索、分析、摘要、画像、关系网络、决策复盘、朋友圈、收藏夹——对已解密的微信数据库做全史检索与跨会话成文。
  数据接入三来源：已解密 vault（离线库）/ wx-cli（在线查询）/ 导出文件，统一消息 schema。
  触发：微信、群聊、聊天记录、群聊精华、群聊摘要、日报、周报、导出聊天、
  联系人、会话列表、搜索聊天、谁说了算、关系网络、
  情绪变化、群友画像、客户跟进、朋友圈、收藏夹、数据实验室、发言排行、词云、
  关系分析、双人互动、wechat-digger、帮我看看这个群、这个群在聊什么、从上次继续、毒舌版摘要、
  语音导出、转账红包、好友申请、图片还原、解密图片、extras。
  DO NOT use when:
  - 写公众号/排版 HTML → wechat-humon-blogger / dbs-wechat-html
  - 微信读书（书/划线）→ taxue-weread
  - 仅操作剪映 → jianying-editor
version: 0.0.9.3-pub
---

# wechat-digger

> 你聊了什么、谁在场、结论是什么——全在这。  
> **本发布版只做识别与分析，不包含密钥提取与数据库解密实现**——把已解密的微信数据库交给它，剩下的事它全包。已解密库里的媒体层（语音导出/转账红包/好友申请/.dat 图片离线解码）可直接使用；抓取微信密钥、解密 SQLCipher 数据库请用你自选的工具完成后接入。

| 用户意图 | 路由 |
|---------|------|
| 环境自检 / 数据源盘点 | `/doctor` · `acquire-info` · `/detect` |
| 数据接入（已解密库在哪、怎么给） | 见下文「数据接入」+ `references/architecture.md` |
| vault 直通（含 moments/favorites） | `/vault …` · `/moments` · `/favorites` |
| 已解密附加层（语音/转账红包/好友申请） | `/extras status|voice|voice-export|payments|requests` |
| V2 图片离线还原（需 pycryptodome，可选） | `/extras images-discover` · `/extras images-decrypt` |
| wx-cli 富命令（朋友圈/公众号/附件） | `/wx …` · `/sns-feed` · `/biz-articles` · `/attachments` |
| 最近会话 / 联系人 / 历史 | `/sessions` · `/contacts` · `/history` |
| FTS 索引 / 搜索 | `/index` · `/search --use-index` |
| 分析 / 摘要 | `/analyze` · `/digest` |
| 商机/承诺跟进/待回复/复联 | `/followups`（配 `references/signal-rules.md` 解读） |
| 噪音群排查/会话质量分层 | `/chats` |
| 同类群汇总/主题成文/我的发言梳理 | `/roundup` |
| 实验室/关系报告（三系精华） | `/report --flavor lab\|dyad` |
| 导出消息 JSON | `/export-msg` |

做完后读 `combo_map.json`。不输出路由过程。

## 数据接入（发布版边界，先读这段）

本包**不含**密钥提取（key capture）与 SQLCipher 解密实现。`wd.py` 的 `keys` / `decrypt` / `refresh` 三个子命令在发布版里会因采集工具缺失而不可用——这是有意设计，不是缺陷。数据来源由你自备，三选一或多路并行：

1. **已解密 vault（推荐，离线全量）**：任何工具解密出的微信明文库（`message_*.db`、`contact.db`、`session.db`、`message_fts.db` 等），通过 `WECHAT_PRIVATE_VAULT` 环境变量或 `~/.config/wechat-local-vault.json` 指向即可。vault 引擎对其只读。
2. **wx-cli（在线实时）**：本机装有 [wx-cli](https://github.com/) 类工具时 `/wx` 系列可用（朋友圈/公众号/附件仅此路）。
3. **导出文件（零门槛）**：`--source export` + 导出 JSON，表驱动接入。

路径解析约定：

```bash
WD_ROOT="${WECHAT_DIGGER_ROOT:-}"
# 探测: $HOME/.agents|/.claude|/.grok/skills/wechat-digger
# 数据: WECHAT_DIGGER_DATA（索引/摘要）
# 明文 vault: WECHAT_PRIVATE_VAULT 或 ~/Library/Application Support/wechat-local-vault
```

禁止硬编码 `/Users/<某人>/`。

## Architecture

| Layer | Module | What |
|-------|--------|------|
| −1 ACQUIRE WRITE | `scripts/acquire/*`（发布版仅只读件） | ~~抓 key / 解密~~ 不随包分发；vault 只读查询随包 |
| 0 ACQUIRE READ | `source_registry.py` | vault / wxcli / export / fixture 表驱动 |
| 1 NORMALIZE | `normalize.py` | CanonicalMessage |
| 2 INDEX | `index_builder.py` | FTS5 |
| 3 ANALYZE + DELIVER | `analyze.py` + `render.py` | 洞察与摘要 |

**一行扩展一类问题**

| 表 | 用途 |
|----|------|
| `SOURCE_REGISTRY` | 新数据源 |
| `ACQUIRE_TOOLS` / `ACQUIRE_OPS` | 采集脚本与操作（vault 只读 op 全保留） |
| `WX_SURFACE` | wx-cli 子命令面 |
| `CAPABILITY_MATRIX` | 按 op 选 vault/wx 引擎 |
| `TYPE_ALIASES` | 新消息类型 |
| `ANALYSIS_MODES` | 分析组合（lab/dyad/full…） |

三系精华见 `references/essence-map.md`。

## 统一命令

```bash
python3 "$WD_ROOT/scripts/wd.py" doctor
python3 "$WD_ROOT/scripts/wd.py" acquire-info

# vault 直通（只读已解密库，含 moments/favorites）
python3 "$WD_ROOT/scripts/wd.py" vault status --format text

# wx-cli 双引擎（需本机 wx + 通常 sudo wx init；未就绪时 doctor 标明，不拖垮 vault）
python3 "$WD_ROOT/scripts/wd.py" wx info
python3 "$WD_ROOT/scripts/wd.py" sns-feed --limit 10
python3 "$WD_ROOT/scripts/wd.py" biz-articles --limit 10
python3 "$WD_ROOT/scripts/wd.py" --source wxcli history --chat "群名" --since 2026-07-01

# 查询 + 分析（默认 vault 优先）
python3 "$WD_ROOT/scripts/wd.py" sessions --limit 20
python3 "$WD_ROOT/scripts/wd.py" history --chat "群名" --since 2026-07-01
python3 "$WD_ROOT/scripts/wd.py" digest --chat "群名" --version normal --print-body

# 全史文本检索（fts 引擎，微信自带 message_fts 解密副本；search 默认走 fts）
# 输出含 total/has_more：has_more=true 说明被 --limit 截断，盘点类结论必须看全量或用 --group-by
python3 "$WD_ROOT/scripts/wd.py" search "关键词" --limit 20
python3 "$WD_ROOT/scripts/wd.py" search "关键词" --since 2023-01-01 --until 2025-12-31
python3 "$WD_ROOT/scripts/wd.py" search "关键词" --since 2026-01-01 --group-by chat --limit 30  # 会话聚合：count×类型×时间跨度，盘点主视图

# 数据覆盖审计（跨度/总量/空洞，定期体检）
python3 "$WD_ROOT/scripts/wd.py" coverage

# 行动信号（商单/承诺/待回复；--self 识别本人）与长周期趋势
python3 "$WD_ROOT/scripts/wd.py" analyze --chat "群名" --mode signals --self "我的昵称"
python3 "$WD_ROOT/scripts/wd.py" analyze --chat "群名" --mode trends --all-time --limit 100000

# 全周期分析（fts 全史文本层；vault 通常只有近期富文本）
# vault 对空窗段返回空时 history/analyze 会自动穿透到 fts；显式 --source fts 强制全史
python3 "$WD_ROOT/scripts/wd.py" analyze --chat "群名" --mode trends --all-time --source fts --limit 100000
python3 "$WD_ROOT/scripts/wd.py" history --chat "群名" --since 2023-01-01 --until 2025-12-31 --source fts

# 商机/承诺跟进（fts 文本层一次扫描窗口段全部会话；状态机+反馈学习+今日行动）
# 自动排除噪音群（线报/广告识别）+ 用户已确认 ignore 的群；显式 --chat 点名时不排除
python3 "$WD_ROOT/scripts/wd.py" followups --self "我的昵称" --window 30
python3 "$WD_ROOT/scripts/wd.py" followups --list
python3 "$WD_ROOT/scripts/wd.py" followups --triage 12 --decision pursue --note "已加微信"
python3 "$WD_ROOT/scripts/wd.py" followups --feedback chat:xxx@chatroom --verdict false_positive  # 整群抑制噪声
# 输出解读（证据分级/角色归属/待回复vs承诺语义/复联规则）见 references/signal-rules.md

# 会话质量排查（识别线报/羊毛/广告类低增量信息群，多信号加权避免误报）
python3 "$WD_ROOT/scripts/wd.py" chats --window 30                       # 全部分层列表+证据
python3 "$WD_ROOT/scripts/wd.py" chats --level noise                     # 只看噪音群
# 确认排除后 followups/roundup 永久跳过该群

# 跨会话信息整合成文（markdown 产物落 default_data_root/roundups/）
python3 "$WD_ROOT/scripts/wd.py" roundup --match "线报" --window 7       # 同类群汇总（点名模式不排噪音）
python3 "$WD_ROOT/scripts/wd.py" roundup --keyword "简历" --window 90    # 跨群主题提取（默认排噪音）
python3 "$WD_ROOT/scripts/wd.py" roundup --mine --self "我的昵称" --window 30  # 我的发言+上下文梳理

# 跨次画像快照（自动与上次 diff：新增/消失话题、新成员、消息增量）
python3 "$WD_ROOT/scripts/wd.py" analyze --chat "群名" --mode summary --snapshot

# 实验室报告（welink 精华）/ 关系报告（垂直 skill 精华）
python3 "$WD_ROOT/scripts/wd.py" report --chat "群名" --flavor lab --print-body
python3 "$WD_ROOT/scripts/wd.py" report --chat "好友" --flavor dyad --print-body
python3 "$WD_ROOT/scripts/wd.py" analyze --chat "群名" --mode lab --input msgs.json
```

**双引擎契约**：默认 vault（离线）；`--source wxcli` 强制在线；`CAPABILITY_MATRIX` 按 op 选引擎（search 首位 fts、history 兜底追加 fts，`biz-articles`/`sns-*`/`attachments` 仅 wx）。  
**不**把 wx 二进制 vendor 进 skill；不重写 SQLCipher。

wx 在线：`sudo wx init`（可能需 codesign）→ `wd.py wx info` 显示 ready。

## 数据能力边界（`coverage` 可复检）

| 层 | 覆盖 | 内容 | 引擎 |
|---|---|---|---|
| 全史文本 | 视解密副本，可回溯多年、百万条级 | 仅文本消息（含归档分片的文本） | fts |
| 富文本全量 | 视解密库范围 | 全类型消息+联系人+会话 | vault |
| 实时 | 此刻 | 新消息/朋友圈/公众号/附件 | wxcli |

**fts 编号体系（当前规则）**：
- c4=session_id 与 c5=sender_id **同属** fts 库自带 `name2id` 域（rowid 即序号）。session.db Name2Id、contact.db name2id、message_0.db Name2Id 均异体系，跨域直映射=无名+随机错名（错名比无名毒），禁止使用。
- search 输出契约：`{hits, total, has_more, limit}`，total 是关键词+全部过滤条件（含 --chat）下的全量命中数；每条 hit 含 `chat_type`（group/official/openim/wework/single）与 `sender_resolved`。**has_more=true 时不许下「只有 N 条」类结论**。

- fts 检索走解密副本 `message_fts.db` 的 content 表 LIKE（WCDB 分词器纯 SQLite 不可用，勿用 MATCH）

## 隐私

- 明文库不进项目工作区，索引/摘要产物落 `WECHAT_DIGGER_DATA`
- 不输出完整 key / salt / wxid（本包也不读取它们）
- 回复优先结论 + 路径 + 下一步

## 错误出口

- 环境异常 → `doctor` / `acquire-info` 盘点现状
- vault 未就绪 → 检查 `WECHAT_PRIVATE_VAULT` 指向的已解密库；需要解密请用你自选的工具完成后接入
- 无消息 → 放宽时间 / 核对群名

## DO NOT

- 编造聊天记录
- 硬编码本机个人路径
- 在对话打印完整密钥
- 毒舌版人身攻击 / 健康家庭身份推断

