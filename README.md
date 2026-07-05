# session-digger

> 跨环境会话历史挖掘。把散落在各处的对话变成可搜索的知识资产。

支持 **Claude Code / Grok Build / Kimi Code** 三环境会话，以及微信等外部对话导入。零依赖，纯 Python 3.6+ stdlib，clone 即用。

---

## 能做什么

- **跨环境搜索** — 在 Claude Code、Grok、Kimi 中同时搜索历史决策、错误、话题
- **导入外部对话** — 微信 JSON/CSV 导出、会议记录、纯文本聊天记录，统一索引
- **极速检索** — SQLite FTS5 索引 + mtime 增量缓存，关键词搜索 <50ms
- **知识存证** — 分析过的会话自动缓存，重复查询 token 消耗降 90%+
- **记忆时效分层** — 永久/周期/一次性三级过期，旧偏好不会污染当前判断
- **模式分析** — 检测重试循环、错误模式，生成候选规则写入 CLAUDE.md
- **群聊画像** — 增量提取参与者画像（金句、活跃时段、兴趣领域），append-only 更新不丢失历史信号

---

## 快速开始

```bash
git clone https://github.com/taxueseek/session-digger.git
cd session-digger

# 查看所有会话
python3 scripts/sd-recall.py sessions --scope all --limit 20

# 搜索（零 API 调用）
scripts/recall-lite.sh "认证 bug"

# 导入微信对话
python3 scripts/dialog-adapter.py ~/Downloads/wechat-export.json --format wechat
```

安装为 Claude Code 插件后，直接使用斜杠命令：`/recall` `/recap` `/import` `/topics` `/analyze` `/dashboard` 等。

---

## 支持的环境

| 环境 | 状态 |
|------|------|
| Claude Code | ✅ 原生 |
| Grok Build | ✅ |
| Kimi Code | ✅ |
| 微信 / 任意聊天记录 | ✅ 导入后索引 |

SchemaProbe 自动适配未知 JSONL 格式——新环境无需写适配器。

---

## 与其他技能的协作

session-digger 只负责解析和路由，不做分析本身。它通过 `combo_map.json` 定义了一套连招映射，与周边技能形成三层协作：

**数据层** — `jsonl-core` 提供规范的 JSONL 解析基础设施，`experience-synthesis` 在其上做经验提炼，`git-mining` 将会话与 git 提交交叉关联。三者共享同一套 schema 和 FTS 索引，互不重复解析。

**路由层** — 每个命令执行完毕后自动提示下一步。`/analyze` 完成后提示 `/apply` 采纳候选规则；`/apply` 写入后提示 `/audit` 验证记忆健康度；`/topics` 切分话题后提示用 `/recall` 深入具体关键词。

**语义层** — `topic_classify.py` 将会话自动归类为投资分析、内容创作、技能开发等 9 大主题，每个主题路由到对应的 `taxue-*` 技能做深度分析。一次搜索，从「找到对话」到「理解对话」到「用对话中的知识做事」形成闭环。

---

## 安装

**作为插件：**
```
/plugin install session-digger
```

**作为独立工具：**
```bash
git clone https://github.com/taxueseek/session-digger.git
```

---

## License

ISC
