# Resource Boundary Spec

## Keep in SKILL.md

- 触发条件
- 核心流程
- 必要的输出约束

## Move to references

- 长流程说明
- 示例集
- 评估标准
- 治理细则

## Move to scripts

- 可重复执行
- 需要确定性
- 适合自动化校验

## Move to evals

- should-trigger / should-not-trigger / near-neighbor 样例
- output eval 的 baseline、with-skill、assertions
- 防止用户已经指出过的失败再次发生

## Move to reports

- 运行脚本生成的验证结果
- `reports/skill-ir.json`
- 发布、评审、证据和缺失项说明

不要把报告里的生成结果反向塞回 `SKILL.md`，除非它已经变成稳定规则。

## 根入口隔离

一个可安装 skill 包只能有一个可被递归发现的入口：根目录的 `SKILL.md`。

- 仓库内嵌示例使用 `SKILL.example.md`。
- 测试夹具使用 `SKILL.fixture.md`。
- 示例或夹具复制成独立 skill 后，再把入口重命名为 `SKILL.md`。
- 验证和打包时扫描整个源树与归档；如果根目录之外仍有精确命名的 `SKILL.md`，应当阻断发布。

原因不是目录整洁，而是安装器可能复制整个仓库，agent 又可能递归发现入口文件，从而把示例或夹具误激活成独立 skill。
