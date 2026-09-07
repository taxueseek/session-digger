# env-radar 架构文档

> 内部环境雷达 v1.0 — 整体架构设计

> **状态标注（2026-07-31）**：本文档为**设计稿**，以下内容尚未实现、与当前代码不符：
>
> - 三层缓存 / 增量更新（mtime + content_hash + 30% 阈值）——当前为幂等全量重建
> - 安全漏洞审计（npm audit / pip-audit）、废弃依赖、重复依赖检测
> - 7 脚本拆分（radar-scan/report/structure/convention/health/evolution/init-db）
>
> 当前实现：`radar-build.py`（入口）+ `radar_common` / `radar_structure` /
> `radar_convention` / `radar_health` / `radar_evolution`（采集），
> `radar-query.py`（入口）+ `radar_commands` / `radar_report`（查询）。详见 SKILL.md。

## 1. 系统定位

env-radar 是 session-digger 的子技能，负责**工作区/项目的静态扫描与结构理解**。

它与现有子技能的分工：

| 技能 | 职责 | 数据源 |
|------|------|--------|
| env-radar | 项目结构 + 约定 + 健康 + 演化 | 文件系统 + 包配置 + git |
| env-doctor | AI 编码环境诊断 | 环境原生命令 |
| native-diag | 各环境原生诊断 | codex/grok/kimi CLI |
| git-mining | Git 历史挖掘 | git log/blame/diff |
| deep-analysis | 会话错误根因 | JSONL 会话 |
| skill-insight | 技能使用分析 | 会话索引 |
| experience-synthesis | 经验提炼 | 多源 |

**关键区别**：env-radar 聚焦**项目本身**（代码、依赖、结构），env-doctor 聚焦**环境**（CLI 工具、配置、网络）。

---

## 2. 数据流

```
┌─────────────────────────────────────────────────────────────┐
│                    用户触发 /slash 命令                      │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│              雷达调度器 (radar-scan.py)                       │
│                                                             │
│  1. 检测项目根目录                                          │
│  2. 检查缓存状态 (env-radar.db)                             │
│  3. 决定扫描模式: 全量 / 增量 / 缓存命中                    │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                    4 维度并行采集                           │
│                                                             │
│  ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐  │
│  │ 结构雷达  │ │ 约定雷达  │ │ 健康雷达  │ │ 演化雷达  │  │
│  │           │ │           │ │           │ │           │  │
│  │ 目录树    │ │ 命名采样  │ │ 依赖审计  │ │ 热点文件  │  │
│  │ 模块边界  │ │ 风格统计  │ │ 安全检查  │ │ 变更频率  │  │
│  │ 入口发现  │ │ 架构识别  │ │ 配置对比  │ │ 技术债    │  │
│  │ 依赖图    │ │ 测试模式  │ │ 过期检测  │ │ 贡献分布  │  │
│  └─────┬─────┘ └─────┬─────┘ └─────┬─────┘ └─────┬─────┘  │
│        │             │             │             │         │
│        └─────────────┴──────┬──────┴─────────────┘         │
│                             │                               │
└─────────────────────────────┼───────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                 SQLite 缓存层 (env-radar.db)                 │
│                                                             │
│  projects ←→ files ←→ dependencies                         │
│      ↕           ↕           ↕                              │
│  conventions  health_issues  evolution                     │
│      ↕           ↕           ↕                              │
│  config_items  tech_debt   file_change_frequency           │
│      ↕                        ↕                             │
│  structure_modules      scan_meta                          │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│               报告生成器 (radar-report.py)                   │
│                                                             │
│  1. 查询缓存数据                                            │
│  2. 计算综合评分                                            │
│  3. 生成建议列表                                            │
│  4. 输出报告 (文本 / JSON / Markdown)                       │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                   模型综合推理                               │
│                                                             │
│  - 关联 4 维度发现                                          │
│  - 识别风险模式                                             │
│  - 生成优先建议                                             │
│  - 输出最终报告                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. 模块关系图

```
env-radar/
├── SKILL.md                    # 技能定义（本技能入口）
├── SCHEMA.md                   # 数据库 schema
├── ARCHITECTURE.md             # 本文档
├── agents/
│   └── radar-agent.md          # 子代理定义（模型调度参考）
├── scripts/
│   ├── radar-scan.py           # 全量扫描入口（主脚本）
│   ├── radar-report.py         # 报告生成器
│   ├── radar-structure.sh      # 结构雷达采集
│   ├── radar-convention.py     # 约定雷达采集
│   ├── radar-health.sh         # 健康雷达采集
│   ├── radar-evolution.sh      # 演化雷达采集
│   └── radar-init-db.py        # 数据库初始化
└── references/
    └── report-template.md      # 报告模板参考

缓存数据库位置：
~/.claude/.session-digger/env-radar.db
```

### 脚本职责说明（设计稿）

> 下表中的 7 脚本为设计稿规划；当前实际为 9 个文件：
> `radar-build.py` / `radar-query.py` 两个入口 + `radar_common` / `radar_structure` /
> `radar_convention` / `radar_health` / `radar_evolution` / `radar_commands` / `radar_report`
> 七个模块。功能映射：radar-scan.py → radar-build.py，radar-report.py → radar-query.py，
> 四维采集脚本 → 对应 radar_* 模块。

| 脚本 | 职责 | 输入 | 输出 |
|------|------|------|------|
| `radar-scan.py` | 主调度，协调 4 维度采集，写入 DB | `--project <path>` | DB 写入 + 摘要 JSON |
| `radar-report.py` | 从 DB 查询生成报告 | `--project` + `--format` | 文本/JSON/Markdown |
| `radar-structure.sh` | 目录树 + 模块 + 入口 | 项目路径 | JSON |
| `radar-convention.py` | 命名/风格/架构统计 | 项目路径 | JSON |
| `radar-health.sh` | 依赖/安全/配置检查 | 项目路径 | JSON |
| `radar-evolution.sh` | 热点/技术债/git 统计 | 项目路径 + git | JSON |
| `radar-init-db.py` | 初始化 DB + 创建表 | DB 路径 | 无 |

---

## 4. 与现有子技能的协作

### 4.1 数据流向

```
┌────────────────┐     ┌────────────────┐
│   env-doctor   │     │  native-diag   │
│  (环境诊断)    │     │  (原生诊断)    │
└───────┬────────┘     └───────┬────────┘
        │                      │
        │    环境/配置问题      │
        └──────────┬───────────┘
                   │
                   ▼
        ┌─────────────────────┐
        │     env-radar       │
        │   (项目结构/健康)    │
        └──────────┬──────────┘
                   │
        ┌──────────┼──────────┐
        │          │          │
        ▼          ▼          ▼
┌────────────┐ ┌────────┐ ┌──────────┐
│ git-mining │ │ deep-  │ │ memory-  │
│ (git 历史) │ │analysis│ │management│
└────────────┘ └────────┘ └──────────┘
```

### 4.2 触发协作规则

| env-radar 发现 | 推荐下游 | 触发条件 |
|----------------|----------|----------|
| 环境配置冲突 | env-doctor | 检测到 .env 缺失或 PATH 异常 |
| 会话错误与代码相关 | deep-analysis | 用户在问「这次为啥失败」 |
| 热点文件需深入理解 | git-mining | 用户想知道「这个文件为啥老改」 |
| 技术债需规划 | experience-synthesis | 用户想「清理技术债」 |
| 项目记忆需更新 | memory-management | 扫描发现新模块/重构 |

### 4.3 协作示例

**场景：用户问「这个项目最近怎么了」**

```
1. env-radar → 扫描项目
   - 发现: 热点文件 src/services/user.py 30 次/90天
   - 发现: 技术债 34 个 TODO
   - 发现: 2 个 HIGH 安全漏洞

2. git-mining → 深入热点文件
   - 追溯 user.py 的修改历史
   - 发现: 3 次重构 + 5 次紧急修复

3. env-doctor → 检查环境
   - 发现: npm 版本不一致导致构建失败

4. 模型综合 → 生成报告
   - 「user.py 是核心瓶颈，建议拆分」
   - 「安全漏洞需立即修复」
   - 「环境不一致导致构建不稳定」
```

---

## 5. 缓存架构

### 5.1 双层缓存

```
┌─────────────────────────────────────────────┐
│             L1: 内存缓存 (可选)             │
│  - 当次会话内的重复查询                      │
│  - 无需持久化                                │
└─────────────────────┬───────────────────────┘
                      │ 未命中
                      ▼
┌─────────────────────────────────────────────┐
│           L2: SQLite 持久缓存               │
│  - env-radar.db                             │
│  - 跨会话持久化                              │
│  - 基于 mtime + hash 增量更新                │
└─────────────────────┬───────────────────────┘
                      │ 未命中/过期
                      ▼
┌─────────────────────────────────────────────┐
│           L3: 文件系统（真源）              │
│  - 实际项目文件                              │
│  - 包配置文件 / lock 文件                    │
│  - git 历史                                  │
└─────────────────────────────────────────────┘
```

### 5.2 增量更新策略

```python
# 伪代码：增量更新逻辑
def scan_project(project_path, db):
    cached = db.get_project(project_path)
    current = collect_file_fingerprints(project_path)

    if not cached:
        return full_scan(project_path, db)

    changed = []
    for path, (mtime, hash) in current.items():
        if path not in cached.files:
            changed.append(('ADD', path))
        elif cached.files[path].hash != hash:
            changed.append(('MODIFY', path))

    for path in cached.files:
        if path not in current:
            changed.append(('DELETE', path))

    if len(changed) == 0:
        return CacheHit(cached)

    if len(changed) / len(current) > 0.3:
        # 变更超过 30%，全量重建
        return full_scan(project_path, db)

    # 增量更新
    return incremental_scan(project_path, db, changed)
```

### 5.3 缓存过期规则

| 维度 | 过期时间 | 原因 |
|------|----------|------|
| 结构 | 24 小时 | 结构相对稳定 |
| 约定 | 7 天 | 约定变化缓慢 |
| 健康 | 1 小时 | 依赖可能随时更新 |
| 演化 | 6 小时 | git 持续有提交 |

---

## 6. 多语言兼容

### 6.1 语言检测逻辑

```python
LANGUAGE_SIGNATURES = {
    'python': {
        'config_files': ['pyproject.toml', 'setup.py', 'setup.cfg', 'requirements.txt', 'Pipfile'],
        'lock_files': ['poetry.lock', 'Pipfile.lock'],
        'entry_patterns': ['main.py', '__main__.py', 'manage.py', 'app.py', 'wsgi.py'],
        'test_frameworks': ['pytest', 'unittest', 'nose'],
        'extensions': ['.py', '.pyx'],
    },
    'typescript': {
        'config_files': ['package.json', 'tsconfig.json'],
        'lock_files': ['package-lock.json', 'yarn.lock', 'pnpm-lock.yaml'],
        'entry_patterns': ['index.ts', 'main.ts', 'app.ts', 'server.ts'],
        'test_frameworks': ['jest', 'vitest', 'mocha', 'ava'],
        'extensions': ['.ts', '.tsx'],
    },
    'javascript': {
        'config_files': ['package.json'],
        'lock_files': ['package-lock.json', 'yarn.lock', 'pnpm-lock.yaml'],
        'entry_patterns': ['index.js', 'main.js', 'app.js', 'server.js'],
        'test_frameworks': ['jest', 'vitest', 'mocha'],
        'extensions': ['.js', '.jsx', '.mjs', '.cjs'],
    },
    'go': {
        'config_files': ['go.mod'],
        'lock_files': ['go.sum'],
        'entry_patterns': ['main.go'],
        'test_frameworks': ['testing'],
        'extensions': ['.go'],
    },
    'rust': {
        'config_files': ['Cargo.toml'],
        'lock_files': ['Cargo.lock'],
        'entry_patterns': ['main.rs', 'lib.rs'],
        'test_frameworks': ['cargo-test'],
        'extensions': ['.rs'],
    },
    'java': {
        'config_files': ['pom.xml', 'build.gradle', 'build.gradle.kts'],
        'lock_files': [],
        'entry_patterns': ['Main.java', 'Application.java'],
        'test_frameworks': ['junit', 'testng'],
        'extensions': ['.java'],
    },
}
```

### 6.2 降级策略

当语言无法识别时：
1. 使用通用文件类型分类（source / test / config / doc / build）
2. 跳过语言特定约定检测
3. 保留结构和演化维度（与语言无关）

---

## 7. 扩展性设计

### 7.1 新增维度

如果需要添加第 5 个维度（如「安全雷达」独立）：

1. 新建采集脚本 `radar-security.sh`
2. 在 `SCHEMA.md` 新增表 `security_*`
3. 在 `radar-scan.py` 添加调度分支
4. 在 `radar-report.py` 添加报告段落
5. 在 `SKILL.md` 更新维度说明

### 7.2 新增语言

如果需要支持新语言（如 Kotlin）：

1. 在 `LANGUAGE_SIGNATURES` 添加 Kotlin 配置
2. 在 `radar-convention.py` 添加 Kotlin 风格检测
3. 无需修改 schema（language 字段已预留）

### 7.3 插件式采集器

```python
# 采集器接口
class BaseCollector:
    name: str
    dimension: str

    def collect(self, project_path: str) -> dict:
        raise NotImplementedError

    def write(self, db: Database, project_id: str, data: dict):
        raise NotImplementedError

# 注册新采集器
COLLECTORS = {
    'structure': StructureCollector(),
    'convention': ConventionCollector(),
    'health': HealthCollector(),
    'evolution': EvolutionCollector(),
}
```

---

## 8. 性能目标

| 指标 | 目标 | 说明 |
|------|------|------|
| 首次扫描 | < 30 秒 | 1000 文件项目 |
| 增量扫描 | < 5 秒 | 变更 < 30% |
| 缓存命中 | < 500ms | 直接读 DB |
| 报告生成 | < 2 秒 | 从 DB 查询 |
| DB 大小 | < 10MB | 1000 文件项目 |

---

## 9. 隐私与安全

### 9.1 不采集

- `.env` 文件中的**值**（只采集 key 名）
- `node_modules/` / `vendor/` 等第三方代码
- `.git/` 目录内容
- 二进制文件内容

### 9.2 脱敏规则

| 数据类型 | 处理方式 |
|----------|----------|
| API Key / Secret | 只存 key 名，不存 value |
| 数据库连接串 | 只存 host/port，不存密码 |
| 邮箱/手机号 | 不采集 |
| 绝对路径 | 转为相对路径 |

### 9.3 存储位置

- 缓存 DB：`~/.claude/.session-digger/env-radar.db`
- 与 session-digger 共享存储目录
- 不上传、不跨机器同步

---

## 10. 错误处理

### 10.1 降级策略

| 故障场景 | 降级行为 |
|----------|----------|
| 非 git 仓库 | 跳过演化雷达，输出 3 维度 |
| 无 lock 文件 | 依赖分析使用 package.json 声明 |
| npm audit 失败 | 跳过安全漏洞检查 |
| pip-audit 未安装 | 降级为 pip list --outdated |
| 项目过大 (>10000 文件) | 采样扫描，标注置信度 |
| 数据库损坏 | 删除重建，全量扫描 |

### 10.2 错误码

| 码 | 含义 | 处理 |
|----|------|------|
| E001 | 项目路径不存在 | 提示用户 |
| E002 | 无权限读取文件 | 跳过该文件 |
| E003 | 数据库写入失败 | 内存模式运行 |
| E004 | 采集脚本超时 | 跳过该维度 |
| E005 | 语言不支持 | 通用降级 |

---

## 11. 与 session-digger 的集成点

### 11.1 共享基础设施

| 设施 | 位置 | 用途 |
|------|------|------|
| SQLite 数据库 | `~/.claude/.session-digger/` | 缓存存储 |
| Python 运行时 | 系统 Python 3.10+ | 脚本执行 |
| bash | 系统 bash | 采集脚本 |
| git | 系统 git | 演化雷达 |

### 11.2 共享约定

- 数据库 schema 风格一致（`index_meta` 表、`content_hash` 字段）
- 增量更新逻辑一致（mtime + size + hash）
- 脚本路径解析一致（`SD_ROOT` preamble）
- 报告格式一致（严重程度分级）

### 11.3 数据隔离

env-radar 使用独立的数据库文件 `env-radar.db`，不与 session-digger 的 `index.db` 混合，避免：
- 表名冲突
- 写入锁竞争
- 版本升级耦合

---

## 12. 未来演进方向

### Phase 2（计划中）

- **依赖图可视化** — 生成 Mermaid 依赖图
- **变更影响分析** — 修改某文件影响哪些模块
- **技术债趋势** — 追踪技术债增减趋势
- **多项目对比** — 同时扫描多个项目横向对比

### Phase 3（远期）

- **CI 集成** — 在 CI 中运行，生成 PR 影响报告
- **自动修复** — 对低风险问题自动提交修复 PR
- **团队画像** — 基于 git 统计团队协作模式
