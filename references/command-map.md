# 子命令查表（SKILL.md 搬出）

> 逐字搬移，未做改写。这张表是**查表用**的，不是路由逻辑：SKILL.md 每次激活都整体
> 进上下文，而这里 28 行只有在「主表没命中、需要找冷门入口」时才需要读。

以下命令仍可用，经标志、子命令文件或专项 skill 进入（不必从主表记忆）：

| 用户说的 | 去 |
|---------|-----|
| 模糊浏览会话（fzf） | `/recall-fuzzy` |
| 时间线、项目进展 | `/timeline` |
| **错误根因 / 意图分类 / 这次为啥失败** | **`deep-analysis`**（先于泛化 `/analyze`） |
| 提炼经验、找重复模式 | `experience-synthesis`（错误多时可先 deep-analysis） |
| 管理记忆文件、审计/清理 | `memory-management` 或 `/audit` |
| 解析会话数据 | `jsonl-core`（底层仍是 echolib） |
| 挖掘 git 历史 | `git-mining` |
| 保存分析结果供复用 | `/save-summary` |
| 找错误模式、重试循环、用户修正 | `/analyze` |
| 数据包 → 模型自发分析 → 结论回存（一站式深析） | `/deep-analyze` |
| 趋势分析、周/月环比、工具回归检测 | `/trend` |
| 跨会话技能差距分析、SKILL.md 提案 | `/optimize` |
| 技能资产自检（路由覆盖/硬编码/安装漂移） | `skill-insight`（`scripts/skill-health.py`） |
| 检测未知 agent 格式 | `format-detector.py` |
| 分析后采纳规则写入 CLAUDE.md | `/apply` |
| 建立搜索索引、加速查询 | `/index` |
| 导入外部对话（微信/JSON/CSV/文本） | `/import` |
| 选主题后提取上下文包路由到 taxue-* 技能 | `/topic-scan --topic <编号>` |
| 从会话中提炼持久知识 | `/extract` |
| 交互式清理过期记忆 | `/prune` |
| 群聊参与者画像提取 | `/profiles` |
| 全链路回溯：主题扫描 + 经验提炼 | `/digest` |
| 修复/恢复会话 | `jsonl-core` + `/recall` |
| 技能使用洞察、哪些技能闲置 | `skill-insight` |
| 环境自检、配置检查、跨环境冲突、环境健康诊断 | `env-doctor`（读 capabilities.json 调度原生命令 + 脚本） |
| 环境基础设施巡检、网络连通性、skill 漂移检测 | `env-doctor` |
| 调用各环境原生诊断命令、结构化输出到索引 | `native-diag`（`scripts/native-diag.py --env <claude|codex|grok|kimi|mimo|all>`） |
| 微信聊天记录识别分析（已解密库全史检索/群画像/商机跟进/噪音群治理/成文/语音导出/图片还原） | `wechat-digger` 子技能（`skills/wechat-digger`，用户自备已解密库） |
