# wechat-digger — 微信本地数据识别与分析

session-digger 的微信子技能：把已解密的微信本地数据库变成**可检索、可分析的知识资产**——全史文本检索、群聊画像、关系网络、商机跟进、噪音群治理、跨会话成文。

**本发布版只做识别与分析。** 不包含、也不会分发微信密钥提取（key capture）与 SQLCipher 解密实现；`wd.py` 的 `keys` / `decrypt` / `refresh` 子命令在公开包里不可用（`doctor` 会如实标注）。你用任何自选工具完成解密后，把明文库路径交给它即可。

## 快速开始

```bash
# 1. 指向已解密的微信明文库（目录含 message_*.db / contact.db / session.db / message_fts.db）
export WECHAT_PRIVATE_VAULT="/path/to/decrypted"

# 2. 环境自检
python3 scripts/wd.py doctor

# 3. 全史检索（走微信自带 message_fts 解密副本）
python3 scripts/wd.py search "关键词" --group-by chat --limit 30
```

依赖：Python 3.8+ 标准库即可跑分析层；`export_chat` 等导出件需要 `zstandard`（`scripts/setup_deps.sh` 一键装进 skill 本地 venv）。wx-cli 在线功能需自装 wx 工具。

## 与完整内部版的差异

| | 完整内部版 | 本发布版 |
|---|---|---|
| 密钥提取（内存抓 key） | ✅ | ❌ 剔除 |
| SQLCipher 全量/增量解密 | ✅ | ❌ 剔除 |
| 已解密 vault 只读查询 | ✅ | ✅ |
| fts 全史检索 / 分析 / 成文 | ✅ | ✅ |
| wx-cli 在线桥 | ✅ | ✅ |

内部版另有独立仓库与迭代节奏，公开版以 `SYNC.md` 记录同步基线。数据接入契约详见 `SKILL.md` 的「数据接入」一节。

## 隐私边界

- 明文库与索引产物不进本仓库；`WECHAT_DIGGER_DATA` 自行指定
- 不输出完整 key / salt / wxid
- 自带 `health.py` 防泄漏 lint：任何 `.py/.md/.json` 出现绝对用户路径即报错
