---
name: env-radar
description: |
  内部环境雷达 v1.0。扫描当前项目/工作区的代码结构、依赖、配置、约定、模式，
  让 AI 代理在 30 秒内「理解这个项目的全貌」。
  四维探测：结构雷达、约定雷达、健康雷达、演化雷达。
  触发：env-radar、环境雷达、项目扫描、项目体检、代码扫描、
  项目全貌、了解项目、熟悉代码、快速上手、项目概览、
  project scan、codebase scan、onboard、understand this project
version: 1.0.0
---

# env-radar

> 四维扫描，30 秒看懂项目全貌。

## 核心原则

1. **只读探测** — 不修改任何项目文件，只扫描和分析
2. **幂等重建** — 每次构建清空旧数据后全量写入 SQLite，重复扫描结果一致
3. **模型是大脑** — 脚本只采集原始数据，综合判断由模型完成
4. **跨语言兼容** — 支持 Node.js / Python / Go / Rust / Java / 通用项目

## 四维雷达

### 1. 结构雷达（Structure）

**目标**：回答「这个项目长什么样」

| 探测项 | 数据源 | 输出 |
|--------|--------|------|
| 目录树 | `find` / `tree` | 层级结构 + 深度统计 |
| 模块边界 | 包配置文件（package.json / pyproject.toml / go.mod / Cargo.toml） | 模块列表 + 入口文件 |
| 依赖图 | lock 文件（package-lock / poetry.lock / go.sum） | 直接依赖 + 传递依赖数量 |
| 入口文件 | main / index / app / cmd | 启动链路 |
| 配置文件 | .env / config.* / settings.* | 配置项清单 |

**关键命令**：
```bash
# 目录树（排除 node_modules / .git / __pycache__）
find . -type f -not -path '*/node_modules/*' -not -path '*/.git/*' -not -path '*/__pycache__/*' | head -200

# 入口发现
find . -maxdepth 3 \( -name "main.*" -o -name "index.*" -o -name "app.*" -o -name "cmd" -type d \) -not -path '*/node_modules/*'

# 包配置
cat package.json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps({k:d.get(k) for k in ['name','version','main','scripts','dependencies','devDependencies']}, indent=2))"
```

### 2. 约定雷达（Convention）

**目标**：回答「这个项目遵循什么规则」

| 探测项 | 方法 | 输出 |
|--------|------|------|
| 命名规范 | 正则采样（文件/变量/类/函数） | camelCase / snake_case / PascalCase 分布 |
| 代码风格 | 缩进/引号/分号统计 | 2-space / 4-space / tab；单引/双引 |
| 架构模式 | 目录结构 + import 分析 | MVC / 分层 / 微服务 / 插件 |
| 错误处理 | try/catch 密度 + 错误类型 | 错误处理覆盖率 |
| 测试模式 | test 文件分布 + 框架检测 | jest / pytest / go test |

**关键命令**：
```bash
# 命名规范采样
find . -type f \( -name "*.js" -o -name "*.ts" -o -name "*.py" \) -not -path '*/node_modules/*' | head -50 | xargs grep -hE '^(function|def|class|const|let|var|interface|type)' 2>/dev/null | head -100

# 缩进风格
find . -type f \( -name "*.js" -o -name "*.ts" -o -name "*.py" \) -not -path '*/node_modules/*' | head -30 | xargs awk '/^ /{spaces++} /^	/{tabs++} /^  /{two++} END{print "tab:",tabs,"2space:",two,"4space:",spaces}' 2>/dev/null

# 测试框架检测
grep -E '"(jest|vitest|mocha|pytest|unittest|go-test)"' package.json pyproject.toml go.mod 2>/dev/null
```

### 3. 健康雷达（Health）

**目标**：回答「这个项目健不健康」

| 探测项 | 数据源 | 输出 |
|--------|--------|------|
| 依赖版本 | 依赖文件 + 包注册表 API（仅前 20 个） | 过期包 / 最新版本差距 |
| 缺失配置 | .env.example vs .env、CI 配置文件 | 缺失项清单 |

> 注：安全漏洞审计（npm audit / pip-audit）、废弃依赖、重复依赖检测为设计目标，尚未实现。

**关键命令**：
```bash
# Node.js 审计
npm audit --json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d.get('metadata',{}),indent=2))"

# Python 审计（需 pip-audit）
pip-audit --format json 2>/dev/null || pip list --outdated --format=json 2>/dev/null

# 过期包
npm outdated --json 2>/dev/null

# 配置对比
diff <(grep -v '^#' .env.example 2>/dev/null | grep '=' | cut -d= -f1 | sort) <(grep -v '^#' .env 2>/dev/null | grep '=' | cut -d= -f1 | sort)
```

### 4. 演化雷达（Evolution）

**目标**：回答「这个项目在往哪走」

| 探测项 | 数据源 | 输出 |
|--------|--------|------|
| 热点文件 | git log + 提交频率 | 修改最频繁的 Top 10 文件 |
| 频繁修改区 | git log --name-only | 热点目录 |
| 技术债集中区 | TODO/FIXME/HACK 注释 | 债务密度分布 |
| 贡献者分布 | git shortlog | 提交者集中度 |
| 变更趋势 | git log --since | 近期活跃度 |

**关键命令**：
```bash
# 热点文件 Top 15
git log --name-only --pretty=format: --since="90 days ago" 2>/dev/null | grep -v '^$' | sort | uniq -c | sort -rn | head -15

# 热点目录
git log --pretty=format: --name-only --since="90 days ago" 2>/dev/null | grep -v '^$' | xargs -I{} dirname {} | sort | uniq -c | sort -rn | head -10

# 技术债密度
grep -rEn '(TODO|FIXME|HACK|XXX|WORKAROUND)' --include='*.js' --include='*.ts' --include='*.py' --include='*.go' . 2>/dev/null | grep -v node_modules | wc -l

# 贡献者集中度
git shortlog -sn --since="90 days ago" 2>/dev/null | head -10

# 近期活跃度
git log --oneline --since="30 days ago" 2>/dev/null | wc -l
```

## 调度策略（模型决策参考）

```
用户请求扫描
    │
    ▼
检测项目根目录（package.json / pyproject.toml / go.mod / .git）
    │
    ▼
radar-build.py 幂等重建（清空旧数据 → 4 维度采集 → 写入 SQLite）
    │
    ▼
radar-query.py 读取数据库 → 模型综合推理 → 输出报告
```

## 工具/命令清单

| 工具/命令 | 用途 | 依赖 |
|-----------|------|------|
| `radar-build.py` | 扫描入口，调度 4 维度采集并写入 DB | Python 3.10+ |
| `radar_common.py` | 公共模块：常量、工具函数、DB 初始化 | Python 3.10+ |
| `radar_structure.py` | 结构雷达采集 | Python 3.10+ |
| `radar_convention.py` | 约定雷达采集 | Python 3.10+ |
| `radar_health.py` | 健康雷达采集 | Python 3.10+ |
| `radar_evolution.py` | 演化雷达采集 | Python 3.10+ |
| `radar-query.py` | 报告查询入口 | Python 3.10+ |
| `radar_commands.py` | 查询命令实现（8 个命令） | Python 3.10+ |
| `radar_report.py` | 报告渲染（markdown/json/html） | Python 3.10+ |
| `sqlite3` | 缓存查询 | 系统自带 |

## 调用示例

### 全量扫描

```bash
# 扫描项目，写入 SQLite 数据库
python3 scripts/radar-build.py . --output ~/.claude/.session-digger/env-radar.db

# 从数据库生成总览报告
python3 scripts/radar-query.py ~/.claude/.session-digger/env-radar.db overview
```

### 单维度报告

```bash
# 只看结构（目录树）
python3 scripts/radar-query.py <db> structure

# 只看约定
python3 scripts/radar-query.py <db> conventions

# 只看健康
python3 scripts/radar-query.py <db> health

# 只看演化（需 git 仓库）
python3 scripts/radar-query.py <db> evolution
```

### 针对性查询

```bash
# 只看热点文件 Top 20
python3 scripts/radar-query.py <db> evolution --top 20

# 只看过期依赖（健康报告内）
python3 scripts/radar-query.py <db> health

# 全文搜索
python3 scripts/radar-query.py <db> search --query fastapi --type all

# 两次扫描对比
python3 scripts/radar-query.py <db1> diff --other-db <db2>

# 导出完整报告（JSON / HTML）
python3 scripts/radar-query.py <db> export --format json
```

## 输出格式

### 标准报告

```
=== env-radar 项目雷达 ===
项目: <project_name>
路径: <project_path>
扫描时间: <timestamp>
扫描模式: 全量重建（幂等）

━━━ 结构雷达 ━━━
语言: Python (主) + TypeScript (辅)
模块数: 12
入口: src/main.py → src/app.py
依赖: 43 直接 / 187 传递
目录深度: 最大 6 层，平均 3.2 层

━━━ 约定雷达 ━━━
命名: snake_case (78%) / camelCase (15%) / PascalCase (7%)
缩进: 4-space (92%)
引号: 单引号 (85%)
架构: 分层架构 (API → Service → Repository)
测试: pytest，覆盖率约 60%
错误处理: try/catch 覆盖率 45%

━━━ 健康雷达 ━━━
过期包: 12 个（其中 3 个重大版本落后）
安全漏洞: 2 个 HIGH / 5 个 MEDIUM
废弃依赖: 1 个（2 年未更新）
配置完整度: .env 覆盖 .env.example 的 8/10

━━━ 演化雷达 ━━━
热点文件:
  1. src/services/user.py (28 次/90天)
  2. src/models/order.py (24 次/90天)
  3. tests/test_user.py (19 次/90天)
热点目录: src/services/ (40%) > src/models/ (25%)
技术债: 34 个 TODO / 8 个 FIXME
贡献者: 3 人（A: 65%, B: 25%, C: 10%）
近期活跃度: 47 次提交/30天

━━━ 综合评估 ━━━
健康度: 72/100
  ✓ 结构清晰，模块边界明确
  ✓ 约定统一，风格一致
  ⚠ 依赖过期较多，建议升级
  ⚠ 热点文件集中，存在耦合风险
  ✗ 2 个 HIGH 安全漏洞需立即处理

建议:
1. [HIGH] 修复安全漏洞: CVE-2024-xxxxx (lodash)
2. [MED] 升级过期包: fastapi 0.68 → 0.110
3. [LOW] 拆分热点文件: src/services/user.py
```

## 缓存策略

当前实现为**幂等全量重建**：每次 `radar-build.py` 运行都会清空旧数据后重新扫描写入，
重复运行结果一致。增量扫描（基于 mtime + content_hash 的部分更新）为设计目标，尚未实现。

## 严重程度

| 级别 | 含义 | 示例 |
|------|------|------|
| CRITICAL | 需立即处理 | 安全漏洞 HIGH、构建失败 |
| WARNING | 建议处理 | 依赖过期、热点耦合 |
| INFO | 提示信息 | 命名不统一、测试覆盖低 |
| PASS | 正常 | 符合最佳实践 |

## Path resolution

脚本为同一目录下的 Python 模块，入口脚本（`radar-build.py` / `radar-query.py`）
直接 import 同目录模块，无需额外路径配置。

## 下游协作

| 发现 | 推荐 |
|------|------|
| 环境配置问题 | → env-doctor |
| 会话错误与代码相关 | → deep-analysis |
| 热点文件需深入理解 | → git-mining |
| 技术债需规划 | → experience-synthesis |
| 项目记忆需更新 | → memory-management |

## DO NOT

- 不修改项目文件 — env-radar 是只读探测
- 不执行未知脚本 — 只运行 skill 自带的采集脚本
- 不暴露敏感信息 — .env 中的密钥只显示 key 名，不显示 value
- 不替代人工判断 — 脚本只输出数据，综合评估由模型完成
- 不在扫描时运行测试或构建 — 避免副作用
