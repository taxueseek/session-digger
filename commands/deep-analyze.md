---
name: deep-analyze
description: |
  Pack index data, then analyze it yourself: one command hands you a bounded
  evidence pack (global aggregates + top sessions + user-message samples) and
  you decide where to dig. Conclusions get saved back into the summary cache.
  Triggers: "deep analyze", "自发分析", "深度分析会话", "分析我的工作", "这段时间怎么样".
argument-hint: [--days N] [--agent NAME] [--keyword KW] [--top N] [--sort quality|recent] [--min-messages N]
allowed-tools: Bash, Read
---

# deep-analyze — 取数 → 自发分析 → 结论回存

Arguments: $ARGUMENTS

**Script path discovery:**
```bash
SD_ROOT="${SESSION_DIGGER_ROOT:-${CLAUDE_PLUGIN_ROOT:-${HERDR_PLUGIN_ROOT:-}}}"
[[ -z "$SD_ROOT" ]] && SD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." 2>/dev/null && pwd)"
if [[ -z "$SD_ROOT" || ! -f "$SD_ROOT/scripts/sd-recall.py" ]]; then
  for _c in \
    "$HOME/.agents/skills/session-digger" \
    "$HOME/.claude/plugins/session-digger" \
    "$HOME/.claude/skills/session-digger" \
    "$HOME/.grok/skills/session-digger"
  do
    [[ -f "$_c/scripts/sd-recall.py" ]] && SD_ROOT="$_c" && break
  done
fi
# Use $SD_ROOT/scripts/<script> in subsequent commands.
```

## 第一步：取数（确定性，一条命令）

```bash
python3 $SD_ROOT/scripts/deep_analyze.py $ARGUMENTS
```

产出有界的 Markdown 数据包（默认 ≤12KB ≈ 3K token）：全局概况、重点会话
（规模/主题/用户消息样本/工具错误画像）、哪些会话已有历史分析结论（含
结论时间与「之后是否有新内容」的新鲜度对比）。取数前会自动检查索引新鲜度
（默认 6 小时，`--max-age-hours` 可调，超龄自动增量重建——保证数据不过期）。
数据包就是完整素材——**不要再去解析原始 JSONL**，那是这条命令要替你省掉的
几千倍成本。只有当某个结论必须看原始细节才能下时，才用数据包里给出的
「会话文件」路径做精确的定点读取。

## 第二步：自发分析

拿到数据包后，你自主决定分析路径——不必逐项平均用力，发现值得深挖的
线索就顺着走。以下视角按常见价值排序：

1. **工作主线**：这段时间的主题是什么？哪些是真工作（有产出、有推进），
   哪些是空转（反复重试、中断、无结论）？
2. **摩擦模式**：反复出现的问题——同类工具错误、重复配置、来回返工。
   每个模式给出会话证据。
3. **决策与转向**：用户在哪些会话里改变了方向？这些转向透露什么偏好
   与约束？
4. **盲区与建议**：做了但没做完的；该出现但一直没出现的。给出可执行的
   下一步，最多 3 条。

纪律：

- 只归纳数据包内可见的证据；超出证据的推断明确标注「推测」。
- 数字一律引用数据包原文（错误率等已实算），不要心算新比例。
- 每个结论标注来源会话 id，可核查。
- **已有分析结论的会话**：数据包会列出结论条数、最新时间、要点，以及
  「分析之后会话是否又有新内容」。先向用户展示这些已有结论，**询问用户
  是否重新分析**，按用户决定执行——不要因为存在结论就自行跳过。标注
  「⚠ 分析之后又有新内容」的会话，默认建议重新分析。

## 第三步：结论回存 + 报告

对深入分析的每个**文件型**会话回存结论（`dimcode://` 等 `://` 开头的
虚拟会话没有文件路径，跳过回存）：

```bash
python3 $SD_ROOT/scripts/sd-recall.py save-summary "<会话文件路径>" \
  "<≤500字分析结论>" --query deep-analyze --agent <agent> --tier periodic
```

tier 选择：认知规律（跨项目长期成立）→ `permanent`；阶段性结论/偏好 →
`periodic`；仅本次任务相关 → `once`。

最后向用户输出完整分析报告——报告本身直接给出，不截断、不省略要点。

## 参数

- `--days N`：时间窗（默认 7，`0`=全部）
- `--agent NAME`：限定环境（zcode / dimcode / kimix / ...）
- `--keyword KW`：FTS 关键词筛选（走中文可用的全文索引）
- `--top N`：重点会话数（默认 10）
- `--sort recent`：按时间而非信息量排序
- `--min-messages N`：滤掉琐碎会话（默认 ≥4 条消息）
