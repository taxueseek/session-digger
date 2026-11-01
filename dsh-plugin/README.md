# session-digger DSH plugin

session-digger 的 DSH 插件形态（skill + 插件双形态的插件面，同 argo-dsh 的形态纪律）。

把跨环境会话挖掘注册为 DSH 原生一等工具：**sd_search / sd_sessions / sd_stats**。每次调用单发执行 `python3 sd-recall.py <cmd> --json`——零常驻进程、零常驻 token；本地索引查询是低频动作，常驻连接在这里是纯负债。

## hybrid 路由（0.9.35 起，`dshNative` 配置可关）

宿主挂载 `@deepseek-ai/dsh-session-query-sqlite` 后，DSH 自己的会话（`~/.dsh/sessions`，zstd 事件存储，Python 索引读不了——`_session_analysis.py` 的 allow-list 有实测记录）经 `ctx.sessionQuery` 进程内检索并进结果：毫秒级、零 spawn、实时新鲜、不依赖索引重建。其余 16+ 环境照走 Python 索引；两路在 execute 里并行，总耗时取 max 而非 sum。

- **服务未挂载**：输出与 0.9.34 完全一致（静默降级，不加噪音行）。
- **服务在但调用失败/超时**：Python 结果照回，DSH 段如实标注 `unavailable` 与原因，绝不吞 Python 结果；DSH 腿受 `nativeTimeoutMs` 约束（首搜建索引时让位）。
- **scope=current 不搜 DSH 原生**（按 cwd 过滤需要调用方会话 cwd，工具层拿不到）：输出 `rerun with --scope all` 提示。`agent` 指定第三方环境时同样跳过；`agent=dsh` 时 DSH 腿照常出（Python FTS 对 DSH 是 deny-list，唯一有意义的命中只能来自原生腿）。
- **keyword 语义对齐**：`sd_sessions` 带 keyword 时 DSH 段走会话分组搜索，与 CLI 段同受关键词过滤。
- **形态纪律注记**：CLI 是外部进程，到不了 `ctx.sessionQuery`，DSH 行只出现在插件形态且以 `agent='dsh'` 标记来源——双形态间唯一的有意分歧。
- **隐私边界对齐**：DSH 命中每条只回 150 字片段（与 CLI user_messages 窗口一致），不回完整转录。

宿主侧的打开方式（web profile 用户层 `cordis.patch.yml`；官方 dsh-base 自带的该行默认 `openAt: never` 搜索关死，同 id 覆盖即可）：

```yaml
- id: session-query-sqlite
  name: '@deepseek-ai/dsh-session-query-sqlite'
  config:
    path: !!js dshHomePath('session-search.db')
    openAt: first-search
```

注意 `!!js` 上下文里只有 `dshHomePath` 可用（`dshCachePath` 不在，用它激活即 ReferenceError，连带 sessionQuery 消费方全部 pending）。

## 安装

```sh
# 从 GitHub（公开仓）
dsh plugin --profile web add "github:taxueseek/session-digger#main&path:dsh-plugin"

# 或从本地 checkout（link: 安装自动探测仓库根，无需配置）
dsh plugin --profile web add "link:/path/to/session-digger/dsh-plugin"
```

依赖：Python ≥ 3.9（session-digger 本体，纯 stdlib 零 pip 依赖）。

## 工具

| 工具 | 对应 CLI | 用途 |
| --- | --- | --- |
| `sd_search` | `sd-recall.py search --json` | 按关键词跨环境搜会话，回元数据 + 裁剪后的证据片段；服务在时并出 DSH 原生命中 |
| `sd_sessions` | `sd-recall.py sessions --json` | 列最近会话（元数据行，可按关键词过滤）；服务在时追加 DSH 原生 TSV 段 |
| `sd_stats` | `sd-recall.py stats --json` | 聚合计数（会话/消息/工具调用/错误/token + 分环境）；服务在时追加 DSH 会话计数 |

完整命令面（trend / skill-gap / skill-health / reflect / usage / topics / analyze）经 `nativeTools` 配置按需开启，或走 skill 形态（SKILL.md 路由）在 agent 会话内执行。

## 配置（cordis.patch.yml 用户层覆盖，同 id + name 才是替换）

```yaml
- insert:
    - id: session-digger
      name: '@taxueseek/session-digger-dsh'
      config:
        nativeTools: [sd_search, sd_sessions, sd_stats]  # 默认值；[] = 全关；未知名 loud fail
        pythonCommand: python3        # 默认；可设 SESSION_DIGGER_PYTHON
        nativeTimeoutMs: 30000        # 单次 CLI 超时
        nativeMaxChars: 24000         # 返回文本上限
        dshNative: true               # hybrid 路由开关；false = 纯 Python 行为
        sessionDiggerRoot: /path      # 默认自动探测：SESSION_DIGGER_ROOT > 常见安装位 > 本包所在仓库根
```

## 形态纪律

- **Python CLI 是唯一真源**：插件是薄封装，工具参数即 CLI 参数，渲染文本与 CLI 文本输出逐行同构——两条形态同一份信息，不可能漂移。唯一例外是上面说明过的 DSH 原生段（CLI 进程够不到宿主内服务，插件形态显式标记来源）。
- **版本号运行期从 package.json 读取**，永不硬编码。
- **不 import `@deepseek-ai/*`**：cordis 只承诺公共 ctx 契约；`sessionQuery` 走 `ctx.get('sessionQuery')` 字符串契约 + 引擎形状校验，缺方法即降级。
- **隐私边界**：工具只回元数据与裁剪后的片段（与 CLI 文本输出相同的 150/80/120 字窗口），不回完整转录；要全文走 skill 形态的 `--deep`。
- **新鲜度契约**：返回的 Index 行说清索引新鲜度；stale 时先 `index-builder.py build` 再重试。DSH 原生段不走索引、永远新鲜。

## 测试

```sh
cd dsh-plugin && npm test   # node:test，55 项：发现/参数构建/渲染/单发执行/降级/hybrid 路由
```
