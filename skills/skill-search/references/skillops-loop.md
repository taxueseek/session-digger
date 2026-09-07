# SkillOps Loop

SkillOps 是发布后的轻量维护循环：用显式反馈和验证证据决定下一步，而不是让 skill 自己偷偷改规则。

## 可收集的信号

- 用户明确说某个输出“不好用”“太泛”“太丑”“不该触发”。
- 同类任务反复需要手工补同一段规则。
- trigger eval 出现 false positive 或 false negative。
- README 安装者找不到前置条件或排错方式。
- 发布流程反复漏掉 secret scan、install proof、PR merge。

## 默认动作

| 信号 | 默认动作 |
|---|---|
| 一次性偏好 | 先记录在报告或本次输出，不改规则 |
| 重复触发错误 | 补 trigger eval 或改 description |
| 输出质量回归 | 补 output eval 或更新参考规则 |
| README/发布漏项 | 改 README playbook 或 release gate |
| 高风险权限问题 | 进入 Governed 门禁，要求人工确认 |

## 安全规则

- 不隐式扫描私有聊天、私有日志或账号数据。
- 不保存原始私人内容；只保留脱敏摘要、计数和证据路径。
- 每个持久写入都要能对应一个验证命令。
- 报告可以提出建议，但不能自动改 AGENTS、memory、skill 或脚本，除非用户明确要求。

## 下一轮输出

当 SkillOps 发现值得改的地方，输出应包含：

1. 观察到的重复信号。
2. 推荐改到哪里：memory、AGENTS、现有 skill、eval、script、README、release gate。
3. 为什么这是最小持久面。
4. 需要跑的验证命令。
