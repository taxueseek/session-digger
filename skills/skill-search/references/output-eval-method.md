# Output Eval Method

Trigger eval 证明“会不会叫对 skill”。Output eval 证明“叫对以后结果有没有变好”。

## 什么时候需要 output eval

- skill 会生成公开内容、报告、代码、发布物或账号操作。
- 输出质量高度依赖规则，而不是单纯整理文件。
- skill 要进入 Production、Library 或 Governed 模式。
- 用户已经指出过同类失败，需要防回归。
- README、发布描述或脚本输出会影响外部安装者判断。

## 最小 case 结构

每个 case 至少写清：

- `prompt`：真实用户会怎么说。
- `input_files`：需要读哪些文件；没有就留空。
- `baseline_output`：不用 skill 时的普通结果。
- `with_skill_output`：按 skill 行为得到的结果。
- `assertions`：能自动或半自动检查的要求。
- `human_notes`：需要人判断的审美、语气、完整性。

## 好断言

优先检查真实交付质量：

- 必须生成的文件路径。
- 必须包含的章节、边界、排除项或证据路径。
- 必须保留的命名、版权、README 安装命令。
- 禁止出现的私密路径、token、空泛占位符。
- 禁止过度声明“已发布”“已人工评审”“已 provider-backed”。

避免只奖励复读固定句子。能靠背一个短语通过的断言，不是好断言。

## 证据边界

- `recorded_fixture` 只能说明回归样例可复现，不能叫模型实跑证据。
- provider/model、token usage、runner command 都缺失时，不要说 provider-backed。
- 人工 blind review 没有 reviewer、reviewed_at、winner decision 和基于 rubric 的 reason，就只能算 pending。
- 缺证据时写 `missing evidence`，不要用计划替代证明。

## Blind A/B 完整性

当 baseline 与 with-skill 的优劣不能只靠断言判断时，生成三个分离产物：

1. `blind review pack`：随机化 A/B，只包含任务、rubric 和两个匿名输出。
2. `answer key`：单独保存 A/B 来源，并显著标记必须在判断完成后打开。
3. `review decisions`：记录 reviewer、reviewed_at、winner、confidence（可选）、rubric-based reason，以及“先判断、后揭晓”的 attestation。

只有有效判断才能计入 agreement 或 win rate。Pending、缺理由、提前泄露答案、仅由作者自评的案例必须单独展示限制，不能混入已完成的人类证据。揭晓前的报告不得显示 expected winner。
