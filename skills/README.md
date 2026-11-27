# session-digger 子技能

精炼索引（5 个 MECE 核心专精层 + `wechat-digger` 外部数据源）。主 skill 只路由；此处只放专精。共享路径：`common_paths.py`。

| 子技能 | 层 | 入口 |
|--------|----|------|
| jsonl-core | L0–L1 | 多格式解析（JSONL / zstd / SQLite / Antigravity）/ 恢复 |
| deep-analysis | L2 | 全环境错误根因 + 意图分类（复用 `echolib` + `index.db`） |
| experience-synthesis | L3 | 教训与决策提炼分类法 |
| memory-management | L3 | 跨环境规则 / 记忆生命周期 |
| skill-insight | L3 | 跨宿主技能用量（含 DimCode 原生事件）+ 资产自检 |
| wechat-digger | 外部数据 | 微信已解密库识别分析（发布版不含密钥/解密栈，见其 `SYNC.md`） |

旁路参考：`../references/git-mining.md`（git ↔ 会话交叉分析，由 `/timeline` 与 `file-historian` 按需加载）。  
编排：`../combo_map.json`。
