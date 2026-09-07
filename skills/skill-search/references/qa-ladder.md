# QA Ladder

1. 先确认描述能不能准确触发
2. 再确认边界是否足够清楚
3. 然后检查资源是否都能被找到
4. 最后看输出是否适合交付或发布

最轻的流程先过，没必要一上来把包做重。

## 2.0 加强版

5. Production 以上导出 `reports/skill-ir.json`
6. 公开发布前检查 README 产品页、安装证明、secret scan 和 public claim
7. 缺少外部证据、人工评审或 provider-backed 跑数时，写明 `missing evidence`
