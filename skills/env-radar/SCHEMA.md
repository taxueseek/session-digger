# env-radar Schema

> SQLite 数据库设计（实际实现，与 `radar_common.py` 中 `SCHEMA_SQL` 一致）。
> 默认数据库位置：`<project>/.env-radar/radar.db` 或 `--output` 指定路径。

## 设计原则

1. **单项目模型** — 一个数据库对应一次扫描的项目，无多项目外键
2. **幂等重建** — 每次 `radar-build.py` 运行清空旧数据后全量写入
3. **维度分离** — 结构/约定/健康/演化四个维度独立表
4. **无敏感内容** — 不存密钥值，配置文件只记录 key 名

---

## 表结构

### project_meta — 项目元信息（单行）

```sql
CREATE TABLE project_meta (
    id INTEGER PRIMARY KEY,
    root_path TEXT NOT NULL,
    name TEXT,
    scanned_at REAL,
    total_files INTEGER,
    total_lines INTEGER,
    git_branch TEXT,
    git_remote TEXT
);
```

### files — 文件快照

```sql
CREATE TABLE files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,               -- 绝对路径
    rel_path TEXT NOT NULL,           -- 相对路径
    extension TEXT,                   -- 扩展名（含点，如 .py）
    size_bytes INTEGER,
    lines INTEGER,
    is_entry BOOLEAN DEFAULT 0,       -- 是否入口文件
    module_name TEXT,                 -- 所属模块
    depth INTEGER,                    -- 目录深度
    parent_dir TEXT
);
```

### modules — 模块边界

```sql
CREATE TABLE modules (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    file_count INTEGER,
    main_language TEXT,
    is_leaf BOOLEAN DEFAULT 1
);
```

### dependencies — 依赖信息

```sql
CREATE TABLE dependencies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT,
    latest_version TEXT,
    is_outdated BOOLEAN DEFAULT 0,
    dep_type TEXT,                    -- runtime / dev / peer / optional
    package_manager TEXT,             -- npm / pip / cargo / go
    file_source TEXT
);
```

### conventions — 约定统计

```sql
CREATE TABLE conventions (
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,           -- naming / style / architecture / error_handling / comments / imports / health
    name TEXT NOT NULL,
    value TEXT,
    count INTEGER DEFAULT 0,
    sample_files TEXT                 -- JSON 数组
);
```

### evolution — 热点文件

```sql
CREATE TABLE evolution (
    id INTEGER PRIMARY KEY,
    file_id INTEGER,                  -- 关联 files.id
    commit_count INTEGER DEFAULT 0,
    last_modified TEXT,
    first_seen TEXT,
    author_count INTEGER DEFAULT 0,
    FOREIGN KEY (file_id) REFERENCES files(id)
);
```

### tech_debt — 技术债明细

```sql
CREATE TABLE tech_debt (
    id INTEGER PRIMARY KEY,
    file_id INTEGER,                  -- 关联 files.id
    line_number INTEGER,
    tag TEXT,                         -- TODO / FIXME / HACK / XXX / TEMP / HACKME
    content TEXT,
    FOREIGN KEY (file_id) REFERENCES files(id)
);
```

### commit_stats — 提交频率聚合

```sql
CREATE TABLE commit_stats (
    id INTEGER PRIMARY KEY,
    period TEXT NOT NULL,             -- 当前仅 week
    period_start TEXT,                -- 形如 2026-W31
    commit_count INTEGER,
    files_changed INTEGER,
    authors TEXT                      -- JSON 数组
);
```

---

## 索引

```sql
CREATE INDEX idx_files_rel_path ON files(rel_path);
CREATE INDEX idx_files_extension ON files(extension);
CREATE INDEX idx_files_module ON files(module_name);
CREATE INDEX idx_evolution_file ON evolution(file_id);
CREATE INDEX idx_tech_debt_file ON tech_debt(file_id);
CREATE INDEX idx_deps_name ON dependencies(name);
```

---

## 数据流

```
radar-build.py（入口）
   ├─ radar_structure.py  → files / modules
   ├─ radar_convention.py → conventions
   ├─ radar_health.py     → dependencies / conventions(health)
   ├─ radar_evolution.py  → evolution / tech_debt / commit_stats
   └─ 主流程写入 SQLite（幂等重建：先 DELETE 再 INSERT）
                │
                ▼
radar-query.py（入口）→ radar_commands.py 各命令 → radar_report.py 渲染
   overview / structure / conventions / health / evolution / search / diff / export
```

---

## 说明

- `conventions.sample_files`、`commit_stats.authors` 以 JSON 数组存储，查询端用
  `_parse_json_list()` 解析（兼容旧版逗号分隔格式）。
- 版本字段比较使用语义版本解析（`version_key()`），避免字符串比较误判。
- 增量扫描（基于 mtime + content_hash）为设计目标，当前未实现，每次构建全量重建。
