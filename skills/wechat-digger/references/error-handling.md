# 错误处理

每条错误 JSON / 回复必须包含：

1. **fact**：发生了什么（可核验）
2. **action**：用户或 Agent 下一步做什么
3. **forbidden**：不要做什么

## 常见出口

### no_source / all_sources_failed

- fact：vault / wxcli / export 均不可用或返回空
- action：`wd.py doctor`；缺库则 vault 增量解密；微信未开则启动并 `wx init`
- forbidden：不要编造消息

### empty history

- fact：该 chat + 时间窗无消息
- action：放宽 `--since` / 核对群名 `contacts --query`
- forbidden：不要用其他群数据冒充

### index error

- fact：FTS 查询失败或库损坏
- action：删除 data_root/index/messages.db 后 `index` 重建
- forbidden：不要手改 sqlite schema 半成品

### privacy

- fact：请求展示 key/salt/完整 wxid
- action：拒绝完整值，可提示「已配置/未配置」
- forbidden：写入日志、提交 git、贴进公开文档
