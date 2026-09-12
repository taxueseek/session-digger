# 三系上游精华吸纳图（v0.0.4）

| 上游系 | 代表 | 吸纳什么 | digger 落点 | 不吸纳 |
|--------|------|----------|-------------|--------|
| **取数 CLI** | wx-cli 0.3 | 命令面、daemon 探测、`--json` | `wx_bridge` + `CAPABILITY_MATRIX` | 不 vendor 二进制、不重写扫 key |
| **解密库** | wechat-decrypt / vault | 离线全量/增量解密 | `scripts/acquire/*`（已有） | 不并行第二套密钥方案 |
| **分析产品** | welink / ChatLab | 排行、词云原料、时段热力、总览报告 | `ANALYSIS_MODES.lab` + `wd report --flavor lab` | 不装 Docker GUI/MCP 全栈 |
| **垂直 Skill** | she-love-me 类 | 双人互动比、回应间隔、关系报告 | `ANALYSIS_MODES.dyad` + `wd report --flavor dyad` | 不拷恋爱剧本与外呼 |

## 一行扩展

- 新分析块：在 `analyze.py` 实现函数 → `ANALYSIS_MODES` 某 mode 加块名  
- 新报告章节：改 `report.render_lab_report`  
- 新取数 op：`WX_SURFACE` / `CAPABILITY_MATRIX` 一行  

## 综合效果目标

单一入口 `wd`：离线 vault + 可选 wx + 实验室报告 + 关系报告 + 群聊精华，**强于单系工具**，而不合并它们的运维负担。
