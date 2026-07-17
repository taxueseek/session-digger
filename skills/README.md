# session-digger 子技能

精炼索引。主 skill 只路由；此处只放专精。共享路径：`common_paths.py`。

| 子技能 | 层 | 入口 |
|--------|----|------|
| jsonl-core | L0–L1 | 解析 / 恢复 |
| deep-analysis | L2 | 错误根因 + 意图 |
| experience-synthesis | L3 | 教训提炼 |
| git-mining | 旁路 | git ↔ 会话 |
| memory-management | L3 | 记忆生命周期 |
| skill-insight | L3 | 技能用量 + 自检 |
| env-doctor | 运维 | 综合诊断 |
| native-diag | 运维 | 原生 CLI 采集 |

编排：`../combo_map.json`（v0.9.18+ 含 `subskill_protocol` 与 deep-analysis 挂载）。  
回档：`archive/pre-subskill-tune-20260717`。
