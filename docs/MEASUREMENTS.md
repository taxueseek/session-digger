# Measurements

> 本仓库所有性能数字集中在这页。每行带日期、口径、样本数和复现方式；页外
> 任何地方（CHANGELOG、SKILL.md、对话、记忆）出现的性能声明若在本页没有对应行，
> 一律视为未证实。借鉴 greenbubbles 的 MEASUREMENTS 纪律（2026-10-02 引入）。

两条前提：

- **全部数字来自机主单机**（Apple silicon，macOS 26.6，arm64，Python 3.14 /
  sqlite 3.53）。不外推到其他机器；同一命令的绝对耗时随系统负载波动很大
  （同一读数出现过 0.21s 与 0.5s），跨时刻对比一律用 git worktree 交错 A/B 或
  min-of-N，不用单点绝对值下结论。
- **本项目的延迟目标从未正式设定过**，因此也没有「达标/未达标」的说法。
  每轮优化的目标是消灭当轮实测出的最大洼地。

## 检索（FTS 热路径）

| 日期 | 场景 | 前 → 后 | 倍数 | 口径与来源 |
|---|---|---|---|---|
| 09-15 | 按会话批量删 FTS 行 | 193.6s → 17.5s | 11× | 500/批 `_bulk_delete_by_session`；真库实测 |
| 09-17 | search limit=20 | 0.89s → 0.01s | 44× | `session_id` UNINDEXED 全表扫描类清零 + 证据索引期投影；真库 |
| 09-17 | 增量构建 | 12.5s → 3.9s | 3.2× | 同上轮；真库 |
| 09-17 | 全量重建 | ~255s → ~70s | 3.6× | 同上轮；索引 333→162MB（FTS5 optimize+VACUUM，重写 ≥10% 触发） |
| 09-26 | stats 的 dispatch_session_stats | 0.248s → 0.078s | 3.2× | 170ms 在重复 dispatch；CHANGELOG v0.9.30 |
| 09-29 | 批量 stats+tools+messages（200 zcode 会话） | 1.471s → 0.181s | 8.1× | 索引期投影复用；CHANGELOG |
| 09-29 | `--topic` 路径（limit=500，477 会话） | 4.62s → 2.91s | 1.58× | 同上 |
| 09-29 | stats 跨环境 COUNT | 37.6ms → 0.0014ms | — | `index_meta('total_fts_rows')` 构建期写、读侧直取；CHANGELOG v0.9.32 |

## 会话发现（topic-scan 家族）

| 日期 | 场景 | 前 → 后 | 倍数 | 口径与来源 |
|---|---|---|---|---|
| 09-26 | topic_classify.get_all_sessions | 4.9–5.2s → 1.5–2.2s | ~3× | discovery_only 包裹 + first_prompt 两级回填；worktree 交错 A/B；first_prompt 非空率 13%→94% |
| 09-29 | discovery 轻量枚举（裸调 cross_tool_list_sessions limit=0） | 块外 1.53s → 块内 0.158s | ~10× | 默认反转：轻量枚举是缺省，预览是显式选项；worktree A/B，215MB 语料 |
| 09-29 | 端到端 topic-scan（部署副本 v0.9.31 → 本版） | stats 0.191s → 0.125s；min 1.76s | — | CHANGELOG v0.9.32 |

## 内存与占用

| 日期 | 场景 | 前 → 后 | 口径与来源 |
|---|---|---|---|
| 09-29 | zstd 解压峰值（825KB 压缩 / 5.1MB 解压 / 8957 行） | 36.1MB → 4.7MB | staged 解压 1MB 块；全有或全无校验保留；CHANGELOG v0.9.33 |
| 09-17 | 索引库体积 | 333MB → 162MB | FTS5 段不自动合并，optimize+VACUUM；09-19 CJK 重解析后再测 249→160MB |

## DSH 插件形态（2026-10-06，v0.9.34 双形态落地）

| 日期 | 场景 | 实测 | 口径与来源 |
|---|---|---|---|
| 10-06 | 插件工具调用（sd_search，node→python 单发） | 77–81 ms；冷启动 224 ms | 真库，min-of-3；与直接调 CLI 的 76–81 ms 无差别（spawn 跳跃在噪声内） |
| 10-06 | sd_stats 工具调用 | 79 ms | 同上 |
| 10-06 | 插件常驻上下文成本 | ≈862 tokens/turn（工具 schema 768 + 检索预算段 94） | chars/4 口径；skill 形态为触发时一次性 ≈1138 tokens（SKILL.md 4554 chars） |
| 10-06 | 对照：MCP 全量面形态（未采用） | ≈3.7K tokens/turn | argo-dsh 实测口径；本地索引查询是低频动作，常驻面纯负债——选 CLI 单发的量化依据 |

## 正确性（并发与降级，非速度）

| 日期 | 场景 | 结果 | 口径与来源 |
|---|---|---|---|
| 09-29 | apply-rules 8 进程并发 append / memory | 8/8 幸存、单标题 | flock 临界区；`test_apply_rules_lock.py` 双真并发子进程钉死 |
| 09-29 | zstd 坏帧 | 零记录 + 告警，不产半截 | staged 解压全有或全无；`test_adapter_robustness` |

## 全量基线（2026-10-02，本轮实测）

| 命令 | 实测（3 次 min–max） | 备注 |
|---|---|---|
| `sd-recall.py search` | 0.08–0.15s | 热路径 |
| `sd-recall.py --recap` | 0.07–0.08s | |
| `sd-recall.py usage` | 0.07–0.10s | |
| `sd-recall.py reflect` | 0.18s | |
| `sd-recall.py dashboard` | 0.10–0.13s | |
| `sd-recall.py topics` | 0.10–0.27s | |
| `topic-scan.sh --limit 20` | 0.34–0.77s | 含 shell 进程 |
| `index-builder.py build` | 4.41s（含 3 天窗口）/ 2.38s（无新货地板） | 地板里 dimcode 指纹冷扫 ~0.85s，见「量过没做」 |
| 全量测试套件 | 477 passed / 6 failed → 本轮后全绿，~113s | 6 红为模型归一口径迁移遗留，见 v0.9.34 |

复现：`for i in 1 2 3; do /usr/bin/time -p python3 scripts/<cmd> 2>&1 >/dev/null | grep real; done`（先 `export SESSION_DIGGER_ROOT` 指向本仓）。

## 量过但不做（纪律，防止重复踩）

| 想法 | 量出的上界 | 放弃理由 |
|---|---|---|
| 指纹阶段跳读未变文件（09-24） | 可省 0.138s，约全量构建 5% | 代价是 schema 迁移，不抵 |
| summary_cache_info 批量化（09-25） | 每文件几 KB，50 次 stat <1ms | 无值得改的收益 |
| wechat-digger fts 全量物化改迭代器（09-25） | — | 改变归并顺序影响分页正确性 |
| TOOL 面并入默认排名（09-24） | 召回 +10% | top-20 版面 35% 被工具摘要占掉，10% 换 35% 不划算 |
| 加密文本缓存（greenbubbles 同款决策） | 省 ~1/3s | 要在盘上留第二份消息副本 |

## 已知未动（量化过，设计级）

- 增量构建无新货地板 2.38s 中，dimcode 指纹冷扫 ~0.85s（覆盖索引物理读，v0.9.31 起记录在案）。
- ZCode family 分账恒判 rollup（子代理 token 被丢）、`_policy.py` 的 `input_includes_cache` 口径与实测相反、dsh `cache_hit_rate` 全 NULL——各自有独立语义待定（09-17 排查结论）。
- 模型归一标签的历史行：v0.9.29/34 的口径修正不触发全量重解析（沿用 09-29 先例），旧行标签随自然增量更新收敛，非全库一致。
