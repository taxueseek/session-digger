# session-digger

> 跨环境会话历史挖掘。把散落在各处的对话变成可搜索的知识资产。

支持 **Claude Code / Grok Build / Kimi Code / ZCode / DIM / Reasonix** 等多环境会话，以及外部对话导入。零依赖，纯 Python 3.6+ stdlib，clone 即用。

---

## 能做什么

- **跨环境搜索** — 在 Claude Code、Grok、Kimi、ZCode 等多环境中同时搜索历史决策、错误、话题
- **导入外部对话** — 微信 JSON/CSV 导出、会议记录、纯文本聊天记录，统一索引
- **极速检索** — SQLite FTS5 索引 + mtime 增量缓存，关键词搜索 <50ms
- **知识存证** — 分析过的会话自动缓存，重复查询 token 消耗降 90%+
- **记忆时效分层** — 永久/周期/一次性三级过期，旧偏好不会污染当前判断
- **模式分析** — 检测重试循环、错误模式，生成候选规则写入 CLAUDE.md
- **群聊画像** — 增量提取参与者画像（金句、活跃时段、兴趣领域），append-only 更新不丢失历史信号
- **自动记忆沉淀** — `remember.py` 从 SQLite 索引自动生成 `memory/*.md`（全局统计、环境习惯、技能使用概况、错误模式），索引数据零成本落地为持久记忆

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

# 自动生成记忆文件（基于已有的 SQLite 索引）
python3 scripts/remember.py
# 预览模式，不写入文件
python3 scripts/remember.py --dry-run
# 只生成技能使用统计
python3 scripts/remember.py --only skill
```

安装为 Claude Code 插件后，直接使用斜杠命令：`/recall` `/recap` `/import` `/topics` `/analyze` `/dashboard` `/trend` `/optimize` `/profiles` 等。

---

## 支持的环境

| 环境 | 状态 |
|------|------|
| Claude Code | ✅ 原生 |
| Grok Build | ✅ |
| Kimi Code | ✅ |
| ZCode | ✅ |
| DIM (小米) | ✅ |
| Reasonix | ✅ |
| 微信 / 任意聊天记录 | ✅ 导入后索引 |

SchemaProbe 自动适配未知 JSONL 格式——新环境无需写适配器。

---

## 与其他技能的协作

session-digger 只负责解析和路由，不做分析本身。它通过 `combo_map.json` 定义了一套连招映射，与周边技能形成三层协作：

**数据层** — `jsonl-core` 提供规范的 JSONL 解析基础设施，`experience-synthesis` 在其上做经验提炼，`git-mining` 将会话与 git 提交交叉关联。三者共享同一套 schema 和 FTS 索引，互不重复解析。

**路由层** — 每个命令执行完毕后自动提示下一步。`/analyze` 完成后提示 `/apply` 采纳候选规则；`/apply` 写入后提示 `/audit` 验证记忆健康度；`/topics` 切分话题后提示用 `/recall` 深入具体关键词。

**语义层** — `topic_classify.py` 将会话自动归类为投资分析、内容创作、技能开发等 9 大主题，每个主题路由到对应的 `taxue-*` 技能做深度分析。`skill-insight` 在此层之上提供技能使用洞察——哪些技能高频使用、哪些闲置未用，基于索引数据而非猜测。

一次搜索，从「找到对话」到「理解对话」到「用对话中的知识做事」形成闭环。

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

## 最近更新（v0.9.0）

### 自动记忆沉淀

新增 `scripts/remember.py`，从 SQLite 索引自动写入 `memory/*.md` 文件，索引数据直接沉淀为持久记忆：

- `memory/auto-stats.md` — 全局统计（会话数、消息数、工具调用、错误率）
- `memory/auto-env-habits.md` — 各环境使用习惯（会话数、错误率、平均消息量）
- `memory/auto-skill-usage.md` — 高频工具 Top10 + 闲置技能清单
- `memory/auto-errors.md` — 跨环境错误模式

执行 `python3 scripts/remember.py` 即可一次性生成上述文件，后续 `/recall` 可直接检索。

### 技能使用洞察（skill-insight）

新增 `skill-insight` 技能，复用已有 SQLite 索引做技能使用分析——直接定位闲置技能、高频领域工具、环境习惯。触发方式：在对话中说「技能使用分析」「哪些技能没用过」「技能清理建议」。

### 内部重构

- **zcode-adapter.py 瘦身 95%** — 查询逻辑移入 `echolib.py` 的 `zcode_db_*` 函数，zcode-adapter.py 降级为 CLI 包装层，消除子进程调用的开销和出错面
- **topic_classify.py 去 subprocess** — ZCode 数据获取不再经过子进程管道，改为直接调用 echolib
- **echolib.py 共享 helper 提取** — `_iter_jsonl()` / `_strip_system_reminder()` / `_extract_content_text()` / `_match_call_results()` 四项共享函数，消除 30+ 处的重复解析模式
- **测试基线** — `tests/test_baseline.py` 覆盖 echolib 导入、ADAPTER_REGISTRY 完整性、dispatch 函数、scan_environments 等关键调用

---

## License

ISC
