# 任务分析矩阵与高频 Prompt 模板（MECE）

> 按「第一性原理 + 奥卡姆剃刀 + MECE」整理：把会话挖掘与日常指挥拆为 5 类互斥且穷尽的任务，每类给出 **可得到的分析产出** 与 **可直接复用的优质 Prompt**。

## 一、五类任务分析矩阵（MECE）

| 任务类型 | 对应入口 | 能得到什么分析产出 | 适用时机 |
|---|---|---|---|
| **T1. 断点接力与上下文恢复** | `/recall` / `/recap` | 未完成任务清单、最后决策点、当前卡点与可立即续接的下一步 | 跨天继续、上下文压缩后、切换 Agent 接力 |
| **T2. 方案收敛与快速拍板** | 直接对话 / `deep-analysis` | 多方案优劣矩阵、最小可行改动范围、明确的执行授权 | 面对多个可选方案或需要快速推进本地改动时 |
| **T3. 版本对比与架构评审** | `/timeline` / `git-mining` | 本地版 vs 上游/公开版差异、改动收益与副作用、冗余抽象清单 | 多轮迭代后收口、准备合并或对外同步前 |
| **T4. 规则/Skill 审阅与根因排障** | `/analyze` / `/optimize` / `/audit` | 指令冲突、不必要停顿点、重复报错模式、可落盘的 Skill 改进提案 | Agent 频繁停下来问确认、或同一类错误反复出现 |
| **T5. 高频 Prompt 与习惯挖掘** | `/deep-analyze` / `/reflect` | 个人高频口头禅、任务分布、可固化为快捷指令/Skill 的重复模板 | 定期复盘（周/月度），把隐性指挥习惯沉淀为显性资产 |

---

## 二、可直接复用的优质 Prompt 模板

### T1. 断点接力与上下文恢复
- **极简接力**：
  `继续未完成的任务。先用 3 句话对齐当前进度、未完成项与卡点，再直接推进下一步。`
- **跨 Agent 交接**：
  `梳理一下最近 [Kimi Code / Antigravity / DSH] 未完成的任务，列出已改文件、遗留问题和可直接接手的执行步骤。`

### T2. 方案收敛与快速拍板
- **多方案收敛**：
  `梳理下制作/改造方案，基于奥卡姆剃刀与第一性原理给出 2-3 个可选方案（含改动面与代价），确认后再继续。`
- **极简拍板**：
  `执行方案 [A/B]，按最小改动推进，改完跑靶向验证并汇报结果。`

### T3. 版本对比与架构评审
- **内外/前后版本对比**：
  `梳理一下：与公开版本（或上一版）相比，本地版又做了哪些新的改进？其优劣之处、高明之处与不合理的地方分别在哪里？`
- **多轮迭代收口**：
  `梳理下近期 [项目/Skill] 的改进，对比前后几轮的效果与代码质量，指出冗余抽象与可以删减的部分。`

### T4. 规则/Skill 审阅与根因排障
- **指令冲突与停顿点审计**：
  `审阅我的 AGENTS.md 和相关 Skill，寻找不清晰、冲突或重叠的指令——特别是会导致不必要停顿、要求多余确认或工作不完整的规则。`
- **甩链接/外部线索核查**：
  `[URL] 核查下这里提到的现象或问题在当前项目里是否存在，给出实证依据与最小修复方案。`

### T5. 高频 Prompt 与习惯挖掘
- **个人指挥习惯体检**：
  `翻下最近 [1/2] 个月的主会话用户 Prompt（排除子代理与系统注入），按 MECE 归类我最常发的指令与口头禅，并提炼成可直接复用的 Prompt 模板。`
- **多维度深度调研起手式**：
  `基于 MECE 原则、奥卡姆剃刀与第一性原理，调研 [目标/数据源/方案]，给出可落地的筛选结论与对比表。`

---

## 三、用户 Prompt 提取与去噪口径（Layer 0/1 实测经验）

做 T5（高频 Prompt 挖掘）或跨端统计时，必须先过以下四条硬过滤，否则系统噪音会淹没真实习惯：

1. **只保留主会话**：
   - Claude Code：排除 `agent-*.jsonl` 子代理文件，仅取 `type == "user"` 且非 `isMeta`、非 `tool_result`。
   - DSH（DeepSeek Harness）：`session.v4.jsonl.zstd` 用 `zstd -dc` 解压，跳过首行 `delegationDepth > 0` 的子代理会话与 `--private-tmp` 探针目录。
   - Antigravity：取 `type == "USER_INPUT"` 且 `source == "USER_EXPLICIT"`，正则剥离 `<USER_REQUEST>...</USER_REQUEST>` 正文（去掉 `<ADDITIONAL_METADATA>`）。
   - OpenCode：`opencode.db` 中限定 `session.parent_id IS NULL` 且排除 `/private/tmp` 目录。
2. **剥离 Harness 注入消息（尤其 DSH `user/message`）**：
   排除 `Current runtime context.`、`The approval policy changed`、`The user changed the approval policy`、`[BUDGET NOTICE]`、`[BUDGET CONVERGENCE]`、`[EXPLORATION NOTICE]`、`You are repeating the exact same tool call`、`本回合已调用工具`、`[Message from Chief`、`[model changed`、`<skill_content`。
3. **分离固定评测/探针题**：
   如压测固定句（`say hello`、`Reply with exactly: OK`、`只回复两个字：正常`）或重复跑的写作/Eval 模板，统计时单列，不与日常干活 Prompt 混算。
4. **统计口径**：
   同时统计「出现总次数」与「跨会话数（unique sessions）」，优先按跨会话数排序，避免单次循环重试拉高权重。
