# Session Digger Changelog

> 本文件是 `SKILL.md` 的版本历史，逐字搬移，未做改写。
> 搬离原因：历史条目占 SKILL.md 79% 的篇幅（25.3k / 32.1k 字符），而 SKILL.md 每次被
> 激活都整体进入上下文；版本历史对当前任务没有操作性价值。
> 当前版本与用法看 `SKILL.md`，改了什么看这里。

**v0.9.27** — 逐会话证据合并为一个进程：lite 召回慢路径快一倍

上一批（v0.9.26）留下的方案 D 本轮补做：recall-lite 的逐会话循环，旧实现每个会话至少
再 spawn 1 个 Python 进程查摘要缓存，缓存未命中时最多再加 4 个（消息、工具错误、决策点、
回存摘要），每个进程都重新付一遍解释器启动和库加载。现在全部收进一个
`sd-recall.py lite-report` 子命令：sessions 的列表直接顺着管道流进单个进程，输出与旧版
逐字节一致。

- **进程数**：最多 1 + N×5 个 Python → 固定 2 个（sessions + lite-report）。
- **实测**：慢路径（3 个会话强制全解析 + `--decisions`）1.10 秒 → 0.43-0.56 秒，快约一倍；
  缓存命中路径持平（0.33 → 0.42-0.51 秒，噪声区间）。
- **顺带修掉一类 bug**：决策点块沿用旧 heredoc 的 claude 专用解析器，grok/kimi 等会话的
  `--decisions` 永远输出「(no decision points found)」——列表明明列出了这些会话。改用按
  注册表路由的通用解析器后修复（claude 行为不变，实测 grok 格式会话从 0 条到正常输出）。
- **钉死一个假绿测试**：旧回归断言 `"决定" in stdout or "decision" in stdout` 会被
  「(no decision points found)」里的 "decision" 空转满足，且 fixture 用的是不符合真实
  Claude schema 的纯字符串 content，实际一条消息都没解析出来。新测试直接跑真实入口
  `lite-report`，断言收紧到决策行本体，并补缓存命中、trailer 行忽略两个行为钉。
- **依赖**：无新增，仍是纯 stdlib。
- **测试**：全量 370 通过、1 跳过（净增 2 个）。

**v0.9.26** — 三条坏死路径修通，会话列表从 1 秒降到 0.2 秒

一次 check+hunt 协作复盘：先量化基线再动手，三个 bug 全部实测复现、修复全部 red-green
钉死（新增 13 个回归测试，全量 368 通过）。

- **recall-lite 整条链坏死（低级 bug）**：脚本默认传 `--agent auto`，argparse 直接拒掉，
  `--scope all` 的 lite 召回 100% 失败。修复：`_AGENT_MAP` 增加 `auto → cross` 别名。
- **recall-lite `--decisions` 必崩**：决策点分支的 `sys.path` 多套一层 dirname 指到了
  scripts 的父目录，`import echolib` 永远 ModuleNotFoundError。修复后实测能输出真实决策点。
- **索引构建永久 errors=4**：Grok 自有的 session_search.sqlite 会残留已删除会话的幽灵条目，
  旧过滤器只挡目录、挡不住「根本不存在的路径」（`is_dir()` 对其返回 False）。一行谓词
  「普通路径只保留存在的文件」消灭整类问题：构建 errors 归零，顺带 pruned 掉 4 条陈旧行。
- **sessions 列表提速 4-5 倍**：0.75-1.0 秒 → 0.15-0.25 秒。根因是 dimcode 列表模式仍在对
  590MB 的 messages 大表逐会话 COUNT（冷页缓存首轮 0.51 秒，占跨代理列表 ~78%）。修复走
  既有的 `discovery_only` 契约——列表模式不取内容级字段，计数由 index.db 按路径回填，
  与 workbuddy/kimi 的 quick_scan 同一形状，不改架构。
- **`*.summary.jsonl` 自污染闭环斩断**：save-summary 写出的侧车文件被自家 `"*.jsonl"` 扫描
  重新识别成会话（MSGS=0 空行混进列表、注册表计数虚增）。用与构建器同一谓词
  （`".jsonl." in name`）把 claude 目录扫描、回退索引、递归发现、universal 四处同漏一并堵上。
- **universal 扫描收笼**：`--agent universal --scope current` 之前因适配器不认 `cwd` 参数、
  TypeError 降级后丢范围约束全扫 $HOME——25.5 秒换来 0 条结果。现在 universal 认 `cwd`，
  只扫当前目录树：0.22 秒、有结果。`--scope all` 的全盘发现能力保留（显式调用，约 18 秒），
  跨代理列表默认仍不含 universal。
- **依赖**：无新增，仍是纯 stdlib。

**v0.9.25.2** — 重建的 73% 字节是重复读：一次会话只解析一次

上一版把「构建时每会话多处调用探测」留成观察项。本轮先量化再动手，结论与直觉相反：
**探测不是瓶颈**——全量重建里 `_probe_schema` 只被调用 963 次、其中 321 次未命中，路径级缓存
已经生效。真瓶颈是**同一个转录文件在适配器路径上被完整解析 3–4 次**（stats / tools /
messages 各走一遍，identity 扫描再走一遍）。

- **口径**：全量重建 **5,653 次解析 / 1,556 个文件**，解析 **3,862 MB**，其中 **2,833 MB（73%）
  是对同一个文件的重读**。浪费分布是散开的——1–5 MB 组 46%、5–20 MB 组 30%、<1 MB 组 22%，
  不是「几个大文件拖累」，所以只优化大文件没有用。改完之后同口径 **2,495 MB（−35%）**。
- **修法（parse_once 作用域）**：`_iter_jsonl` 在作用域内把一个**完整**读完的记录列表记下来，
  后续读者直接重放。三个细节决定正确性：**只有读到底才提交**（`_probe_schema` 故意只读 40 条
  头部就 `break`，`GeneratorExit` 会跳过提交，否则整段会话会被截成 40 条——静默的召回空洞）；
  **读不到东西不提交**（原来的 reader 自己吞 `OSError`，那会把一次瞬时失败记成「这个文件是空的」
  并缓存一整个会话）；**作用域一退就丢**（不做淘汰策略，也不会跨会话驻留）。用 `ContextVar`
  而不是模块全局，与既有的 `discovery_only` 同构，嵌套的 `cross_tool_list_sessions` 线程
  扇出不会继承它。
- **效果（交错 A/B，同一份代码只切开缓存开关，6 组配对）**：**user CPU −28%**（中位；逐组
  −37/−32/−27/−24/−23/−30%）、**wall −10%**（−24/−19/−5/−9/−3/−11%，受机器负载影响）、
  **maxRSS 同向下降**（配对 −17%/−5%/−2%…多数组持平或更低，未出现上涨）。探针侧同样干净：
  `json.loads` 调用数 6.74M → 3.88M，函数调用总数 171M → 122M。
- **等价性是量出来的，不是推出来的**：6,379 个会话逐个「作用域内算一次 / 不设作用域算一次」
  背靠背对比（毫秒级间隔，规避活跃机器上转录文件持续被写入造成的漂移），**差异 0 处**
  （仅排除构建时钟列）。第一次用「两次重建比摘要」的做法**证明不了任何事**——同一份代码
  连跑两次摘要也不同，是这个活体语料在写自己的会话。
- **顺带：4 处私有解析循环并回正规 reader**。`grok` 的 `events.jsonl`/`chat_history.jsonl` 有两个
  函数各自手写 `open + strip + json.loads + except` 循环，正是 `_iter_jsonl` 文档里说"已收敛
  30+ 处"却没收敛到的遗留。改走正规 reader 后：381 个 `chat_history.jsonl` **每个只解析 1 次**
  （另有 561 次重放），并顺带白得 zstd 支持。**隔离实测**（新旧适配器互换，其余代码不变）：
  JSON 字节 **2,747 → 2,496 MB（−9.2%）**、解析调用 3.88M → 3.68M（−5.1%）、wall −2.3%——
  **收益是真的，但不大**；真正的理由是这一段本就该走正规 reader，顺带把「同一个文件解析 3 次」
  收成 1 次。**等价性实测**：
  grok 家族的 **411 个转录**（跨 `~/.grok`、`~/.kigi`、`~/.kimix` 三个共用该布局的环境）
  × 5 种调用形态（`grok_extract_tools` 全量/仅错误、`_grok_extract_messages` 全量/仅 user、
  `_grok_session_stats`）与改动前的模块逐条对比，**0 处差异**。
- **低级别 bug 1：恒真谓词把「未过滤」路径变成了最贵的路径**。`_session_filter` 永远返回一个
  可调用对象，于是 `--scope all`（默认 scope + 默认 agent）也走「把整个排名集合取回来再筛」。
  实测 `agent`：**71.8 ms vs 24.9 ms（15,210 行），答案完全相同**；`argo` 25.9 vs 9.3 ms。
  它还让 `_fts_search` 文档里「未过滤路径保持 `limit*5`」那句话**失真**。**一行修复**：没有规则
  可施加时返回 `None`——任何调用方传进一个空转谓词都会自动走便宜路径，这一类不再是每个调用方的
  自觉。
- **低级别 bug 2：空结果把「没法检索」报成「索引里没有」**。`!!!` 被分词器清成空 MATCH 表达式，
  旧文案却说「索引里任何范围都没有这个词——换个词或检查这条会话是否已建索引」，把读者支去找
  不存在的数据。现在先问一遍分词器：问不出问题就直说「这个词没有可检索内容」。
- **低级别 bug 3：`detect_topic_boundaries` 把每条消息分词两次**（`text_prev` 重算了上一轮的
  `text_cur`）。改成把上一轮结果带下去，分词调用 137,626 → 70,818。**等价性**：全语料 6,016 个
  会话 80,122 条消息 + 3,000 条随机边界流，逐条相同。
- **低级别 bug 4：五处版本声明各说各话，而且没有门禁**。`SKILL.md`/`combo_map.json`/README 徽章
  是 0.9.25，而 `.claude-plugin/plugin.json` 停在 **0.9.3**、`marketplace.json` **0.9.2**、
  `herdr-plugin.toml` **0.9.12**。没有任何构建期读者，所以没有任何东西发现——按 manifest 版本
  判断更新的安装器，等于**从来没看见过最近二十个版本**。修法是**一条门禁**：`tests/
  test_manifest_versions.py` 以 `SKILL.md` frontmatter 为唯一真源断言六处一致（写死版本号本身
  才是会过期的那个东西），并就地修正三处漂移。
- **回归门 +18（337 → 355 passed / 1 skipped，3.9.6 与 3.14.7 双版本同结果）**：
  `tests/test_parse_once.py`（9 项作用域契约 + 1 项「一次会话只完整解析一次」的端到端，
  后者特意只数**读完**的遍数，避免把探测的 40 条头部误算成一遍）、
  `tests/test_manifest_versions.py`（2 项）、`tests/test_scope_selection.py`（+5：直接读绑定
  参数证明未过滤路径没有放宽、真实过滤仍放宽、agent 收窄同轴、空结果两种诊断）。
  **变异验证 3/3 全被捕获**：把作用域换成 `nullcontext` / 让谓词重新恒真 / 改回漂移版本号，
  对应门禁立刻失败，文件逐字节还原。
- **实测面**：8 个 herdr action 逐条 rc=0；`smoke-multi-env` 15 个已适配环境列举与路由正常；
  12 个子技能 front-matter 校验；`_check_public_api` PASS；**空 HOME** 下 10 条命令 7 条 rc=0，
  其余 3 条是设计内的「Index not built / usage」而非崩溃。
- **量过但没做**：
  - **第 4 遍读（identity 扫描）不值得动**：`_scan_file_for_model_tokens` 全量只有 5,242 次调用 /
    63,898 条记录 / **0.57 s**（它有 8,000 行上限，且只在缺 model/token/first_prompt 时触发）。
  - **探测只读 40 条头部是有意的**，不能纳入缓存——正是它逼出了 parse_once「读到底才提交」的
    设计。
  - **grok 的 `updates.jsonl`（39 MB × 340 次读）帮不上**：它走的是自己的 `open` 循环，且每次读
    都发生在**不同会话**的计算里，会话级作用域按设计不该跨会话复用。
  - **`--scope all` 的 `sessions` 列表 0.39 s 没有冗余**：全部是跨 30 个环境的真实发现 I/O
    （`is_dir` 3,078 次 + dimcode SQL 0.21 s）。
  - **未知环境的角色键名边界没动**：实测 7 种新形状 **4 种可读、3 种静默读空**
    （`speaker+body` / `from+text` / `prompt+response`——缺的是角色**键名**，`role`/`type` 是写死的）。
    这是**能力扩展**，超出本轮「最小改动、不影响能力」的范围，边界已由
    `tests/test_unknown_environment.py` 钉住，改动前先量收益。

**v0.9.25.1** — 复核：过滤必须在「选择」内，而不是对结果做事后清扫

对 v0.9.24/0.9.25 做了一轮严格复核。先把性能口径量清楚：**CPU 侧已无数量级瓶颈**
（min-of-3 热缓存：`sd-recall search` 0.07–0.21 s、`sessions --limit 200` 0.44 s、
无变更构建 0.67 s、冷装全量重建 17.8 s），所以本轮不追 CPU，只修两类**假的否定结论**和一处
**静默的字段失真**。

- **`--scope current` 把有历史的词报成「没有」（P0，最严重）**：cwd 过滤发生在 FTS 已经按 BM25
  截断之后。实测本机 GPT 项目：`记忆` 在项目内有 **51** 个会话、top-20 里 **0** 个；`agent`
  项目内 **174** 个、top-20 里 **0** 个。于是这个**默认作用域**对两者都输出
  「No match ... in scope 'current'」，而空结果报告紧接着说「索引里另有 584 行」——
  暗示的是「不在这个项目里」，实际是「不在这 20 条里」。`--agent` 收窄同理，会静默少返。
  **修法**：过滤条件作为**选择的一部分**交给 `_fts_search`（新增 `keep` 谓词），过滤存在时
  窗口放宽到整个排名集合；选择谓词收进 `_session_filter`，一处定义 cwd 与 agent 两条规则。
  过滤谓词跑完 6,373 行 `session_in_cwd` 只要 62 ms，而 FTS 取全量命中集最坏 50 ms（`agent`
  15,206 行），因此**正确性不以延迟为代价**：`--scope current` 暖缓存 0.08 s，与改动前同量级。
  未过滤路径（`cross` + `all`）窗口保持 `limit*5`，逐字未变。
- **空结果报告的命中数口径与检索路径不一致（P1）**：`_facet_counts` 不 JOIN `sessions`，而
  `_fts_search` 必须靠这个 JOIN 才拿得到路径。库里有 4,542 行孤儿 FTS 行时（重建前实测），
  报告会承诺「索引另有这些行」——**放宽作用域也拿不到**。**修法**：同一个 JOIN，
  并补上 `build_match_query` 返回 None 时的空结果分支。
- **换族后旧族的字段残留（P1，设计缺陷类）**：`_apply_family` 就地改同一个 dict，
  于是被否决的族留下的字段会被接任的族继承。实测同一份文件：`nested_message` 先被选中、
  读不出正文 → 回退 `flat_role`，但 `model_field` 仍指向 `message.model`，
  而「找不到 model 才去扫顶层 key」的兜底因为「字段看起来已有答案」被跳过 ——
  最终报出 `nested-model`，而这份记录真正携带的是顶层 `top-level-model`。
  **修法**：`_apply_family` 改为**纯函数**（从同一份 `base` 派生、返回新 dict），
  一次调用消灭这一类残留。变异验证：把 `dict(base)` 改回 `base`，3 个门禁立刻失败。
- **grok 子会话回填：29 次全表扫描 → 1 次**：每个子会话一条 `UPDATE ... (id=? OR id LIKE ?
  OR jsonl_path LIKE ?)`，无可用索引即一次整表遍历。**实测 85.3 ms → 21.5 ms（−75%）**，
  占无变更构建（0.67 s）约 9.5%。同一个值、同一批行，收成「一次扫描 + 每 900 个 id 一条语句」，
  与同文件 `_delete_scoped_rows` 已有的写法一致；顺带把 `set` 排序，去掉构建内部的不确定顺序。
- **回归门 +13 项**：`tests/test_scope_selection.py`（页面确实全在项目外 → 项目内会话仍须返回、
  `--scope all` 仍按排名、`--agent` 同轴、未过滤路径仍填满页、孤儿行不计入「另有」、
  不可检索关键词不报错）、`tests/test_probe_routing.py::FamilyAttemptIsolationTest`（纯函数契约 +
  基座不被改写 + 被否决族不捐赠 model 路径 + 端到端读到自己记录的 model）、
  `tests/test_index_maintenance.py::TestGrokChildBackfill`（角色正确 + 子会话只由一条语句标记）。
  **变异验证 4/4 全被捕获**（事后过滤 / 去掉 JOIN / 改回就地改 / 退回逐子会话 UPDATE），
  文件逐字节还原。全量 **337 passed / 1 skipped**。
- **量过但没做**：`skill-gap-finder analyze` 输出 **56 KB**（38 条提案，是次大命令的 8 倍），
  但每条证据已 `[:10]` 截断，体积来自提案条数本身；收成 top-N 是产品取舍（人工审核面），
  不在本轮「最小改动」范围。工具摘要的检索深度上限也不在 facet 里，而在采集侧
  `result_preview` 的 150 字符封顶（索引内 TOOL 行平均 143 字符、无一超过 500）——
  放宽要改解析语义 + bump `_PARSER_EPOCH` + 17.8 s 全量重解析，需先量收益。

**v0.9.25** — 全环境读取审计 + 两处「静默读成空」的路由缺陷

起因是问「各环境的对话数据都能读到吗」。按环境逐项对账（发现数 ↔ 索引数、零可检索行、证据投影、
路径存在性、最新一条的日期 ↔ 磁盘最新一条），16 个环境里 13 个**发现数与索引数完全一致**，
`user_evidence_json` 空值 **0**、失效路径 **0**。审计同时暴露 **380 个会话零可检索文本**，逐个
追根因后分成两类：**真对话读不出**与**本来就不是对话**。这一轮修前者。

- **探测族判错（codebuddy 1.3 MB / 54 条消息 / 75 KB 正文，索引 0 行）**：记录是扁平的
  `role`+`content`，其中混进了极少数带 `message` 字典的记录。探测按「有 `message` 字典」判为
  `nested_message` 族，随后按 `message.content` 取文本，于是**一条都取不到且不报错**——该会话
  永远显示为空。**修法：选完族要验证真能读出文本**（`_schema_reads_text` 逐角色试读），读不出
  就按 `flat_role → flat → …` 的顺序换族；全都不行则**保留原判**（这是修复，不是改默认值）。
  族的构造收进 `_apply_family`，避免分支体复制两份。
- **弱特征误判成 claude（commandcode 0.9 MB / 111 条消息，索引 0 行）**：内容检测里 claude 的
  注释写着「`type_field` 必需」，实现里却没有这条约束——`cwd` 一项就值 4 分，于是带
  `cwd`+`sessionId`+`parentId` 的命令码转录被判定为 claude，交给 Claude 读取器后按它的字段路径
  取文本，同样是**静默 0**。**修法：按注释实现，把评分整段挂在 `type_field` 之下**；并补一条
  反向门禁——真 claude 形状（`type`+`message.model=claude-*`）必须仍然解析为 claude。
- **实测收益**：零可检索会话 **380 → 358**（−22，全部在 universal），FTS 行 79,643 → 80,033；
  逐个验证 codebuddy **0 → 54 行**、commandcode **0 → 111 / 0 → 50 行**；抽样 438 个会话跨 14 个
  环境重算统计，**下降 0 个**（唯一变化是修好的那一个上升）。
- **量过但没做：把采样窗口扩成「一直扫到找到 N 条带 schema 的记录」**。实现后实测**全库 2,027 个
  会话里，带 schema 的记录首次出现位置中位数是第 1 条、最大第 2 条**——「schema 埋在遥测前言下面」
  这个场景在本机不存在，真正修好上面两处的是族验证与 claude 门槛。而它让 **147 个大文件白扫一遍**，
  全量重建 +12 s。**撤掉**，并留一条 `ProbeCostTest` 钉住「探测只读头部 40 条」这个上界，
  防止下次又被加回来。
- **代价如实记账**：族验证只影响**全量重建**，A/B 同机对比 36.9 s → 46.4 s（+9.5 s，+26%）；
  **增量路径不受影响**（无变化时 2.2 s / 0.4 s）。换族本身在隔离测量里只占 3.3 s/全库，多出来的
  是构建时每会话多处调用探测带来的重复开销——留作下一轮的观察项，不预先抽象。
- **回归门 +8 项**（`tests/test_probe_routing.py`）：族验证（杂散 `message` 字典不得劫持家族）、
  真嵌套格式不得被压成 flat_role、被接受的 schema 必须能读出文本、非对话文件不得被硬安上 schema、
  探测只读头部、claude 门槛正反两面。**变异验证 3 项全被捕获**：去掉 claude 门槛 / 让族验证恒真 /
  把窗口退回只看头部，各自对应的门禁立刻失败（文件逐字节还原）。全量 **324 passed / 1 skipped**。
- **本轮未修（用户已明确跳过子代理）**：zcode 的 **154 个子代理目录**没有 `transcript.jsonl`（只有
  `metadata.json` + `output.txt`，其中 prompt 与最终报告可读）与 kimi_code 的 **42 个 `agents/agent-N/`
  wire 日志**（9.3 MB，适配器写死 `agents/main/`）。另外 `universal` 剩下的空行多为 hook 事件日志、
  轨迹文件与缓存，**本来就不是对话，空是对的**——审计里我一度把「字节大」当成「有内容」，逐个打开
  才发现 197 KB 的「有内容」其实是系统提示词，一条对话都没有。

**v0.9.24** — 工具输出进入检索面（默认不参与排名）+ 空结果可判读 + 入口分册

起点是一次**问题重定义**：先按「索引字节覆盖率」量，全语料 **2.2%**（28.8 MB / 1332.6 MB），
看着像重大缺陷。解剖一个 21.6 MB 的子代理转录后发现这个口径是错的——它 6,794 条记录里
`model_request` 占 17.87 MB，那是同一段上下文被**重复发送 53 次**。改成「去重后的独立证据」
再量：4,916 条快照消息去重后是 **215 条 / 670 KB**，其中**工具输出 576 KB（86%）**。同时这
一次也**证伪了自己关于「适配器漏正文」的假设**：那 12 条独立 user 消息里 11 条是
`<system-reminder>` 噪声，真正的任务提示词 1 条，适配器恰好就是取到它的那条。所以缺口不在
采集完整度，在**检索面**：只有助手那行 `[TOOL: name] <key>` 进了 FTS，工具结果整块不在。

- **写入侧新增 `TOOL` 分面**：从单遍分析已经解析好的 `result_preview` 生成摘要行（不再重读
  文件），400 字符封顶，同会话内相同摘要折叠为一行——同一个文件被读 20 次是一份证据，不是
  二十份。**刻意不并入 `all_msgs`**：那张表同时喂证据投影、话题边界和身份推断，塞进去等于
  顺手改掉三件事。`_PARSER_EPOCH` 升 v7，增量构建自动重解析一次，无需 `--rebuild`。
- **实测产出**：4,371 行 TOOL / 627 KB（`zcode_v2` 3,954 + `claude` 417），正文行数一行未变；
  全量重建 18.9s → 22.19s（+17%）。
- **消融（先做，再定默认值）**：从 TOOL 行抽 250 根针问新旧两版索引——旧版已能找到 223 根
  （89%，它们同时也出现在正文里），新版 248 根，**净回收 25 根 / 10%**。代价实测：把 TOOL 并进
  默认排名后，查 `argo` 的 top-20 从「19 ASSISTANT + 1 USER」变成「12 + 1 + **7 TOOL**」，
  **35% 的版面**被工具摘要占掉。10% 的收益换 35% 的稀释不划算，**据此定为默认排除**，
  `--include-tools` 并入、`index-builder.py search --tools-only` 专查；默认结果集回到
  19 + 1，与改动前逐项一致。
- **空结果从一句话变成可判读的状态**：实测 `第一性原理` 在 `--scope current` 下返回 0，
  而索引里有 **1,960 行**，旧输出只有 "No matching sessions found"——与「真的没有」不可区分。
  现在空结果打印作用域、索引状态行、同词在更大范围的分面命中数，以及下一步命令（含
  「这些是工具摘要、用 `--tools-only` 读」这一路）。
- **命中行带上身份字段**（`agent` / `project` / `model` / `outcome` / `modified` / `messages` /
  `path`），与 FTS 同一次查询取回：此前每条命中要回答「这是哪次会话、算不算数」都得再查一次。
- **入口分册**：SKILL.md 11,038 → **9,421 字节（−15%）**。子命令查表（28 行）与 `/usage` 取法
  逐字搬进 `references/command-map.md` / `references/usage.md`，入口只留路由与判据；新增
  「检索预算与停止条件」（默认额度、停止条件、正文与工具两层、空结果不是结论）。
- **回归门 +14 项**（`tests/test_tool_facet.py`）：摘要前缀与分面、400 字符封顶、同会话折叠、
  占位符不当证据、两处默认排除、`--tools-only` 只回 TOOL、`--include-tools` 两层都回、命中身份
  字段、空结果四种情形。**变异验证**：删掉 `sd-recall` 或 `index-builder` 任一处的 `role != 'TOOL'`
  过滤器，对应门禁立刻失败（两项都验证过，文件逐字节还原）。全量 **316 passed / 1 skipped**。
- **顺带发现（未改）**：重建前的索引里有 **4,542 行孤儿 FTS 行**（`sessions` 里已无对应行），
  所以「行数对比」必须用 JOIN 后的口径，否则会把清理旧账误读成新改动删了数据。全量重建会清掉。
- **量过但没做：指纹阶段跳读文件**。增量构建在「什么都没有变」时花 2.2s 发现、0.9s 指纹。
  想法是 `(mtime, size)` 未变就不读那 8 KB 头尾——响应只需一次 stat。**先量上界再做**：文件型
  候选 2,035 个，纯 stat 0.008s、头尾读 0.146s，**可省上界 0.138s，约占全量构建的 5%**，代价是
  为存 size 加一次 schema 迁移 + 迁移测试。收益不抵代价，**放弃**（这也是 v0.9.23.4 那条
  「先把代价量出来再决定动不动手」的同一条纪律）。指纹那 0.9s 的大头不在这里，在 `dimcode://`
  虚拟会话的逐会话映射查询。

**v0.9.23.5** — 修掉「永远不会更新的会话行」

上一轮把这条如实留在台账上，本轮修掉。183 行（`user_evidence_json` 空值）之所以永远不更新，是因为**索引的扫描范围与报告层的发现范围是两套定义**：

- `scan_sessions` 只遍历 `ENV_REGISTRY ∪ KNOWN_UNADAPTED`；`scan_all_environments_parallel` 另外扫 `$HOME` 下未知 dot-dir 并报为 `discovered`。于是被发现的目录不会被索引，已索引的行不会被重访。
- 实测这 183 行（`pi:` 166 / `kimixi:` 8 / `taxue:` 5 / `kiro:` 2 / `jimeng-cli:` 1 / `opencodex:` 1）文件全部存在，但 `indexed_at` 停在 10:18，同期其余 6348 行是 11:16——**它们永远不会更新**。剪枝的「注册表根目录之外则保留」护栏又让它们免于被清掉，于是既不会刷新也不会消失。

**修法：从索引自身把环境推导回来**，不持久化任何东西。报告层把未知 dot-dir 命名为 `dotdir.name.lstrip(".")`，所以环境 id `pi` 的根就是 `~/.pi`——只要「索引里有该 id 的行」且「该目录还在」，这个环境就仍然活跃，必须留在扫描集合里（`echolib.adopted_envs`）。

- **扫描集合与剪枝集合收成同一个函数**（`_builder._scan_envs`）：两个集合必须一致，否则要么留下永不更新的行，要么删掉从没列过的行。
- **刻意不走另外两条路**：① 自动索引任意未知 dot-dir——会把 `~/.foo/*.jsonl` 的内容吃进 FTS 与证据投影，而报告层只是*列出*目录名，索引内容是不同量级的动作，对发布给别人的工具是隐私升级；② 显式登记进 `KNOWN_UNADAPTED`——发布版里会出现本机路径。「只从索引已有的 id 推导」保证发现面永远不会超过上一次构建已经认定为会话的范围，且双向收敛（目录删了就不再收养）。
- **路径来自数据**：id 由索引行推导后要当路径段用，故 `/`、`\`、`..`、前导 `.` 一律拒绝。
- **实测**：收养 6 个环境，183 行全部刷新（证据空值 **183 → 0**，会话 6535 → 6539，多出 4 个新增会话）；首轮构建 2.97s（一次性重解析 190 个），随后回到 0.43–0.47s 且幂等；发现阶段代价 **+7.1ms**（`scan_sessions` 171 → 186–197ms；最贵的 `jimeng-cli` 3.6ms 是「整个 dot-dir 换 1 个会话」）。先前冻结的 `pi:` 会话现在正常渲染用户消息。
- **残留（刻意保留）**：环境目录被删除后，其行无法刷新但也**不会被删**——保留的是这些会话仍可被 FTS 检索到的历史；护栏刻意不把「看不见的目录」当作删除证据。代价是这些行的统计停留在最后一天。
- **回归门 +12 项**（`tests/test_adopted_envs.py`）：id ↔ dot-dir 映射、目录消失即不再收养、已注册环境不重复收养、非目录不收养、路径转义输入被拒、端到端「冻结行被重新扫描并被构建刷新」、剪枝与扫描集合一致。做了变异验证：去掉 `_scan_envs` 里的收养后 3 项（含端到端那项）失败。全量 **302 passed / 1 skipped**。

**v0.9.23.4** — 复核轮之四：把「静默降级」当缺陷处理

同一口径重测全部日常入口后，CPU 侧已无数量级瓶颈（最慢的单项是发现阶段的 26 ms），
所以这一轮不再找热点，改为审「读到的数据是假的却看不出来」这一类。四条都是真实可复现
的静默失败，共同根因是**哨兵值与真实值不可区分**。

- **空证据投影被当成「这个会话没有用户消息」**（召回洞，静默，线上正在发生）：`user_evidence_json` 的默认值是 `''`，表示「从未计算」，而 `'[]'` 表示「算过了，没有用户轮次」。`cmd_search` 只判断「索引里有这一行」，于是 `''` 渲染成零证据。线上索引 6531 行里 **183 行（2.9%）**处于该状态，实测 `--decisions` 输出与普通检索**逐字相同**——旗舰功能整个失效而没有任何信号。新增 `_evidence.projection_available()` 作为该契约的唯一判据，两个消费点（`sd-recall`、`deep_analyze`）都改为不满足就走文件扫描兜底。兜底代价实测 0.8 ms/会话（20 条结果 16 ms），可忽略。
- **可读的索引不等于当前的索引**：同一份 `index.db` 被多个安装副本共用，旧副本每次运行都会把它自己的 schema 版本盖回去（实测线上 v4 被写成 `"2"`）。结果就是「HIT」但数据缺项。`_schema.init_db` 的迁移台账改为**单调**——只在代码版本更新时迁移，库比代码新时不动它；`cmd_search` 的索引状态行也改为说出缺什么：「`HIT, schema v2 < code v4 — evidence falls back to file scan (run: index-builder.py build)`」。运行一次构建即消失。
- **FTS 删除与插入不同生共死**（数据损坏，静默）：批量删除先执行，随后逐会话插入；插入失败只记一条 warning，会话行却留在库里。下一轮构建因指纹一致跳过它——于是「列表里看得见、搜不到」是**永久**的，且不计入 `errors`。改为插入失败即删掉该行并计入错误，下一轮 `prior` 为空必然重试。
- **扫描失败未登记于 glob 兜底分支**（可删整环境）：适配器分支会把失败环境记进 `_SCAN_FAILED_ENVS`，兜底分支却是裸 `except: continue`。而剪枝只跳过登记过的环境——于是「列举失败」被读成「用户把这个环境的会话全删了」，整个环境的行连同 FTS、边界行被真删。两个分支改为同一契约。
- **`skill-health` 的 10 条硬编码告警里 10 条是假的**：自检遍历文件系统，把 `.gitignore` 排除的本地产物（`skills/*/reports/` 的交接笔记与前期研究）也计入——那些文件本来就合法地写着本机路径，发布版里一个都没有。改为按 git 跟踪的文件判定（`ls-files` + `--show-prefix`，非仓库环境退回目录遍历），并把手写用户名白名单收成一处（`/Users/dev/`、`/Users/me/` 这类示例不再误报）。实测 10 → 0，且真实用户名路径仍判 CAUGHT（示例名与 `/Users/<user>/` 占位形式不报）。
- **`combo_map.json` 缺 `/deep-analyze`**：于是 `/deep-analyze` 完成后没有下一步提示（协议要求「完成后读取 combo_map 对应 next」）。补齐并修正版本号漂移（0.9.23 → 0.9.23.3）。
- **死代码**：`_DIMCODE_FP_MAP = None` 在同一文件里定义两次（第二处还挂着重复注释），合并为一处。
- **`SKILL.md` 32.1k → 6.9k 字符（−78%，每次激活省 ≈9.7k token）**：历史条目占 79% 的篇幅，而 SKILL.md 每次被激活都整体进上下文。原文逐字搬进 `CHANGELOG.md`（校验：搬移前后字节级一致），SKILL.md 只留一行指针。路由覆盖不受影响（`skill-health` 的 missing_routes 仍为 0）。
- **回归门 +17 项**：`test_evidence_projection.py` +5（`''` 与 `'[]'` 必须可区分）、`test_schema_migration.py` +3（版本单调、降级不重放）、`test_index_maintenance.py` +3（FTS 失败即不留行、下一轮必重试、glob 失败必登记）、`test_unknown_environment.py` +6（未知环境端到端）。新测试逐条做了变异验证：把修复逐个还原后对应测试必须失败（FTS 两条、glob 一条、版本单调一条均确认失败）。
- **实测面**：全量 **290 passed / 1 skipped**；Python **3.9.6 与 3.14.7 双版本**均通过；30 个脚本在 3.9.6 下全部可导入；`herdr-plugin.toml` 各 action 与主/子 CLI 子命令 rc=0；15 个已适配环境 `smoke-multi-env` 正常；`wechat-digger` 子技能 208 passed；12 个子技能 front-matter 合法；**空 HOME**（一台没有任何 agent 环境的机器）下 10 条命令 9 条 rc=0（`deep_analyze` 在无数据时如期返回「范围内没有符合条件的会话」并 rc=1，属设计内）。
- **未知环境普适性实测**：7 种「新 agent 可能采用的记录形状」里 **4 种可用**（`role`+`content`、`role`+`text`、`type`+`content`、`type`+`message.content`），**3 种静默归零**（`speaker`+`body`、`from`+`text`、`prompt`+`response`）。角色*取值*早就做了别名（`human`/`bot`/`ai` 都在 `_USER_VALS`/`_ASSISTANT_VALS`），缺的是角色*键名*——`_probe_schema` 的候选路径硬编码为 `role`/`type`。本轮只把这个边界测出来并写进测试文件的文档串，**未改探针**：扩展启发式属于能力变更而非缺陷修复，且会牵动 3660 行适配器里的家族判定，风险与收益不成比例。

**v0.9.23** — 日常性能轮：全表扫描类缺陷清零（搜索 44×、构建 3.2×、库 −51%）

四个瓶颈都用实测定位、修复后用同一口径复测。共同根因是 FTS5 的 `session_id` 是 UNINDEXED 列——凡按会话读写都要全表扫描。

- **检索：去掉按会话读 FTS**：`evidence_from_index` 每条结果都 `SELECT ... WHERE session_id = ?`，而 FTS5 的 `session_id` 是 UNINDEXED 列，这条查询只能全表扫描。改为索引期投影（`index_builder/_evidence.py`，写进 `sessions.user_evidence_json`，schema v4），随 `quick_stats_from_index` 的批量读一起返回——搜索路径归零额外查询。投影在索引期对**完整文本**求值决策模式，只有展示文本截断到 300 字，所以 `--decisions` 不受展示截断影响。检索端到端实测 0.03 s / 3 条结果（含证据渲染）；投影与文件扫描兜底路径在 40 个抽样会话上逐条比对 `user_messages`，零不一致。（早先记的 0.89→0.01 s / 单次 127 ms / 未命中 1240 ms 无法在同一索引副本上复现，口径修正见 `docs/engineering/quality-performance-baseline.md`）
- **构建 12.5s → 3.9s**（3.2×）：三处叠加。① `_single_pass_analyze`（320 行）写完从未接线，而 `_compute_session` 仍在同一次构建里读同一文件 3–4 遍（stats/rich/messages/identity）；接线后实测单会话 205ms→75ms，19 个字段零差异。门禁一并从**黑名单改成白名单**——原黑名单漏掉 `dsh`（zstd 事件流）与 `universal`（SchemaProbe），这两类文件被静默读成 0（实测 610 条消息报成 0）。② dimcode 会话指纹用 `MAX(createdAt) GROUP BY sessionId`，该列无索引覆盖，要在 436MB/13.5 万行的外部库上逐行回表，实测冷启 2.30s → 0.078s。③ 逐会话 `DELETE FROM messages_fts` 每次全表扫描 28.9ms，整体重解析时 182s；改为按 900 个 id 分批一次删完。**注**：无变更构建里的增益实际来自 ① 之外的两处——`_backfill_session_roles` 的 `needs_role` 守卫与 ②，③ 在这条命令上不生效（无变更时删除列表为空），已更正归因
- **索引 333MB → 162MB**（−51%）：FTS5 从不自动合并段，`messages_fts_data` 在增量删插中从 51MB 涨到 219MB（内容未变）。新增 `_compact_index`（`optimize` + `VACUUM`，实测 3.7s+1.1s），按**重写量**触发（≥200 且 ≥10%，碎片本就来自重写），`build --compact` 可手动触发
- **测试不再写生产库**：`tests/conftest.py` 把所有测试指向临时数据目录。此前 `test_deep_analyze` 经 `ensure_fresh_index` → `build_index` 会重建用户的真实索引（`builder.DB_PATH` 未被 patch），实测表现为并发时 `database is locked` 与线上库持续页膨胀
- **低级 bug**（均实测复现）：① DSH store 白名单写死 v0/v2 且只认 `session-` 前缀目录 → 299 个会话只索引 215 个（丢 84 个，28%），改为按版本号取最高并放开目录名前缀；② DSH 的 `user/message` 未按 `data.source.kind` 过滤，系统注入（skill-catalog/agent-instructions/plugin）被当用户消息 → user_messages 虚增 **3.84×**；③ Codex `msg_count` 恒为 0（`_, first_prompt = _codex_quick_scan(...)` 丢了计数）；④ 占位模型名单靠手工维护、已漂移两次（`model='dsh'` 有 107 行），改为从环境注册表派生；⑤ `detect_topic_boundaries` 的内容位移用 `str.split()` 分词，中文整句是一个 token → 任意两条不同中文消息相似度恒为 0，**每条消息都成"主题边界"**（实测 48763 行 / 71003 条，平均 1.46 条一个），改用规范分词器后噪声行降 43%；⑥ `index-builder.py detail` 的 `PRAGMA table_info` 取错了字段位（`d[0]` 是列序号不是列名），整个详情载荷的键变成 `"0"/"1"/"2"`——按名取任何一个字段都拿不到；全仓其余 12 处该 PRAGMA 的用法都是对的，属单点笔误
- **解析世代对 dimcode 失效**：`_PARSER_EPOCH` 只进了文件指纹，占索引 68% 的 dimcode 行永远不随解析器变更重解析（正是该机制注释里要防的"陈旧 0-token 行"）；世代纳入 dimcode 指纹
- **文件规模**：`_builder.py` 1479 → 760 行（`_session_analysis.py` 承载"文件进、字段出"的分析层），新增 `_evidence.py`；净减 479 行
- **回归门 +27 项**：`test_evidence_projection.py` 12、`test_index_maintenance.py` 11（批量删除上限/压缩不丢行/触发门/detail 字段名）、单遍读门禁与等价性 5（含"未知环境一律不准走单遍读"）。全量 **239 passed / 1 skipped**
- 一次性迁移成本：schema v4 + 世代变更触发全量重解析一次（本机 6290 会话 255s，其中 182s 即上述逐会话删除，已修，后续同规模重建约 70s）

**v0.9.23.3** — 复核轮之三：剩余未提交改动的缺陷（DSH / 单遍解析 / 环境注册表）

第二轮把入口耗时全部压到 0.75s 以内后，转去审查尚未被人看过的那部分未提交改动（`_session_analysis.py` 810 行、DSH 适配器、`_registry_data.py`、wechat-digger）。三条真实缺陷，都带可复现证据。

- **DSH 把用户真实指令当系统注入丢掉**（召回洞，静默）：`data.source.kind` 过滤原先写的是白名单 `kind != "user" -> 丢弃`，而 `coordinator` 是用户对子会话说话的方式——「请立刻把目前查到的所有内容直接输出给我，不要再继续检索了」这类手打指令被整条扔掉。全机 kind 普查：`coordinator` 7 条（3 个会话），其中 `79f834b8…` 一个会话 4 条用户轮次只索引了 1 条。改为**反向白名单**：只丢弃已知注入 kind（plugin / skill-catalog / skill-invocation / agent-instructions / agent-message / subagent-report / subagent-settled，共 810 条），未知 kind 一律按用户输入计——未知更可能是 DSH 的新特性，丢一轮用户输入比多算一行更糟。修复后该会话 1 → 4，DSH 全域 485 → **492**（正好 +7）。
- **一个坏字段炸掉整次构建**：token 计数直接来自第三方 JSONL，`stats["input_tokens"] += usage.get("input_tokens", 0)` 这类累加没有任何类型守卫，`"input_tokens": null` 就抛 `TypeError`；而这些调用位于 `_compute_session` 的 per-session 错误处理**之外**，异常穿透 `pool.map`，整批会话一起失败（实测：构造一条 null usage 的 transcript 即可复现）。全文件 9 处累加同类，改为统一走 `_as_int()`。顺带收掉 `int(x or 0)` 对非数值字符串的抛错。**注意 `json.loads` 默认接受 `Infinity`/`NaN`**，所以 `_as_int` 还要挡 `int(inf)` 的 OverflowError——这个洞是我自己的测试先抓到的。
- **zstd 解压失败静默归零**：`_iter_compressed_jsonl` 的每条失败路径都是裸 `return`，于是截断或半写的 store 得到 0 记录、0 信号，与"空会话"、与"会话文件已消失"（会被判为陈旧行清理）都无法区分。5 条失败路径全部改为带原因的 warning（用 zstd CLI 自己的报错文本）。
- **语义修复需要 epoch 才回流**：上述 kind 过滤改变了 `user_messages`，但指纹基于内容+epoch，不 bump 就只对新会话生效、已有 299 个 DSH 会话仍留着旧值。按 `_PARSER_EPOCH` 的既定用途 bump 到 `v6-dsh-kind-denylist-and-counter-coercion`，触发一次性全量重解析（本机 6287 会话 46s）。
- **环境枚举重复项**（与 v0.9.23.2 同批，单独记）：`known_dirs` 用 `expanduser(root).split("/")[0]` 取顶层目录，绝对路径下恒为空串 → 30 个注册环境全被当成"未知目录"再报一次，72 条结果 19 个重复。修后 51 条、0 重复。
- **回归门 +16 项**：`tests/test_dsh_adapter.py`（store 版本按数值比较、v10 胜过 v9、store 家族泛化匹配、kind 反向白名单覆盖实测全量 kind）、`tests/test_adapter_robustness.py`（9 类畸形 usage 逐一可存活、正常计数不被破坏、坏 zstd 必留痕、健康 store 仍静默）。全量 **273 passed / 1 skipped**
- 顺带核实但**未改**：单遍读与多遍路径在 claude+zcode_v2 全部 134 个文件上 stats/messages/tools/errors/flags/identity 零差异，`cache_hit_rate` 也一致（未绕过单一真源）；`topic-segmenter.py` 的 `simple_tokenize` 与 `_cjk.tokenize` **不等价**（`a-b_c/d` → 4 个 token vs 3 个），属第二份实现，已在 `_cjk.tokenize` 的 docstring 里如实标注并把差异写清，不擅自改动 `/topics` 的输出

**v0.9.23.2** — 复核轮之二：会话列举层（面板日常路径）

修完发现与构建后重新测量各入口，最慢的变成 `recall sessions`（1.63s）与 `herdr-search`（1.76s）——herdr 面板与 fuzzy 浏览都走这条。根因与检索端同源：**从文件重算索引里已经有的数据**。

- **`recall sessions` 8×**（`--limit 20` 1.63 → 0.20s；`--limit 200` 3.12 → 0.25s）：① `find_sessions` 经 `_list_from_registry` 调 `cross_tool_list_sessions`，而后者的 `summary`/`first_prompt`/`msg_count` 在调用方**一个都不用**，却全额付了 preview 代价（1.62 → 0.11s，15×；keyword 为空时可证中性，带 keyword 时保持原状）。② 每行的 `created`/`msgs`/`branch` 靠**完整解析每个 transcript** 换取，而索引里三个字段都有——1.36s（200 个文件）变成 0.002s 的索引读。
- **两层用的不是同一套会话 id**：索引键是 `{env}:{adapter_id}`，列举层返回适配器的裸 id。同一批 200 个会话，按 id 命中 **0/200**、按路径命中 **200/200**，所以索引读必须以 `jsonl_path` 为连接键（新增 `_reader.session_stats_by_path`）。旧行为下 `/recall search` 显示 `claude:04aa…`、`/recall sessions` 显示 `04aa…`，用户抄任一 id 都对不上另一半。
- **索引读 + 陈旧回退**：只有索引里没有、或文件在构建之后又长了（比较文件 mtime 与 `jsonl_mtime`）的会话才回退解析，显示的数字仍是实时的。等价性逐字段对拍：limit=20/100/200 共 320 行，**字段差异 0、顺序一致、条数一致**。
- **排序不再抖动**（真 UX bug）：`cross_tool_list_sessions` 用 `as_completed()` 的完成顺序建字典，而该顺序决定轮转起点——`sd-recall sessions --scope all --limit 20` 连跑三次从第 2 位起就不一样。改为按提交顺序轮转。
- **`discovery_only` 跨线程不生效**（本轮自己踩到并修掉）：`ThreadPoolExecutor` 不继承调用方 context，所以包在外面的 `discovery_only` 在 worker 里是空的，preview 照跑。改为每次提交各传一份 `copy_context().copy()`。若只复制一份共享 Context 给 6 个并发 worker，会抛「cannot enter context: already entered」并被逐适配器 `except` 吞掉，表现为**列表静默变短**（本次实测 200 条变 44 条）——因此同时把「全部适配器失败」从 N 条 warning 升级为一条 ERROR，避免系统性失败伪装成「没有会话」。
- **环境枚举返回重复项**：`known_dirs` 用 `expanduser(root).split("/")[0]` 取顶层目录，绝对路径下恒为空串，导致**没有任何注册环境被标记为已知**，30 个注册环境被当成「未知目录」再报一次：72 条结果里 19 个重复 env_id（dimcode/kimix/dsh/proma/…）。改为按 home 的相对路径取第一段，51 条、零重复，discovered 段只剩真正未知的目录。同时把 `as_completed` 的返回顺序固定为注册表顺序（此前 `smoke-multi-env` 每次输出顺序都不同）。
- **回归门 +9 项**：`tests/test_session_listing.py`（按路径的连接、顺序确定、跨线程 context 传播、全失败告警、环境枚举顺序）。全量 **255 passed / 1 skipped**
- 面板实测：`herdr-search` 1.76 → 0.64s、`herdr-sessions 20` 0.31s、`herdr-fuzzy-search` 0.45s

**v0.9.23.1** — 复核轮：发现阶段的丢弃式解析 + 只增不减的索引

同一口径复测上轮台账时发现，**最大的日常瓶颈不在上轮修的三处**，而在发现阶段：`list_sessions` 是给 UI 列表用的，每个会话都会解析 JSONL 头部填 `summary`/`first_prompt`/`message_count`；索引器只读 `session_id` 和 `full_path`，这些字段**一个都不用**。

- **日常无变更构建 2.08s → 0.42s（5.0×）**：新增 `echolib.discovery_only()`（ContextVar 而非模块全局——`cross_tool_list_sessions` 会跨线程扇出，全局会让并发的 UI 列表静默丢失全部预览），7 个适配器的 preview 扫描各加一行早退。`scan_sessions` 5.40s → 1.05s。整轮消融（同代码、两个空索引、开/关各建 6287 会话）：`sessions` 表 32 列**零差异**，`messages_fts` 74045 行与 `topic_boundaries` 27394 行**逐行相同**
- **`elapsed` 口径修正**：`t_start` 原先落在 `init_db`/角色回填/6337 行指纹预载/`scan_sessions` 之后，自报 0.85s 对真实 4.14s。提到函数首行后自报与墙钟一致（0.42s / 0.42s）
- **陈旧会话行清理**（`_prune_stale_sessions`）：索引此前只增不减，53 行常驻——22 行指向已删除文件，其余来自目录改名与一个把备份目录当会话环境的发现缺陷。因为只有被重新发现的会话才会被重写，这些行还连带冻结了旧 schema 的值（49 行证据停留在空串，检索时表现为"这个会话没有用户消息"）。三重护栏：仅全量扫描触发、虚拟路径与注册表根目录之外的路径一律不判、本轮列举失败的环境整体跳过（避免临时卸载/锁库被读成"用户删光了"）。实测 53 → 0，幂等
- **`~/.cc-switch` 全树漫游**：universal 兜底 `rglob` 为拿 4 个 codex 备份遍历 11.2 万文件 / 2.4GB，耗时 0.594s 并把备份发布成 4 个重复会话。`_rglob_jsonl` 按目录名剪枝后 0.017s（35×），幽灵会话归零
- **`limit=0` 约定统一**（`echolib.cap`）：11 个适配器写 `items[:limit]`，`limit=0` 即"不限量"被静默实现成"返回空列表"；4 个此前已各自修过。规则收到一处，并由 `test_discovery_only.py` 的守卫测试禁止第 12 次复现
- **Python 下限**：`skill-gap-finder.py` 用 `@dataclass(slots=True)`（需 3.10），在 macOS 自带 `python3`（3.9.6）上 import 即 `TypeError`，该命令已注册在 `herdr-plugin.toml`。去掉 `slots` 后恢复；`README`/`CLAUDE.md` 的"Python 3.6+"改为真实下限 3.9
- **其他低级修**：`_schema.py` 迁移版本号按字符串比较（`'10' > '9'` 为假，第 10 个迁移会被永久静默跳过）改为整数比较；`_builder.py` 重复的 `import logging as _logging` 块合并；`collapse_near_dups` 删除从被拒的 keep-longest 消融里遗留、无人传入的 `keep=` 参数；`sd-recall` 里把 keep-longest 当成折叠依据的注释改为与实际一致（保留首个，不按长度择优，因为门禁实测原件更小）；`_should_compact` 从内联布尔提为命名函数（原测试逐字抄写该布尔，只证明副本与自身一致）；`universal_list_sessions` 的排序键去掉多余 stat（此前每文件 4 次）
- **实测覆盖**：全量测试 239 passed / 1 skipped；20 个脚本在 Python 3.9.6 下全部可导入；`herdr-plugin.toml` 注册的 12 条命令逐条跑通（含 bash 与 `compileall`）；主 CLI 28 项子命令 rc=0；`smoke-multi-env.py` 15 个已适配环境全部列举正常；12 个子技能 SKILL.md front-matter 合法、`commands/*.md` 引用零缺失
- **未处理（留待确认）**：`~/.claude/.session-digger/` 下 3 个 `index.db.bak-*` 共 761MB 陈旧备份（reports/`precjk`/`prerebuild`/`prefix`），非代码生成，未擅自删除

**v0.9.22** — P1 优化轮：检索证据覆盖 0.9→1.0

- **消融 A（采纳）**：`scripts/retrieval_utils.py` `stream_contains` 替换兜底检索的 50KB 头窗口单次读——头窗口快路不变，未命中后字节级流式扫描（每文件上限 2MB、跨块缝重叠、命中即退），接入 `sd-recall.find_sessions`。实测 recall 0.9→1.0（深埋证据找回），检索 +0.45ms，证据覆盖零回退
- **消融 B（部分拒绝，按 PR#1 准则）**：keep-longest 近重复折叠被门禁拒绝（语料实测原件比填充副本小，折叠丢证据）；收敛为仅折叠字节级完全相同的真拷贝（`collapse_near_dups`），伪重复不折叠且拒绝逻辑用回归测试钉死
- `scripts/footprint.py` 升级为生产同路径（stream_contains + 折叠），新增 `duplicate_rate_raw`/`collapsed_total`/`dup_note` 指标；基线包络重录（recall 冻结 1.0）；新增 `tests/test_retrieval_p1.py` 10 项（含上限 off-by-one 修复的诚实 miss 门）

**v0.9.21** — 测量落地轮 + wechat-digger 媒体附加层（v0.0.8.1-pub）

- **测量框架落地**（PR #1/#2 方向）：`scripts/quality_corpus.py` 固定评估语料（7 场景 14 文件：短/长/重复话题/噪音工具轨迹/缺元数据/多语言 CJK/近重复，证据标记全埋点）+ `scripts/footprint.py` 九项足迹指标（摄取/解析/检索耗时、候选数、重复率、召回、证据覆盖、上下文规模、峰值内存、端到端延迟）+ `tests/test_quality_baseline.py` 质量门禁 8 项（基线包络冻结，改动须显式重录）。**首轮量化发现**：检索兜底路径 50KB 头窗口外深埋证据召回 0（语料 s2-long 实测）；近重复副本未去重（dup_rate 0.667）。两 PR 文档已合并进 `docs/engineering/` 并补实施落点
- **测试基线修复**：`test_grok_quality.py` 4 项失败归因 = v0.9.14 适配器迁移加严 `sessionUpdate` 过滤后夹具未跟进（实测本机 271/271 真实账单行 100% 带 `sessionUpdate`，生产语义正确）；夹具对齐真实格式并新增「非 turn_completed 不计账单」回归锁。`test_combo_coverage.py` 2 项失败 = 校验器盲区（漏扫子技能脚本目录与包目录），补扫修复
- **`skills/wechat-digger/` → v0.0.8.1-pub**：`extras` 命令族（语音元数据/SILK 导出、转账红包、好友申请）+ V2 `.dat` 图片离线还原（`images-discover`/`images-decrypt`，pycryptodome 为可选依赖，核心分析层保持零 pip）+ vault `--start/--end` 日期窗；边界修订=媒体文件离线解码不属于被剔除的「密钥提取/SQLCipher 解密」，解密栈仍不分发（契约测试锁死）；README 增「导出与转写口径」（JSON/Markdown 内置，PDF 走外部转换，语音转写接外部转写器）
- 测试基线：主套件 184 项全绿，wechat-digger 196 项通过/21 跳过

**v0.9.20** — 双线合并 + wechat-digger 子技能收录

- **`skills/wechat-digger/`**（发布版 v0.0.8.0-pub）：微信本地数据识别与分析子技能——全史检索/群画像/商机跟进/噪音群治理/跨会话成文；只含分析层与只读查询件，密钥提取与 SQLCipher 解密栈不分发，`keys`/`decrypt`/`refresh` 如实报 `tool_missing`；数据接入 = 用户自备已解密 vault / wx-cli / 导出文件三来源；`skills/wechat-digger/SYNC.md` 记录拷贝白名单、脱敏对照表（真实会话锚点→虚构名）与校验清单，供后续版本同步
- **双线合并**：主线（0.9.13→0.9.19）与 GPT 工作区旁线（CJK 中文检索、`/deep-analyze`、DSH+ZCode v2 适配、适配器修复、5 个子技能收录）在 07-15 分叉后各自演进，版本号曾双向撞车（旁线的 0.9.14/0.9.17/0.9.18 与主线不同内容）；本版把旁线工作全部并入主线，旁线条目原文保留于文末「旁线」段
- README 徽章与 frontmatter 同步至 0.9.20
**v0.9.19** — Kimix CLI 适配

- 新增 `kimix` 环境适配器（`_adapters_kimix.py`）：复用 Grok Build 适配器，session 格式几乎一致
- `ENV_REGISTRY` 添加 `kimix`（`~/.kigi/sessions/`）
- 支持 `/usage` 跨环境汇总 Kimix CLI token 用量
- Kimix 是基于 Grok Build 的非官方 Kimi Code CLI 社区构建版

**v0.9.18** — 子技能调优：专精优先协议 + deep-analysis 挂 combo + `common_paths`（index.db 定位）；目标「新主路由+调优子技能 > 新主路由+老子技能」。归档 `archive/pre-subskill-tune-20260717`。

**v0.9.17** — 三瓶颈硬化：hub 边界 / usage policy / adapter tier

- **B0 卫生**：`_knowledge` 补 `time`；Grok `_empty_stats("grok")`；ZCode 死代码删除；DimCode 异常打 debug；文档 `echolib/` 对齐
- **瓶颈①**：`_empty_stats` → `_helpers`；`ENV_REGISTRY` / `KNOWN_UNADAPTED` / `scan_*` → `_registry_data`；拆分适配器不再惰性依赖 hub
- **瓶颈②**：新增 `_policy.PROVIDER_POLICY`；`attach_cache_hit_rates` 可按 agent 解析口径；`finalize_session_stats`
- **瓶颈③**：`ADAPTER_TIER` + `tier_supports`；`build_cache_hit_tables(enforce_usage_tier=True)` 半适配不进 usage 主表；`scripts/smoke-multi-env.py`
- 测试：`tests/test_provider_policy.py`；全量 137 passed

**v0.9.16** — 主命令精简 + `/usage` 跨环境 token 可观测

- 路由表收敛为 5 主命令：`/recall`、`/usage`、`/reflect`、`/analyze`、`/dashboard`
- 新增 `/usage`：`zcode_aggregate_model_usage(mode="family")` + `grok_aggregate_model_usage()`；按模型展示 token 与 cache_hit_rate
- `/recap`/`/topics`/`/topic-scan` → `/recall`；`/trend`/`/optimize` → `/reflect`；`/lessons` → `/analyze`
- 其余入口下沉「子命令」表，仍可通过 flags / 命令文件 / 专项 skill 调用

**v0.9.15** — 主对话 / 子代理分列

- 对用户只展示「主对话」「子代理」；内部标记与路径不进报表
- `session_role_label` + `build_cache_hit_tables(split_role=True | role_filter=主对话)`
- 索引可区分时：DimCode / Grok 等主对话与子代理命中率分列，避免混算掩盖波动

**v0.9.14** — 缓存报表口径固化 + WorkBuddy 入库门控

- **报表契约**：主表仅会话均命中率；异常单独列；禁止默认加权污染理解
- **`build_cache_hit_tables` / `mean_cache_hit_rate` / `cache_rate_eligible`**
- **WorkBuddy** 强制适配器路径（修复 total 有数、cache/model 全丢）
- `references/cache-report-rules.md`；trend 聚合同步丢 rate≤0

**v0.9.13** — 增量索引与缓存命中语义：一行修一类数据准确性

- **`input_includes_cache` 显式语义**：`compute_cache_hit_rate` / `attach_cache_hit_rates` 支持适配器声明 token 口径；Claude/Kimi Code=非缓存 leg，Grok/ZCode/DimCode/Codex=总量含缓存，消灭自动推断在边界命中率上的误分类
- **Codex token 真源修复**：读 `total_token_usage` 嵌套字段 + 累计快照取 max（非 sum）— 此前大量会话 input/cache 恒为 0
- **Single-pass 边界门**：ZCode/Grok/Codex/Kimi wire 强制走适配器，杜绝单遍 JSONL 误算/漏算 token
- **DimCode 按会话指纹**：不再用整库 mtime 作全员失效键；任意会话写入不再触发 ~N 全量重索引
- **增量批处理**：指纹/tags 一次加载；`_PARSER_EPOCH` 解析器世代，兼容性修复后自动一次性重解析
- **Kimi standalone** 重新挂入 ENV/适配器；wire 路径接受文件或目录；StatusUpdate token 提取
- 单测：`tests/test_cache_and_incremental.py`；全量 114 passed
### 旁线条目（GPT 工作区开发线 2026-07→09，编号曾与主线撞车，原文保留）

**旁线 v0.9.18** — 子技能收录 wechat-digger（脱敏发布版）+ 版本统一

- **`skills/wechat-digger/`**：微信本地数据识别与分析子技能（发布版基线 v0.0.8.0-pub）。全史文本检索、群画像、关系网络、商机跟进、噪音群治理、跨会话成文；**只含分析层与只读查询件**——密钥提取（extract_keys）与 SQLCipher 解密栈不分发，`keys`/`decrypt`/`refresh` 子命令如实报 `tool_missing`，数据接入=用户自备已解密 vault / wx-cli / 导出文件三来源。`skills/wechat-digger/SYNC.md` 记录拷贝白名单、脱敏对照表（真实会话锚点→虚构名）与校验清单，供后续版本同步
- 主路由表与 DO NOT 同步更新（`/import` 不做微信解密 → 指向 wechat-digger）；`tests/` 208 通过基线上再加 wechat-digger 发布版 209 用例（live 锚点默认跳过）
- 版本号统一：README 徽章与 frontmatter 同步至 0.9.18（此前徽章停在 0.9.6、frontmatter 停在 0.9.14）

**旁线 v0.9.17** — deep-analyze：取数 → 模型自发分析 → 结论回存的一站式命令

- **`/deep-analyze` 命令**（`commands/deep-analyze.md` + `scripts/deep_analyze.py`）：一条命令产出有界数据包（默认 ≤12KB ≈ 3K token）——全局概况（全库+时间窗双口径、环境分布、按日趋势）+ 重点会话（规模/主题/用户消息样本/工具错误画像）+ 已有分析结论标注（避免重复分析）。模型拿到数据包后按四视角框架自发分析（工作主线/摩擦模式/决策转向/盲区建议），结论经 `save-summary` 回存摘要缓存形成复利，虚拟 scheme 会话（dimcode://）跳过回存
- **索引读取层** `index_builder/_reader.py`：`quick_stats_from_index`/`evidence_from_index` 从 sd-recall.py 提升为 canonical 层（sd-recall 改 import，删除本地副本），新增 `recent_sessions`（时间窗/环境/FTS 关键词/质量排序选会话）、`global_aggregates`（totals+按环境+按日一次连接出齐）、`has_summary_cache`（.summary.jsonl 探测）；`DECISION_PATTERNS` 单一真源迁入，sd-recall 反向引用
- **数据包卫生**：用户消息样本过滤系统注入（`<notification>`/`<system-reminder>`/`Image read from`/continuation 摘要）；`tool_errors_json` 进入选会话查询列（修复错误画像恒空的字段缺失）
- **测试**：`tests/test_deep_analyze.py` 新增 7 用例（选会话各分支/聚合形态/注入过滤/预算截断）；全套 137 通过
- **严谨性双门**（评审后补强）：①已有分析结论不再自动跳过——数据包列出结论条数/最新时间/要点，并用 `source_mtime` vs 索引 `jsonl_mtime` 判定「分析之后会话是否有新内容」，命令纪律改为**先向用户展示已有结论并询问是否重新分析**；②索引新鲜度门禁——取数前检查 `last_build`（默认 >6h 超龄自动增量重建，`--max-age-hours` 可调/-1 跳过），实测门禁捞回并行会话新产生的 369 个会话与 dsh/zcode_v2 新环境数据；③`global_aggregates` 时间窗口径修复（此前 totals 未按窗口过滤，窗口行显示全库数字）。全套 139 通过

**旁线 v0.9.16** — 中文检索从 0 到 1：CJK 分词 + 全文召回窗口 + 索引并行化

- **中文 FTS 修复**（最大瓶颈，实测「迁移」「数据库」等查询命中率为 0）：FTS5 unicode61 tokenizer 把连续 CJK 串当整块 token，「迁移」永远命中不了「数据库迁移完成」。修复 = 写入侧 `split_cjk`（CJK 逐字切分进 FTS）+ 查询侧 `build_match_query`（查询词对称重建为逐字 phrase，英文 token 加前缀 `*`），两函数同在 `index_builder/_cjk.py`，写入/查询/读回展示（`uncjk`）三方契约由 `tests/test_cjk_search.py` 金标门禁钉死。重建索引后真实数据验证：「去AI味」63 会话、「半调海报」「数据库迁移」均秒级命中（旧版全 miss）
- **查询串安全化**：旧 `safe_kw` 只处理引号冒号，`NEAR`/`OR`/括号等 FTS5 语法直通用户输入（遇特殊字符即静默报错回退）。`build_match_query` 只输出引号包裹的字面 token（布尔关键字也被字面化），注入不可能
- **召回窗口 500→16k**：各 adapter 的 `text[:500]` 硬截断（zcode/grok/workbuddy/cursor/universal 共 30+ 处，claude 不截——语义分裂）全部撤除，`extract_messages` 统一返回全文；FTS 端单点截断 `index_builder._schema.FTS_TEXT_CAP = 16000`。此前 13350 字符的回复只有前 500 字符可检索。展示层（`[:150]`/`[:300]`）本就自带截断，行为不变。索引 db 91→149MB
- **FTS 三查询点统一**：`sd-recall._fts_search`、`index-builder.search_fts`、`remember` skill 统计全部接入 `build_match_query`；`search_fts` 读回文本经 `uncjk` 还原
- **语义修正**：FTS 无命中返回 `[]`（不再回退全盘文件头扫描——那是几秒的无谓 IO 且结果更差）；索引缺失/查询失败返回 `None` 才回退，且 stderr 显式提示（旧 `except: return None` 静默吞错，故障表现为「搜不到」）
- **检索提速：索引数据复用**：`cmd_search` 命中路径不再逐会话重新解析 JSONL 文件（旧路径每会话 2 次全文件解析）——stats/消息/错误聚合一次 SQL 从 `sessions`/`messages_fts` 表直读，仅索引外的会话与 `--deep` 模式回源文件；`cmd_stats` 改纯 SQL 全量统计（旧版最多只统计 1000 个且逐文件解析），并标注 errors 口径（工具调用失败数，非解析错误）
- **索引构建并行化**：单会话计算提取为 `_compute_session`（编排/计算分离），`ProcessPoolExecutor` 并行（`SESSION_DIGGER_JOBS` 可调/强制串行，<24 会话自动串行）。zcode 314 会话实测 13.44s→3.85s（3.5×）；cross 全量 3758 会话 51.9s（旧代码同口径折算 ≈96s+，且那还是未加全文窗口的旧版）
- **timestamp 防御**：`cmd_search` 四处 `ts[:19]` 对 None/非字符串崩溃 → 统一 `_ts19()`
- **测试**：`tests/test_cjk_search.py` 新增 13 用例（分词往返/查询构造/注入安全/FTS 语义/金标查询）；全套 127 通过

**已知边界（记录待办）**：zcode transcript 断流重连（`stream_recovery_anchor_created`）后 streaming 重组可能丢中段文本，权威 `model_complete` 又被防重复逻辑跳过——真实案例：某会话 12601 字符回复的第 9740 字符处「周报」不可检索。修复需 2-pass 重构（先收集 complete 再流式），未纳入本版。「verdaccio」类词 0 命中为数据源边界（该词仅存在于 thinking/tool_use 块，非对话文本）。dimcode 虚拟会话（`dimcode://`）的索引 project_path 是截断产物（`dimcode:`），真实 cwd 未入索引，`session_in_cwd` 对 `://` 路径一律 False——`--scope current` 下 dimcode 会话（最大环境）永远 miss；修复需 scan/worker/row/cwd 判定 5 处契约联动，独立成轮。

**旁线 v0.9.15** — 契约一致性修复：extract_tools 全线打通 + kimix 路径查找纠错

- **kimix extract_tools 修复**（生产级 TypeError）：`kimix_extract_tools` 原本只有 `(path)` 一个参数，dispatch 层按通用四参契约调用即抛 `TypeError: kimix_extract_tools() got an unexpected keyword argument 'tool_filter'`；且内部错误地调用了 `_claude.extract_tools(path, agent="kimix")`（该函数无 `agent` 参数，二次 TypeError）。现改为全签名 `(path, tool_filter, errors_only, limit)` 并委托 `grok_extract_tools`（双文件 events+chat_history 关联），接受会话目录或 .jsonl 文件路径。真实 ~/.kimix 606 个会话全部可提取工具调用
- **删 dispatch 特判**：`dispatch_extract_tools` 的 `if agent == "grok"` 特判删除——`grok_extract_tools` 内部新增文件→目录自适应，契约统一为「四参 + 文件路径」；grok 真实路径 642 会话验证无回归
- **kimix_session_path 纠错**：原实现委托 `_grok_find_session_dir`，在 `~/.grok/sessions` 里找 kimix 会话（永远 miss）；重写为扫描 `~/.kimix/sessions/<group>/<sid>`，签名对齐 `(cwd, session_id=None)`，cwd 精确匹配 summary.cwd 优先
- **reasonix_list_sessions 返回类型对齐**：原返回 dict 列表，是 cross_tool 中 `isinstance(s, dict)` 双分支的唯一内置来源；改为返回 `SessionMeta`，cross_tool 的 dict 分支降级为仅兜底外部插件并更新注释
- **死代码清理**：zcode `limit*3` 空 if+pass、kimix 未用 `model` 变量、kimix_extract_tools 错误 docstring
- **测试**：`tests/test_kimix_adapters.py` 新增 3 用例（extract_tools 委托、dispatch 文件路径全链路、session_path 解析）；全套 113 通过

**旁线 v0.9.14** — Kimix 0.1.16 数据根收敛 + 每请求缓存指标数据源

- **数据根收敛**：`ENV_REGISTRY`/`_helpers.KIMIX_DIR` 统一为 `~/.kimix`（此前指向 `~/.kigi`，那是独立 CLI Kigi 的数据根，导致 Kimix 会话全部索引错位）
- **缓存命中率进索引**：schema v1→v2 新增 `cache_hit_rate` 列，`index-builder` 落库（此前只有 latest 分支有此能力，真源丢失）
- **每请求缓存指标数据源**：新增 `kimix_cache_metrics()`（读 `~/.kimix/metrics/cache_hit-*.jsonl`，按日聚合）与 `kimix_unified_cache_index()`（读 `~/.kimix/logs/unified.jsonl` 的 `shell.turn.inference_done`，带 sid 会话归属，mtime 缓存）；`kimix_session_stats` 在 updates.jsonl 缺 usage 时用 unified 精确数据回填，并打 `cache_source` 标记
- **索引构建**：`build_index` 对带 `cache_metrics` 的适配器自动写 `env_cache_metrics_*` 到 index_meta
- **路径路由修复**：`_env_path_markers()` 加 `~/.kimix/sessions` 标记——此前 Kimix 的 `chat_history.jsonl` 被文件名/结构线索误判为 grok，dispatch stats 全空
- **Grok 注册接线**：`ADAPTER_REGISTRY["grok"]` 改用 `_adapters_grok.py` 增强版（signals + updates.jsonl 计费 usage + 缓存命中），删除 `_adapters.py` 内 3 个无 usage 解析的旧副本；`<user_query>` 剥离逻辑补回增强版
- **Kigi 走通用适配器**：`.kigi` 不进 `ADAPTER_REGISTRY`（无专用适配器），仅加入 `KNOWN_UNADAPTED` 轻发现；`_scan_via_adapter` 对 universal 按环境根目录限定扫描（此前全屋扫描 500 条截断，`.kigi` 永远扫不到），sid 从路径派生（会话 uuid，避免 stem 碰撞）；`universal_list_sessions` 过滤 grok 家族辅助文件（updates/events/rewind_points/…）
- **重复注册表清理**：`_adapters.py` 末尾重复的 `ENV_REGISTRY`（缺 kimi/kimix，覆盖真源）删除，单一真源归 `_registry_data.py`
- **normalize 修复**：`_scan_via_adapter` 适配器返回会话目录时经 `normalize_session_path` 落到具体 JSONL（此前 fingerprint mtime=None 导致数百条 error）
- **测试**：新增 `tests/test_kimix_adapters.py`（6 用例）与 `tests/test_kigi_universal.py`（3 用例）；全套 110 通过。重建后 errors 579→14

**旁线 v0.9.13** — 工程质量修复：崩溃 bug + 重复循环 + 数据准确性

- **`_knowledge.py` 崩溃修复**：补齐 `import time as _time`（`save_analysis_result` / `load_analysis_result` / `build_summary_index` 调用即崩溃）
- **`_knowledge.py` `build_summary_index` 数据驱动化**：硬编码 4 环境路径 → `ENV_REGISTRY` 驱动（随注册表自动覆盖全部已知环境）
- **`_adapters_zcode.py` `zcode_session_path` 修复**：合并双层搜索为单层，消除每次 `sess_dir` 迭代后重复扫描全部 agent_dir 的 N×M 冗余
- **`_adapters.py` `_grok_session_stats`**：`_empty_stats("unknown")` → `_empty_stats("grok")`；移除冗余 `import os`；移除 `_grok_extract_messages` 内冗余 `import re`
- **SKILL.md**：四层模型 `echolib.py` → `echolib/`（v0.9.7 拆包后遗留）
- 单测：`tests/test_improvements.py` 新增崩溃防护 + adapter 注册完整性

**v0.9.12** — 一行修一类：scope 边界回归修复 + 发现层统一走注册表

- **`session_in_cwd` 抽到 `echolib._helpers`**：Claude dash / Grok URL 统一段边界；`$HOME` 永不命中全库；`bar` 不再误匹配 `bar-baz`（旁支 292ad0a 修复此前未合入 main）
- **`sd-recall find_sessions` 去硬编码**：不再只扫 claude/grok/kimi；`--agent` 动态取自 `ADAPTER_REGISTRY`（codex/cursor/zcode/…）
- **`cross_tool_list_sessions.agent` 改用 registry id**（`agent_display` 保留展示名），消灭下游对显示名的脆弱依赖
- **`normalize_session_path`**：适配器返回目录时落到 `chat_history.jsonl` 等具体文件
- **UX**：`sessions --scope current` 空结果时 stderr 提示 `--scope all`；`format-detector --help` 不再当路径
- **子技能回填**：`env-doctor` / `native-diag` / `deep-analysis`（SKILL 路由已写但 main 安装缺失）；native-diag 路径拼接补 `/`
- 单测：`tests/test_session_scope.py`

**v0.9.11** — Universal SchemaProbe 智能化：一通百通未知环境

- **路由置信度门控**：禁止用正文里的「claude/sonnet」等子串劫持专用适配器；路径优先 → 结构签名 → universal
- **SchemaProbe 家族探测**：`nested_message` / `nested_payload` / `flat_role` / `history_display` / `summary_card`
- **通用解析**：`<user_query>` 剥离、system/env 噪声过滤、display 历史日志、摘要卡 intent/actions
- **KNOWN_UNADAPTED** 扩展 newmax/proma/iflow/deepcode/codebuddy/commandcode 等发现位
- 单测：`tests/test_universal_probe.py`

**v0.9.10** — WorkBuddy / Trae CN 解析对齐 Claude·Codex·Cursor 水准

- **WorkBuddy**：修复 Exit Code 正则双转义（错误永不计数）；`_empty_stats` 占位 model 可覆盖；`<user_query>` 剥离；`reasoning` 思考块；cwd 过滤；ai-title → summary
- **Trae CN**：字面 `\\n` → 真换行；slug 去 `session_memory_`；时间 ISO 化；多日 shard 合并取最新 path；项目 slug 解码；outcome 失败软标错误
- 单测：`tests/test_workbuddy_trae_adapters.py`

**v0.9.9** — ZCode / Kimi Code 解析对齐 Claude·Codex·Cursor 水准

- **ZCode**：`modelRef` 字典解析（不再把 toolName 当 model）；`model_streaming` text_delta 重装；用量 camelCase；`tool_batch_complete` 错误；SessionMeta 列表 + DB 标题；slug=agent_*
- **Kimi Code**：assistant 只计 text 回合（think 不灌水）；`llm.request`/`usage.record` 取 model；slug=session uuid；按 turnId 合并 content.part；`workDir` cwd 过滤；单遍 tool join
- 单测：`tests/test_zcode_kimi_adapters.py`

**v0.9.8** — 借鉴 Grok Build resume-session：Claude/Codex/Cursor 适配增强

- **Cursor 适配器上线**：`agent-transcripts` JSONL + Desktop `state.vscdb`；`<user_query>` 剥离、`tool_use` 计数
- **路径表驱动 `detect_agent_type`**：恢复目录 marker + 文件名线索 + 内容签名；Cursor/Codex 不再误入 universal
- **Codex**：`CODEX_HOME` 双根扫描、完整 UUID 提取、`.jsonl.zst` 透明读取、`local_shell_call` 工具识别
- **一行消一类问题**：`_iter_jsonl` 透传 zstd；`_fast_find_jsonl` 返回 list（消灭 `len(generator)`）；`cross_tool` 默认排除 universal + 按环境 round-robin
- **修复**：`GROK_SEARCH_DB` 未定义导致 Grok list 崩溃；`output_text` 块提取回归

**v0.9.7** — 工程质量：死代码清理 + JSONL 解析归一 + 预存 bug 修复

- 5 模块共 28 个未使用 import 清理（`_helpers`/`_adapters`/`_claude`/`_models`/`_knowledge`）
- `iter_records()` + `session_stats()` 复用 `_iter_jsonl()`，消除 JSONL 解析路径重复
- 修复 `_adapters.py` 的 `SessionStats` 类型注解未导入（添加 `from __future__ import annotations`）
- 修复 `session_stats()` 中嵌套 `is_error` 永远不被计数（error 检测移到 `continue` 之前）
- 21 个公共函数补全 docstring
- CLAUDE.md 架构描述更新（`echolib.py` → `echolib/`）

**v0.9.6** — Reflect 使用回顾：可视化升级 + 单环境数据隔离 + 主题对比度
- `reflect-report` 首页用量总览（用时 / Token / 模型偏好双栏），借鉴数据报告呈现
- 核心发现按**当前时段 + 当前环境**现算，进入 Kimi 等子页不再混入 Claude 等全库汇总
- 主题：跟随系统 / 奶油暖色 / 深褐 / 纯黑 / 冷蓝 / 墨纸；环境色只标侧栏，不劫持主题名
- 字色与强调色对比度校准（约 4.5:1）；中文标签（要盯/留意…）与读数免责
- Hallmark 可视化层：design-tokens 主题体系 + 自包含 HTML 报告

**v0.9.5** — 工厂模式消除 10 个重复 find_jsonl 函数 + dispatch 特化分支消除
- `FIND_JSONL_REGISTRY` 数据驱动：`_project_based_find_jsonl()` + `_tiered_find_jsonl()` 两个工厂
  替代 10 个重复的 `_xxx_find_jsonl()` 函数（-88 行，-35%）
- `dispatch_extract_tools()` 消除 `if agent == "grok"` 特化分支：适配器内部统一入参
- `dispatch_extract_messages()` 消除 `no_tools` 参数含义分歧：适配器路由不依赖 Clsude 专用参数
- `KNOWN_UNADAPTED` 消除冗余：5 个已适配环境移至 `ENV_REGISTRY`，不再与适配器表并列维护
- `ENV_REGISTRY` 补全 5 个新适配环境 + 双目录同步机制

**v0.9.4** — 路径匹配边界检查 + 适配器解析语义修复
- `_session_in_cwd()` 全面边界修复：`$HOME/bar` 不再误匹配 `$HOME/bar-baz` 的会话
  - 移除 `dash[1:] in ps` 冗余条件、`dash in ps` 和 `encoded_cwd in ps` 增加段边界检查（后一字符须为 `/` 或 `.`）
  - basename fallback 只保留带明确路径分隔符的标记（`/bar/`、`%2Fbar%2F`），移除 `-bar-`、`_bar_` 等会在 segment 名称内部误匹配的标记
- `detect_agent_type(path=None)` 不再返回 `"both"`（非有效 adapter 名），改为返回 `existing[0]`（最具体的环境，因 `_ENV_PATH_MARKERS` 按特异性降序排列）
- 根因：路径编码中 `-` 既是 segment 分隔符，也是 segment 名称的合法字符（如 `bar-baz`），简单 substring 匹配无法区分

**v0.9.3** — 高杠杆工程优化
- `detect_agent_type()` 数据驱动重构：13个重复 if-block → `_ENV_PATH_MARKERS` 单一表驱动，新增环境零改核心代码
- `format-detector.detect_one()` 惰性读取 + 提前终止：仅读前40行（非全文），高置信度(≥8)立即返回
- `iter_records()` 异常安全加固：OSError 不再导致未处理崩溃
- `_make_simple_list_sessions()` 性能提升：filesystem mtime 替代 JSONL 首行解析（O(1) vs O(N)）

*session-digger v0.9.19 — 跨环境会话挖掘 + 子技能编排 + 本机使用回顾 + Kimix CLI 适配*
