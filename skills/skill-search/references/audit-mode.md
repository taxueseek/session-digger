# Audit Mode

创建与审计是同一 lifecycle 的两个阶段。当目标是已有 skill 时，切到 Audit Mode：诊断、量化、原子修复，方法与 Create Mode 共用一套判断纪律（Three Gates、问题分类、description 纪律），不维护第二份方法树。

## When to enter Audit Mode

- 用户要「审计 / 体检 / token 太大 / 太慢 / 架构有问题 / 重构 skill」
- creator 写完要做诊断交接
- 自诊断：目标 = 本 skill 路径

NOT: 从零创建 → Create Mode；打磨发布 → Publish Flow。

## Flow

1. **确认目标与深度**：路径；快速扫描 / 深度诊断 / 诊断+执行（默认）。
2. **架构扫描**：列文件按行数排序；读 SKILL.md；大脚本看签名。
3. **前置三问**（同 Three Gates 精神）：没它会变差吗？用 5 次以上？模型自己会吗？
4. **消解漏斗**（任一层消解则停）：
   - 需求真实性 → 跑 prior-art：`python3 scripts/research_prior_art.py "<name+desc 意图>" --name <skill> --local --digger --summary`
   - 触发词精确度 → description 对照相邻 skill
   - 前提假设 → 前置知识/依赖是否成立
   - 主要矛盾 → 根因
   - 有效性 → 德鲁克三查
5. **问题分类**：功能 A → 效率 B → 架构 C → 触发 D；先功能后效率。
6. **量化报告** → 备份 → 原子修改 → 验证。

## Deep material (read on demand, not all at once)

| 文档 | 时机 |
|------|------|
| `references/audit/diagnostic-framework.md` | 漏斗细节、报告模板 |
| `references/audit/engineering-patterns.md` | 执行改进 |
| `references/audit/design-patterns.md` | 架构问题 |
| `references/audit/anti-patterns.md` | 触发/反模式 |
| `references/audit/quality-checklist.md` | 验证 |
| `references/audit/skill-patterns.md` | 重构建设模式 |

## Handoff block (for the final response or commit)

```
TARGET: <skill path>
AUDIT_VERSION: 3.1
TOTAL_ISSUES: N
BY_SEVERITY: P0={n} P1={n} P2={n}

SEEK_ACTION: reuse|adapt|build
SEEK_TARGET: <path|name|url|->
SEEK_EVIDENCE: validated|hypothesis|missing_evidence|-
SEEK_SUMMARY: <第 1 层 prior-art 一句结论>

ISSUES:
- [AUD-001] [P1] SKILL.md:42 | … | saving: ~80

SUMMARY:
{一句话}
```

## Relationship with Create Mode

- 共用 prior-art 引擎、问题分类、description 纪律、渐进披露习惯。
- 路由：description 触发分流；不维护第二份审计方法正文。
