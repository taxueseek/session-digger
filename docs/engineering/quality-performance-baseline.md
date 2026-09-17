# Session Analysis Quality and Performance Baseline

## Problem redefinition

Session analysis is useful only when retrieval and synthesis preserve the evidence needed to explain what happened. The engineering objective is:

> Maximize evidence-backed insight per unit of scan time, memory, and model context while minimizing missed or duplicated signals.

## MECE dimensions

1. **Ingestion**: parsing coverage, malformed records, ordering, and timestamp normalization.
2. **Retrieval**: relevant-session recall, duplicate suppression, filtering precision, and search latency.
3. **Analysis**: clustering, pattern detection, chronology, and evidence linkage.
4. **Output quality**: factual traceability, actionable findings, uncertainty handling, and omission rate.
5. **Resource efficiency**: scan time, memory, context size, and repeated parsing/computation.

## Fixed evaluation corpus

Keep representative sessions covering short/long histories, repeated topics, noisy tool traces, missing metadata, multilingual text, and near-duplicate events.

## Quantitative gates

Measure retrieval recall/precision where labels exist, duplicate rate, evidence coverage, end-to-end latency distribution, peak memory, context footprint, and analysis regression rate.

## P1 optimization gate

Before changing parsing, retrieval, or analysis, establish that the target dominates cost or failure rate. Then make one change, run an ablation, and verify evidence coverage and output correctness remain stable.

## Experiment loop

`fixed corpus -> baseline -> profile -> hypothesis -> single-variable change -> ablation -> evidence regression -> scaling test -> retain/revert`

## 实施落点（2026-09-16）

- 固定语料：`scripts/quality_corpus.py`（7 场景 14 文件，证据标记全埋点）
- 测量 harness：`scripts/footprint.py`（`--json` 出全指标）
- 质量门禁：`tests/test_quality_baseline.py`（基线包络冻结在 `ENVELOPE`，改动需显式重录）

## 实测瓶颈台账（2026-09-17）

口径：本机真实索引（6290 会话 / 74263 条 FTS 行），同一命令、同一时刻前后各测一次。`limit` 指 `sd-recall search` 的结果数。

| 项 | 前 | 后 | 倍率 | 根因 | 落点 |
|---|---|---|---|---|---|
| 搜索 limit=20 | 0.89 s | 0.01 s | 44× | `evidence_from_index` 按 `session_id` 读 FTS = 全表扫描（UNINDEXED），单次命中 127 ms / 未命中 1240 ms，占搜索 85% | `_evidence.py` 索引期投影 + `sessions.user_evidence_json` |
| 增量构建（无变更） | 12.48 s | 3.88 s | 3.2× | ① 单遍读未接线，同一文件读 3–4 遍 ② dimcode 指纹冷启 2.30 s ③ 逐会话 FTS 删除 28.9 ms×N | `_session_analysis.py` / `_dimcode_session_fingerprints` / `_delete_scoped_rows` |
| 整体重建（6290 会话） | 255 s | ~70 s（估） | 3.6× | 同上；其中 182 s 是逐会话 FTS 删除 | 同上 |
| 索引体积 | 333 MB | 162 MB | −51% | FTS5 段从不合并：`messages_fts_data` 51 MB→219 MB，内容不变 | `_compact_index`（optimize + VACUUM） |
| 指纹阶段 | 2.45 s | 0.30 s | 8× | 同上 ② | 同上 |
| 单会话解析（大文件） | 205 ms | 75 ms | 2.7× | 同上 ① | 同上 |

比对方式：修复前后对同一命令取 wall time；正确性用字段级等价（37 会话 × 19 字段零差异）与排序重合度（Top-15 逐条重合 15/15、15/15、14/15，新增命中来自被找回的 zcode_v2 会话）验证，不只看耗时。

**未采纳**：`keep-longest` 近重复折叠（v0.9.22 已拒）、`MAX(orderKey)` 替代 `MAX(createdAt)`（覆盖索引可省 ~80 ms，但语义不如"会话行 updatedAt"直接）、外置内容 FTS 表（`content=` 可将三处按会话访问全部转为索引命中，但需全表重建，当前投影方案以 3 MB 代价拿到同一收益）。

### 口径修正与复核（2026-09-17 二次轮）

上表几行的口径经复核后需要收窄，另补一轮只动代码、可复现的测量。

**无法复现或自相矛盾的行**

- 第 2 行（增量构建 12.48 → 3.88 s）归因不成立：无变更构建里 0 个会话进入解析，
  `_delete_scoped_rows` 收到的是空列表，①③在这条命令上不可能生效。该行的主要增益
  实际来自同轮未进台账的两处——`_backfill_session_roles` 的 `needs_role` 守卫
  （此前每次构建都全表重写 `session_role`，SQLite 无法跳过 no-op 更新，逐页翻动整表）
  与 `_dimcode_session_fingerprints` 的覆盖索引改写。
- 第 1 行（搜索 0.89 → 0.01 s）的 127 ms/1240 ms 在同一索引副本上复测不到
  （实测命中 8–27 ms、未命中 27 ms、冷启单次 1.46 s）。20 × 127 ms = 2.54 s 也大于
  同表所记的 0.89 s，两数互斥。"占搜索 85%" 因此缺落点。结论方向（去掉按
  `session_id` 扫 FTS）仍成立，绝对值需重测。
- 第 5 行 2.45 → 0.30 s（8×）与 SKILL.md 同项 2.30 → 0.078 s（29×）互相矛盾，两者取其一。
- 第 3 行"~70 s（估）"是估值，SKILL.md 按实测引用；估值与实测需分开标注。
- 「`--decisions` 原先只在前 300 字上匹配」不实：旧实现 `pat.search(display)` 匹配的是
  未截断文本，300 字只作用于展示。该行的召回"不降反升"因此无从成立。

**本轮新增（可复现）**

| 项 | 前 | 后 | 倍率 | 根因 | 落点 |
|---|---|---|---|---|---|
| 日常无变更构建 | 2.08 s | 0.42 s | 5.0× | 发现阶段逐文件解析 JSONL 取 preview，而索引器只读 id/path，整段丢弃 | `echolib.discovery_only` + 7 处 preview 守卫 |
| `scan_sessions` | 5.40 s | 1.05 s | 5.1× | 同上 | 同上 |
| 插件/UI 列表（`limit=0`） | 静默返回 `[]` | 返回全部 | — | 11 个适配器写 `items[:limit]`，0 被当成"取零条" | `echolib.cap`，一处规则 |
| cc-switch 环境发现 | 0.594 s | 0.017 s | 35× | universal 兜底 `rglob` 遍历 `~/.cc-switch` 全部 111,777 文件，只为拿到 `backups/codex-history-*/` 里 4 个 codex 备份 | `_rglob_jsonl` 按目录名剪枝 |
| 陈旧会话行 | 53 行常驻 | 0 | — | 索引只增不减；不被重新发现的会话永不重写，连带冻结旧 schema 值（49 行证据停留在空串） | `_prune_stale_sessions` |
| `elapsed` 口径 | 自报 0.85 s / 真实 4.14 s | 自报 0.42 s / 真实 0.42 s | — | `t_start` 落在 `init_db`/角色回填/6337 行指纹预载/`scan_sessions` 之后 | `t_start` 提到函数首行 |

**列举层（herdr 面板 / fuzzy 浏览日常路径，2026-09-17 第三轮）**

| 项 | 前 | 后 | 倍率 | 根因 | 落点 |
|---|---|---|---|---|---|
| `recall sessions --limit 20` | 1.63 s | 0.20 s | 8× | ① 发现阶段 preview 被丢弃（1.62→0.11 s）② 每行 parsed 整个 transcript 取 created/msgs/branch（索引已有） | `_list_from_registry` 的 `discovery_only` / `session_stats_by_path` |
| `recall sessions --limit 200` | 3.12 s | 0.25 s | 12.5× | 同上 | 同上 |
| `herdr-search` | 1.76 s | 0.64 s | 2.8× | 同上（该脚本无关键词时落到 `sessions`） | 同上 |
| 列表顺序 | 三次运行互不相同 | 完全一致 | — | 合并用 `as_completed()` 完成顺序，而它决定轮转起点 | 按提交顺序轮转 |
| 环境枚举结果 | 72 条 / 19 个重复 env_id | 51 条 / 0 重复 | — | `known_dirs` 取 `expanduser(root).split("/")[0]`，绝对路径下恒为空串 → 注册环境全被当成"未知目录"再报一次 | 按 home 相对路径取第一段 |

**id 与路径的命中率（决定连接键）**：同一批 200 个会话，索引按 id 命中 **0/200**、按 `jsonl_path` 命中 **200/200**。索引键是 `{env}:{adapter_id}`，列举层返回裸 adapter id。跨层连接一律用 `jsonl_path`。

**列举层字段级等价**：`--limit 20/100/200` 共 320 行，与旧实现（逐文件解析）逐字段对拍，**差异 0、顺序一致、条数一致**。回退条件为"索引缺该会话"或"文件 mtime 比 `jsonl_mtime` 新"，故显示值仍实时。

### 测量口径纪律（2026-09-17 补）

上表与历史台账里若干数字对不上，共同原因是**没写清冷热缓存与取样方式**。同一段代码在本机实测：

| 片段 | 第 1 次 | 后续 | 说明 |
|---|---|---|---|
| 指纹循环（6293 文件） | 0.862 s | 0.174 / 0.176 / 0.224 s | 第 1 次是冷 page cache |

规则（此后所有行都按此记录）：

1. **取 3 次以上的最小值**，并注明是 min-of-N；单跑一次的绝对值不足以进台账。
2. **注明是否预热**。冷启动首次读盘可以是最小值的三倍以上，不标注就会得出"某处很慢"的错误归因（本轮就曾据此把指纹阶段写成 0.97 s，实为冷缓存）。
3. **前后对比必须同一份数据副本、同一命令、同一取样方式**。跨时刻的两个数字（尤其跨了全量重建）不构成对比。
4. 归因到某个函数前，先用消融把该函数换掉再测一次；只有"换掉它、耗时随之变化"才算归因。本轮 `discovery_only`、`session_stats_by_path` 两处都做了开关级消融。
5. 估值与实测分开标注，估值不进对比表。



复现命令：`SESSION_DIGGER_DATA_DIR=<副本> python3 scripts/index-builder.py build --agent cross`，
取 `/usr/bin/time -p` 的 real。发现阶段的归属用
`python3 -c "import sys;sys.path.insert(0,'scripts');import index_builder._builder as B;print(len(B.scan_sessions('cross')))"`
前后对比；开关级消融见 `tests/test_discovery_only.py`。

**字段级等价（整轮）**：同一份代码、同一批环境、两个空索引，`discovery_only` 开/关各建一遍
（各 6287 会话）。`sessions` 表 32 列**零差异**，`messages_fts` 74045 行逐行相同，
`topic_boundaries` 27394 行逐行相同，会话 id 集合相同。证据投影对 40 个抽样会话与
文件扫描兜底路径逐条比对 `user_messages`，**零不一致**。


### 正确性缺陷（2026-09-17 第四轮，剩余未提交改动）

三条都带可复现证据，前两条改变索引内容，故附 `_PARSER_EPOCH` bump 与一次性重解析成本。

| 缺陷 | 可复现证据 | 影响面 | 落点 |
|---|---|---|---|
| DSH `data.source.kind` 白名单把用户指令当注入丢弃 | 全机 kind 普查：`coordinator` 7 条 / 3 会话；会话 `79f834b8…` 用户轮次索引为 1，实际 4 | DSH 全域 `user_messages` 485 → 492（+7） | 反向白名单 `_DSH_INJECTED_KINDS`（丢弃 810 条已知注入） |
| token 累加无类型守卫，坏字段炸整批构建 | 构造 `"input_tokens": null` 的 transcript → `TypeError` 从 `_compute_session` 抛出并穿透 `pool.map` | 单条坏数据 = 整次构建失败；9 处累加同类 | `_as_int()` 统一入口（含 `int(inf)` 的 OverflowError 防护，`json.loads` 默认接受 `Infinity`） |
| zstd 坏帧静默归零 | 截断的 `.zstd` → 0 记录、0 日志，与"空会话"/"文件已删"不可区分 | 5 条失败路径全部无信号 | 每条失败路径带原因 warning |

语义类修复的**回流条件**：指纹 = 内容 + `_PARSER_EPOCH`，只改解析语义而不 bump epoch，修复仅对新会话生效。本轮 bump 到 `v6-dsh-kind-denylist-and-counter-coercion`，一次性全量重解析 6287 会话 **46 s**（warm cache，min-of-1，属一次性成本）。

**已核实未改**：单遍读 vs 多遍路径在 claude+zcode_v2 全部 134 文件上 6 组字段零差异；`topic-segmenter.simple_tokenize` 与 `_cjk.tokenize` 不等价（`a-b_c/d` → `['a','b','c','d']` vs `['a','b_c','d']`），是第二份分词实现，改动会改变 `/topics` 输出，未动。

### 第五轮：把成本口径换成「每次激活」与「常驻磁盘」（2026-09-17）

前四轮都在压 CPU，本轮先重测一遍确认没有可压的了，再把口径换到真正的大头。

**CPU 侧已无数量级瓶颈**（min-of-3，热缓存，6290 会话 / 169MB 索引）：

| 入口 | 耗时 | 说明 |
|---|---|---|
| `sd-recall search --limit 20` | 0.07 s | 投影齐全时 |
| `sd-recall sessions --limit 200` | 0.44 s | |
| `index-builder build --agent cross`（无变更） | 0.41–0.47 s | 自报 0.41–0.47，与墙钟一致 |
| `deep_analyze --days 7 --top 5` | 0.18 s | 索引新鲜时跳过重建 |
| 其余 13 条日常入口 | 0.06–0.20 s | |

无变更构建 0.45 s 的分解（cProfile 定归属，绝对值以墙钟为准）：发现 `scan_sessions` 171 ms（30 个环境，**最慢单项 26 ms**，`qoder-cn`——整个 dot-dir 遍历换 6 个会话）；文件指纹 108 ms（2028 个文件，各读头 4KB + 尾 4KB）；其余为 sqlite 与角色回填。

**指纹为什么不动**：改成只比 `(mtime, size)` 可省 0.10 s（约 22%），但要加一列 schema、触发一次全量重解析，并放弃「mtime 与 size 都不变而内容变了」这一档检测。0.10 s 不值这个代价，故**不采纳**。同理，把 `messages_fts` 换成外置内容表（`content=`）在体积上是零收益——现在的 `messages_fts_content` 107MB 就是那份内容副本，换成真实 `messages` 表后同样 107MB。

**真正的大头两项**：

| 项 | 前 | 后 | 口径 |
|---|---|---|---|
| 每次激活的 SKILL.md | 32,150 字符 ≈ 12.3k token | 6,939 字符 ≈ 2.6k token | 按 CJK/ASCII 混合估算；历史条目占原篇幅 79% |
| 数据目录常驻 | 1.1 GB | 见下 | `index.db` 338MB（未压缩）+ 3 个 `.bak-*` 727MB + `reports/` 17MB |

**未处理（需人决策）**：`~/.claude/.session-digger/` 下 3 个 `index.db.bak-*`（`20260907-precjk` 91MB / `20260915-prerebuild` 303MB / `20260917-prefix` 333MB）共 **727MB**，是历次迁移前的回滚快照，不是代码产物。索引本身按架构是 Layer 1「可重建缓存」，但删快照不可逆，且同一卷内移到 `.trash/` 并不释放空间，故留给用户决定。另：线上 `index.db` 仍是 338MB 且 6531 行**全部** `user_evidence_json=''`——它由 v0.9.19 的安装副本写成（该副本不认 v4 迁移）。跑一次 `index-builder.py build` 即可同时解决体积（实测 324MB → 169MB）与证据投影。

### 已定位但未修：扫描范围与索引范围不一致（183 行永久冻结）

`scan_sessions` 只遍历 `ENV_REGISTRY ∪ KNOWN_UNADAPTED`；`scan_all_environments_parallel` 另外扫 `$HOME` 下未知 dot-dir 并报为 `discovered`。两套「环境」定义不连通，于是**被发现的目录不会被索引，已索引的行不会被重访**。

实测：6531 行里 183 行（`pi:` 166 / `kimixi:` 8 / `taxue:` 5 / `kiro:` 2 / `jimeng-cli:` 1 / `opencodex:` 1），文件全部存在，`indexed_at` 停在 10:18 而同一时刻其余 6348 行是 11:16——**它们永远不会更新**。`_prune_stale_sessions` 的「在注册表根目录之外则保留」护栏又让它们免于被清掉，于是既不会刷新也不会消失，且（修复前）渲染为零证据。

修复方向有两条，都要动设计而不是打补丁：(a) 让索引发现复用报告层的发现结果——代价是自动索引任意未知 dot-dir，在别人机器上是隐私与耗时问题；(b) 把可索引环境显式登记进 `KNOWN_UNADAPTED`——代价是发布版里出现本机路径。本轮只做了第三件事：让读路径不再把「未计算」读成「没有」（`projection_available`），使这 183 行的**输出**恢复正确；数据陈旧问题如实留在台账上。

