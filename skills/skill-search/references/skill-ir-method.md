# Skill IR Method

Skill IR 是 skill 的平台中立语义契约。它回答“这个 skill 到底拥有哪件事”，而不是“某个平台需要什么文件名”。

## 什么时候导出 IR

- skill 要给团队复用。
- skill 要公开发布。
- skill 要支持 OpenAI、Claude、generic、Agent Skills、VS Code 等多个目标。
- skill 有脚本、权限、外部服务、账号操作或发布流程。
- 以后需要比较版本差异、做回归或生成评审报告。

Scaffold 阶段可以先不导出，但 Production 以上默认导出：

```bash
python3 scripts/export_skill_ir.py . --output reports/skill-ir.json
```

## IR 应包含

- skill 名称、版本、owner、成熟度、上游参考。
- frontmatter description。
- recurring job、输入、输出、边界、目标用户。
- should-trigger、should-not-trigger、near-neighbor 样例。
- workflow、decision points、gate ladder、output contract。
- references、scripts、evals、reports。
- target platforms、activation、execution、trust、permissions、degradation。
- evidence boundary：哪些是证据，哪些只是计划或缺失证据。

## IR 不应包含

- API key、cookie、token、私有账号。
- 不属于包的本机绝对路径。
- 从上游项目复制来的长段原文。
- 未测试平台的“已支持”声明。
- 还没有证据的世界级、生产级、人工评审或 provider-backed 结论。

## 评审问题

看 `reports/skill-ir.json` 时，评审者应该能快速回答：

1. 这个 skill 管什么事。
2. 什么时候应该触发。
3. 什么时候不应该触发。
4. 哪些文件承载真实行为。
5. 哪些 eval 或验证证明它没有跑偏。
6. 哪些平台可以消费它，语义是否有损失。
