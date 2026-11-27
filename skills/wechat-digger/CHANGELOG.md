## Changelog

**v0.0.9.3-pub** — 检索提速轮（内部 0.0.9.3）。内部版修了一个根因级瓶颈：JOIN 把全部命中物化进临时 B 树才开始排序，高频词（35 万命中）要 2-8 秒。解法是让索引的 rowid 与时间序对齐，检索便可走 O(limit) 的早停路径。实测 `search 的` 8.2s→0.16s，全场景稳定在 0.3s 内；时间窗与群过滤同批受益。降级仍以正确性优先：乱序写入自动回退旧排序，索引缺失/损坏照旧回退全扫。顺带修两个静默缺陷（聚合检索的未导入名、拼音缓存表名写错）并清理死代码。**本包契约不变**：不带密钥提取与解密实现；微信数据只在本地读写，不联网不上传；反馈请勿附带隐私文件。

**v0.0.9.2-pub** — 全史 FTS5 索引轮（内部 0.0.9.2，2026-09-19）。检索层按 argo 的改进思路重做：**①全史索引**——`index --full-history` 一次重建毫秒级 FTS5 索引（此前每次查询 LIKE 全表扫描约 1 秒），rowid 上界判新鲜 + 增量同步，索引不可用自动回退旧路径且结果口径不变（正确性优先于速度，兜底异常持久化进 index meta 供 doctor 读）；**②CJK 边界三修**（同源 session-digger 已同步）——数字+CJK 粘连、emoji 并入 token、符号边界，三类静默漏召回消灭；**③语义口径守门**——MATCH 与 LIKE 的语义分叉场景（多词/ASCII 子串）回退旧路径，宁可慢不可错；**④聚合下推**——会话聚合 898ms→175ms；**⑤新能力**：`--rank bm25` 相关度排序、`--chat` 拼音三级匹配、0 命中建议、`ai-monthly` 月度 AI 使用画像（14 品牌归一 + 参与人数/样本置信 + `--html` 纯静态 SVG 可视化报告，默认落本机数据目录）、sns-feed/sns-search/sns-notifications/biz-articles 四命令原生直读已解密副本（朋友圈时间线/全文搜索/互动通知/公众号文章，零 daemon 零微信运行时依赖）；**⑥依赖与消融**——全量消融实验删净死检查/无读者状态/重复 import，第三方依赖终态仅 zstandard（必要）+ pycryptodome（可选，图片层）。**本包契约不变**：不带密钥提取与解密实现；微信数据只在本地读写，不联网不上传；反馈请勿附带隐私文件。

**v0.0.8.2-pub** — 复核轮：列表命令的 `limit` 语义与坏数据护栏

对未提交的媒体附加层做缺陷审查，五条实测确认，都属"静默给出错结果"这一类。

- **`extras voice --chat` 会静默返回空**（召回洞）：chat 过滤原先在 Python 里、SQL 的 `LIMIT` **之后**执行。实测本机某群有 40 条语音，`limit=20/50/200` 全部返回 `count=0`，只有 `limit>=3893`（全表）才找得到——因为最新语音被少数几个活跃群占满，任何单聊都会中招，而单聊正是这个参数的主要用法。改为在 SQL 里先按 Name2Id 解析并过滤，`LIMIT` 之后再没有可丢的东西。顺带：查无此会话时返回带说明的 `note`，不再是一个光秃秃的 0。
- **`extras payments --limit` 是每表生效**：limit 被分别传给两张表，合并后最多 2×limit（实测 `--limit 1` 返回 2 条、`--limit 3` 返回 6 条）。改为合并后按时间倒序统一截断（红包表无时间列，排在定时行之后）。另外 `--since/--until` 对红包表本就不生效，此前静默忽略，现在会在 `note` 里说明。
- **损坏的 `.dat` 会"成功"解出垃圾**：`raw_end = len(data) - xor_size` 可为负，而负索引切片 `data[raw_end:]` 拿到的是几乎整个文件、不是预期的尾部块。于是下载中断或头部损坏的 `.dat` 会被记成解密成功，且**输出比输入还长**（实测 33 字节进、48 字节出）。已加护栏：尾部块不得回插进 AES 块，违反即 `ValueError`。
- **`extras images-decrypt` 没有 `--no-brute`**：它与 `images-discover` 共用 `discover_keys()`，而后者早就有该开关；缺 key 时 `images-decrypt` 会直接进 2^24 UIN 爆破（本机实测约 70k keys/s、多线程无增益，16–29 分钟）且无法跳过。补上开关，并把 `brute` 透传给 `discover_keys`。爆破过程同时加了每 10% 的进度输出——原先每个 KDF 只打一行起始，4–7 分钟内毫无信号，与卡死无法区分。
- **`decrypt_batch` 为返回 20 条而保留全部记录**：每条都 append 进 list，最后只返回前 20 条。真实 attach 树 34.8 万个 `_t.dat` 就是 34.8 万个 dict 与其路径常驻。改为只留前 20 条、其余只计数，并新增 `scanned` 字段。
- **`extras status` 读两个不存在的键**：`fts.thin_months` 与顶层 `extra_layers` 全仓无生产者（SYNC.md 自承该缺口回填未同步），却一直被读并按 `null` 打印。按"没实施的不介绍"移除这两项读取。
- **修掉自己的疏漏**：`image_dat.py` 里 `sys` 只在函数内局部导入，新加的状态输出触发 `NameError`——由新增测试先抓到，已提到模块级。
- **版本一致性**：`combo_map.json` 的 version 从 `0.0.8.0` 对齐到 `0.0.8.1-pub`。
- **回归门 +12 项**：新增 `tests/test_extra_layers_contracts.py`（voice 过滤与截断的先后、payments 的 limit 语义与时间过滤说明、查无会话的说明字段）与 `tests/test_image_dat.py` 的损坏头用例（超大 xor_size 必须拒绝、**解密输出不得长于输入**这条不变量、批量记录数有界、limit 生效）。全部为自足测试（构造合成库并替换根解析），不依赖本机 vault——原有的 live 测试在无库时整类跳过，恰是回归最容易漏掉的时刻。测试 **234 通过 / 18 跳过**。

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
