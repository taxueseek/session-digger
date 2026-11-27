# wechat-digger architecture

## Why this shape

对标 `session-digger` 的工程结论：

1. **表驱动 > if 森林**：新增数据源只改 `SOURCE_REGISTRY` 一行 + 钩子，不碰路由核心。
2. **分层不塌缩**：ACQUIRE / NORMALIZE / INDEX / ANALYZE 成本与可信度不同，禁止混写。
3. **路径可探测**：环境变量 → 常见 skill 安装位，禁止 `/Users/某人`。
4. **先索引后搜索**：FTS 是速度层，不是真相层；真相在 vault / wx-cli。

## SOURCE_REGISTRY 一行扩展

```python
SOURCE_REGISTRY["my_source"] = {
    "priority": 40,
    "label": "...",
    "detect": _detect_my,
    "history": _my_history,
    "contacts": _my_contacts,  # or None
    "sessions": None,
    "search": None,
}
```

优先级：vault(100) > wxcli(50) > export(20) > fixture(1)。`preferred` 可强制。

## CanonicalMessage

所有上游字段别名在 `normalize.py` 收敛。下游（analyze / index / render）**禁止**再写 `if source == "wxcli"`。

## 与旧 skill 关系

| 旧 skill | 关系 |
|----------|------|
| wechat-local-vault | **v0.0.2 起已 vendoring 进 `scripts/acquire/`**；外部 skill 仅可选回退 |
| wechat-insight | 分析能力内聚进 digger |
| baoyu-wechat-summary | 摘要结构吸收进 render |
| mac-wechat-dual-open | 进程工具，不合并 |
| wechat-humon-blogger / dbs-wechat-html | 内容生产，不合并 |
| taxue-weread | 读书，不合并 |

## 自包含采集路径

```
ACQUIRE_TOOLS / ACQUIRE_OPS (表驱动)
        │
        ▼
 scripts/acquire/{vault_cli,decrypt_all_dbs,extract_keys,...}
        │
        ▼
 ~/.config/wechat-*.json  +  ~/Library/Application Support/wechat-local-vault/
        │
        ▼
 SOURCE_REGISTRY["vault"] → normalize → index/analyze
```

## 双引擎（v0.0.3）：vault + wx-cli

**产品入口吸纳，不是代码仓吸纳。** wx 继续做在线/富 op 引擎；digger 做编排与分析。

```
CAPABILITY_MATRIX[op] → [vault, wxcli, ...]
        │
        ├─ vault: scripts/acquire (离线主路径)
        └─ wxcli: wx_bridge → PATH 上的 wx（需 daemon/init）
                │
                ▼
         normalize (仅消息类) → analyze/digest
```

| 表 | 作用 |
|----|------|
| `WX_SURFACE` | 一行挂一个 wx 子命令 |
| `CAPABILITY_MATRIX` | 一行决定某 op 引擎优先级 |
| `SOURCE_REGISTRY` | 读路径多源 detect/fetch |

**禁止** `--format json` 调 wx 0.3（用 `--json`）。  
**就绪**以 `sessions -n 1 --json` probe 为准，不只看 WeChat 进程。

## 一行修改消灭一类问题（本 skill 落地处）

| 问题类 | 一行/一表解法 |
|--------|----------------|
| 新数据源要改 10 处 if | `SOURCE_REGISTRY` 加一行 |
| vault 装在 .agents 还是 .grok | `paths.find_skill_root` 候选表 |
| wx / vault 字段名不一致 | `normalize._first` 别名元组 |
| 消息类型 int/str 混用 | `TYPE_ALIASES` |
| 搜索每次全量扫 | `index_builder` content_hash 跳过 + FTS |
