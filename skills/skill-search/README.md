# Skill Search

> 先搜索，再创造。把提示词、SOP、对话记录和现有技能，做成经过调研、评测、可安装、可发布的 Agent Skill 包。

Skill Search 是一个 meta-skill：它不直接回答领域问题，而是把「调研 → 设计 → 实现 → 评测 → 发布」整套方法固化下来，产出可复用的 Agent Skill 包。核心差异化在 **内置垂直搜索源 + 意图化查询**：面向 skill 生态做 prior-art 调研，而不是对通用 Web 做全量搜索。

## 为什么值得用

- **内置垂直搜索源**：GitHub 官方 API、skills.sh、SkillsMP、ClawHub、官方目录与包管理器，外加本机清单/历史用量两条本地引擎，全部内建在脚本里。不依赖通用搜索引擎，也不要求引入外部全量引擎。
- **先搜索，再创造**：动手前先跑 prior-art 调研，识别已有最佳实践，明确 keep / adapt / reject / invent，避免重复造轮子。
- **证据驱动质量**：门禁化流程——触发评测、输出契约、Skill IR、发布就绪检查，所有公开声明必须有对应证据，缺证据就标 `missing evidence`。
- **自包含发布**：内置发布器走 feature branch → 校验 → PR → 合并 → Release → 干净安装验证，禁止直推默认分支。
- **与本地索引互补**：作为 session-digger 的子技能，`dig.py --prior-art` 会把外部生态视野接进本地会话索引查询，本地找历史、外部找生态。

## 搜索核心

Prior-art 调研是本 Skill 的差异化环节，四类外部垂直源 + 两条本地引擎各司其职：

| 源 | 最佳用途 | 指标含义 | 注意 |
|---|---|---|---|
| GitHub 官方 API | 规范仓库覆盖、按 stars/updated 排序、topic 过滤 | 仓库原生元数据 | 仓库流行度 ≠ Skill 质量 |
| skills.sh | 流行度锚点与安装发现 | installs 是生态采纳遥测 | 采纳 ≠ 满意度或正确性 |
| SkillsMP | 广覆盖、多语言与职业发现 | `stars` 是仓库 stars | 独立索引，有重复/本地化，总量为近似值 |
| ClawHub | OpenClaw 生态覆盖 | downloads/installs/stars 是遥测 | 生态较窄；可疑条目已打标，采纳前需读源码 |
| 官方目录/包管理器 | 第一方信任锚点（anthropics/skills、npm/pypi skill 包） | 精选或已发布状态 | 范围天然窄 |
| 本地清单 / 本地内容（`--local`） | 已装技能复用与内容信号 | 仅清单命中与内容命中 | 本地建议，不是外部证据 |
| 历史用量（`--digger`） | 历史会话的机会/缺口信号 | 仅结构化计数，原始会话永不外泄 | 是信号不是证据，需人工复核 |

本地引擎是正开关：`--local` 扫已装 SKILL.md 清单（local_meta）并借 local-seek 搜技能根目录正文（local_body）；`--digger` 读 session-digger 的机会/缺口 JSON。两者都只读、不进外部源融合，独立成块输出。

检索方法：

1. **意图化查询增强**：按「产出 / 领域动作 / 质量机制 / 相邻同义词」拆出 2–4 个查询，而不是一个宽泛短语。
2. **跨源融合去重**：按规范 GitHub 仓库 + skill 路径去重，折叠翻译、镜像与明显 fork，不把指标加总。
3. **真源核验**：只读源 `SKILL.md`、维护状态、许可证、权限与安全信号；不执行候选代码。
4. **指标语义学**：install 是采纳遥测、stars 是仓库流行度、官方目录是精选状态——三者都不等于用户评分。

## 安装与自然触发

```bash
npx skills add <user>/skill-search
```

你可以直接这样说：

- 「帮我调研一下做会议纪要的 skill，生态里已有哪些最佳实践？」
- 「体检一下现有 skill，给出改进方案」
- 「把这段 SOP 做成可发布的 skill 包」

## 快速开始（本地开发）

```bash
# 校验本包结构
python3 scripts/validate_skill.py .

# 统一 prior-art 调研（四外部通道并行，可加本地引擎与决策收尾）
python3 scripts/research_prior_art.py "<查询 1>" "<查询 2>" --strict --decide --summary --local --digger --output reports/prior-art-candidates.json
```

## 内置命令

| 命令 | 作用 |
|---|---|
| `scripts/research_prior_art.py` | 统一 prior-art 调研入口（skills.sh + SkillsMP + GitHub + ClawHub 四通道，可加 `--local`/`--digger` 本地引擎，`--decide` 产出 reuse/adapt/build/invent 决策与证据状态） |
| `scripts/search_github.py` | GitHub 官方 search API 通道（topic/sort/limit） |
| `scripts/search_skillsmp.py` | SkillsMP 目录通道 |
| `scripts/search_clawhub.py` | ClawHub 目录通道 |
| `scripts/skill_seek_local.py` | 本地引擎（`--local` 清单/内容扫描 + `--digger` 历史用量），被 research 统一入口调用 |
| `scripts/lint_description.py` | description 质量评分与建议 |
| `scripts/creation_handoff.py` | 创建交接单（含 Prior-art SEEK_* 字段）生成 |
| `scripts/aggregate_benchmark.py` | 评测结果聚合（7:2:1 管线） |
| `scripts/validate_skill.py` | 包结构、身份、版本、README 校验 |
| `scripts/export_skill_ir.py` | 导出 Skill IR |
| `scripts/trigger_eval.py` | 触发边界评测 |
| `scripts/release_check.py` | 发布就绪检查 |
| `scripts/publish_skill.py` | 自包含发布（`--dry-run` 只审计不写） |

## 与 session-digger 的关系

Skill Search 挂载为 session-digger 的子技能：

- `dig.py --prior-art "查询"` 调用本 Skill 的调研能力，把外部生态候选并进本地会话索引结果。
- 本地索引回答「我/我们之前做过什么」，外部调研回答「生态里已有哪些最佳实践」，两者互补。
- 本 Skill 不自带本地会话索引，也不复制 session-digger 的索引逻辑。

## 质量检查

```bash
python3 scripts/validate_skill.py .
python3 scripts/trigger_eval.py . --cases evals/trigger_cases.json --output reports/trigger-eval.json
python3 scripts/release_check.py . --phase local --run-tests
python3 -m pytest tests/ -q
```

## 目录结构

```
skill-search/
├── SKILL.md                  # 路由 + 最小工作流
├── manifest.json             # 包台账
├── agents/interface.yaml     # 接口契约
├── references/               # 判断方法（设计/证据/发布）
├── scripts/                  # 确定性行为（搜索/校验/评测/发布）
├── evals/                    # 回归用例
├── reports/                  # 运行证据（含 reports/history/ 历史沿革）
└── tests/                    # 单元测试
```

## Troubleshooting

| 问题 | 原因 | 解决 |
|---|---|---|
| `npx: command not found` | 未安装 Node.js | `node --version && npx --version` |
| GitHub API 限流 | 匿名配额约 60 次/小时 | 设置 `GITHUB_TOKEN` 提升配额 |
| 单个目录源失败 | 外部服务抖动 | 其余源继续，报告中记录 `missing evidence` |
| 触发评测不命中 | 词表与用例未覆盖 | 运行 `trigger_eval.py` 看未命中项，补 case |
| 校验报 README 缺项 | README 不是产品页 | 按 `references/github-readme-playbook.md` 补结构 |

## 安全边界

- 调研只读，不执行候选代码，不把远程 Skill 装进当前会话。
- 发布前检查公开文件中无密钥、Cookie、私有路径或未经验证的声明。
- 不硬编码个人身份与品牌；公开包保持中性署名。
- 发布仅通过 feature branch + PR，禁止直推默认分支；已发布版本不可复用。

## License

ISC

Copyright (c) 2026 skill-search contributors
