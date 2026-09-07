# Operating Modes

## Scaffold

- 适合个人试验
- 优先快速成型
- 只保留最少文件

## Production

- 适合团队复用
- 需要明确触发词、边界和输出
- 必要时补评估和治理

## Library

- 适合共享基础设施
- 优先稳定结构
- 只增加真正复用的资源

## Governed

- 适合公开发布、团队关键流程、账号/密钥/网络/文件写入/付费服务相关 skill
- 需要明确 owner、review cadence、rollback boundary、trust boundary
- 缺少 provider-backed、人工评审、真实安装或 telemetry 证据时，必须标记 `missing evidence`

## 升级规则

- 从 Scaffold 升到 Production：当别人会复用，或触发边界容易误伤。
- 从 Production 升到 Library：当它是基础设施、meta skill、跨平台包或长期维护对象。
- 从 Library 升到 Governed：当输出会影响发布、账号、安全、公开承诺或团队关键流程。
