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
version: 0.0.8.1-pub
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

## 数据能力边界（2026-09-06 审计口径，`coverage` 可复检）

| 层 | 覆盖 | 内容 | 引擎 |
|---|---|---|---|
| 全史文本 | 视解密副本，可回溯多年、百万条级 | 仅文本消息（含归档分片的文本） | fts |
| 富文本全量 | 视解密库范围 | 全类型消息+联系人+会话 | vault |
| 实时 | 此刻 | 新消息/朋友圈/公众号/附件 | wxcli |

**fts 编号体系（实证结论，v0.0.6.0 修订）**：
- c4=session_id 与 c5=sender_id **同属** fts 库自带 `name2id` 域（rowid 即序号）。session.db Name2Id、contact.db name2id、message_0.db Name2Id 均异体系，跨域直映射=无名+随机错名（错名比无名毒），禁止使用。
- c5 曾在 v0.0.5.1 被误判「不可靠」——是拿 c5 与 Msg.real_sender_id 做跨域比较（不同域数值天然不等，与 c4 错名同根）。恢复 c5→fts.name2id 映射后（300/300 与 Msg 表真值交叉验证），**归档层发送者身份全部可解析**，且不再逐条反查 message_0.db。
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

## Changelog

**v0.0.8.1-pub** — 媒体附加层同步轮（内部 0.0.8.1 工作区基线）：①`extras` 命令族落地：`status`（覆盖盘点）/`voice`（media_0 语音元数据，2022-04→）/`voice-export`（按 local_id 导出 SILK，不打印二进制）/`payments`（general.db 转账红包）/`requests`（好友申请）；②V2 `.dat` 图片离线还原：`extras images-discover`（XOR 缩略图 EOI 推导 + wxid KDF / 2^24 UIN 暴力，全离线）与 `extras images-decrypt`；pycryptodome 为**可选依赖**（仅图片层，核心分析层保持零 pip 依赖，缺失时报可操作错误）；③`vault` 系命令新增 `--start/--end` 日期窗（朋友圈/收藏夹时间过滤）；④边界修订：媒体文件离线解码不属于被剔除的「密钥提取 / SQLCipher 解密」，`extract_keys`/`decrypt_all_dbs`/`list_contacts`/`search_sns` 仍不分发（契约测试锁死）。测试 196 通过 / 21 跳过（无 Crypto 环境自动跳过图片解密用例）。

**v0.0.8.0-pub** — 首次随 session-digger 发布的公开版。相对完整内部版：剔除密钥提取（extract_keys）、SQLCipher 解密（decrypt_all_dbs）及自带解密的 legacy 采集件（list_contacts/search_sns），`keys`/`decrypt`/`refresh` 子命令随之不可用；保留 vault 只读查询（vault_cli/export_chat）与全部分析层；真实会话锚点改虚构名（锚点测试在无库环境自动跳过）。分析能力与内部版同源同基线。

**v0.0.8.0** — 噪音群治理 + 跨会话成文轮：①`chat_quality.py` 会话质量分层：群名特征词/发言集中度（广播群）/模板化刷屏/口令链接密度/广告 emoji 密度/**跨群同文**（同一内容短窗多群出现=群发嫌疑，归一键去数字防优惠码差异）七信号加权，问答密度与无支配多人互动反向降分，单点特征不定性；实测 105 会话分层 noise=6/low=7/normal=92，线报群全部命中且证据可解释。②`chats` 排查命令：分层列表+理由+确认入口（接 followups --feedback chat ignore）。③followups 扫描自动排除噪音群（自动识别+用户确认双层；显式 --chat 点名不排除；--include-noise 覆盖）。④`roundup` 成文命令三模式：`--match` 同类群汇总（点名不排噪）/`--keyword` 跨群主题提取（默认排噪）/`--mine` 我的发言+前后 2 条上下文梳理；去重引擎 URL+归一文本双键（优惠码数字不同的同模板也合并），N 群同发只记一条附出现群列表。108 测试全绿（95→108）。

**v0.0.7.0** — 分析能力吸纳轮（hub + session-digger 精华，数据仍全走自家 fts/vault 层）：①`followups` 新命令 + `followups.py` 状态机模块（hub opportunity_store lite）：商机/承诺信号持久化，candidate 14 天无新证据自动过期（stale）、新信号复活（reinforcement 强化计数），feedback 学习（confirmed 升正式机会/low_priority 封顶/false_positive·ignore 整群抑制），triage 分流 pursue/wait/pause/ignore/won/lost；待回复与等待对方按消息尾迹现算（含「对方短应答只关待回复不关承诺」语义）；复联双方向提醒。②`references/signal-rules.md` 方法论移植：证据分级（机器置信度不当事实）/角色归属（加热者不建联）/任务状态语义/复联规则/摘要质量。③`trends` 输出新增 `periods`：环比（messages/activeUsers/sentimentNet）+ 线性回归外推 2σ 异常检测（session-digger forecast-engine 精华；均值基线在爬坡/常数序列会误漏报，回归基线避免）；`fts_recent` 跨会话窗口扫描 op 落地。95 测试全绿（78→95）。

**v0.0.6.1** — 分析层接通 fts（瓶颈修复）：①`fts_history` 全史取数 op 落地（引擎矩阵 history 链早已预留 fts 位，此前适配器为 None），`analyze/history/digest/report/export-msg --source fts` 直达全史文本层；②空结果≠有覆盖：history/search 的空列表结果自动穿透到下一引擎（vault 查早期年份返回空 → 自动落 fts 全史层；全空仍返回合法空而非报错）；③尾部 `--source` 扩展到全部取数子命令（与顶层前置 `--source` 等效，argparse SUPPRESS 不互覆）；④新增对拍回归门（吸收 hub test_live_parity 精华）：search total 必须等于直接 SQL 全表 COUNT（无过滤/带 chat 过滤两组）。注意：fts 层仅文本消息，富文本/非文本分析仍走 vault。

**v0.0.6.0** — 数据口径统一轮（code-review 评审驱动）：①P0 c5 编号域纠正：实测 300/300 证明 c5 与 c4 同属 fts name2id 域，v0.0.5.1 的「c5 不可靠」是跨域比较误判；恢复映射后归档层发送者全部可解析，删除逐条反查 message_0.db 的整套机制（_NameBook 瘦身 1/3，每 hit 2 次 DB 查询→0 次）；②P0 search `total` 语义修正：--chat 过滤下推 SQL（`c4 IN`），total/has_more 从「全库值」变为「过滤后真值」，同时消除 cap=300 采样的静默漏数据；③P0 `--group-by` 修「先 top-N 后过滤」：过滤在 SQL 层先于截断，此前不在全局 top50 的会话会被错报为 0 命中；④hits 排序偏差消除：分片拼接后全局排序再取 limit；⑤性能：COUNT(*) OVER() 窗口函数单扫描同时取行+全量计数，contact 显示名一次性预载（修 19.6s chat 过滤）；⑥normalize `_to_ts` naive 字符串改按本地时区（原当 UTC，export 源与 vault 源混用时差 8 小时）；⑦分析层 topics 停用词过滤、graph 空 id 防碰撞；⑧`--snapshot` 强制 --chat（防 unknown.json 串聊污染）；search 子命令支持尾部 `--source`。

**v0.0.5.1** — 搜索修复轮（诊断误判事故复盘）：①P0 limit 硬编码 50 静默截断 → `--limit` 全链透传 + 输出 `total/has_more`；②P0 chat 名映射换真源：fts 库自带 name2id（原 session.db Name2Id 异体系，无名+随机错名，锚点「示例单聊A」曾错映射到无关会话）；③P0 sender 弃用 c5 直映射（v0.0.6.0 已纠正此判断）→ 短暂引入 (local_id,sort_seq) 反查 Msg 表机制，归档层 `u:<id>` 标注；④新增 `search --group-by chat` 会话聚合视图；⑤hit 增加 chat_type/sender_resolved 字段。

**v0.0.5.0** — 全史检索：`fts` 引擎接入（微信自带 message_fts 解密副本，文本全史覆盖）；search 默认走 fts + `--since/--until`；`coverage` 覆盖审计命令；修复 wx 0.3 文案判定/JSON 警告前缀/`--chat` 默认值误匹配/history limit 截断。

**v0.0.4.0** — 三系精华：`ANALYSIS_MODES`（lab/dyad/full）+ 排行/词云/时段热力/双人互动；`wd report --flavor lab|dyad` 实验室与关系报告。取数仍 vault+wx 双引擎，不 vendor 上游全仓。

**v0.0.3.0** — 双引擎路由：`wx_bridge` 表驱动吸纳 wx-cli 0.3 命令面；`CAPABILITY_MATRIX`；不 vendor wx 二进制。

**v0.0.2.0** — 采集自包含：vendoring 采集栈；`acquire_bridge` + `wd keys|decrypt|refresh|vault`。（发布版只保留 vault 只读件，见 v0.0.8.0-pub）

**v0.0.1.0** — 统一路由 + 四层模型 + FTS + 分析摘要。
