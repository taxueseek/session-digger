# wechat-digger — 微信本地数据识别与分析

session-digger 的微信子技能：把已解密的微信本地数据库变成**可检索、可分析的知识资产**——全史文本检索（毫秒级 FTS5 索引）、群聊画像、关系网络、商机跟进、噪音群治理、跨会话成文、月度 AI 使用画像。

**血统与性能**：检索层延续 [argo](https://github.com/taxueseek/argo) 的改进思路——量化定位瓶颈、索引优先 + 增量同步 + 语义口径守门，把全史关键词查询从 LIKE 全表扫描的秒级压到毫秒级；同样的「先测后改、口径不变」纪律贯穿每一轮优化。

**本发布版只做识别与分析。** 不包含、也不会分发微信密钥提取（key capture）与 SQLCipher 解密实现；`wd.py` 的 `keys` / `decrypt` / `refresh` 子命令在公开包里不可用（`doctor` 会如实标注）。你用任何自选工具完成解密后，把明文库路径交给它即可。

**数据只在本地。** 本技能的全部读取、索引与分析都发生在本机——不联网、不上传、无遥测。唯一写盘的产物（索引/报告/状态文件）都落在本机数据目录。

**关于反馈**：提 issue 或 PR 时**请勿附带密钥文件、数据库文件、真实聊天记录或任何含会话内容的截图**——匿名化描述现象（命令 + 脱敏输出 + 环境）即可。隐私数据一旦提交进公开仓即无法彻底撤回。已解密库的媒体层开箱即用：语音导出（SILK）/转账红包/好友申请/`.dat` 图片离线还原（`wd.py extras …`，图片层需可选依赖 pycryptodome）。

## 快速开始

```bash
# 1. 指向已解密的微信明文库（目录含 message_*.db / contact.db / session.db / message_fts.db）
export WECHAT_PRIVATE_VAULT="/path/to/decrypted"

# 2. 环境自检
python3 scripts/wd.py doctor

# 3. 全史检索（走微信自带 message_fts 解密副本）
python3 scripts/wd.py search "关键词" --group-by chat --limit 30
```

## 导出与转写口径

| 目标 | 命令 | 说明 |
|---|---|---|
| 结构化 JSON | `wd.py export-msg …` | 全字段消息，机器可读 |
| Markdown | `wd.py vault export-chat --mode full` / `--mode incremental`（底层 `export_chat.py`） | 人类可读全文，按联系人/日期组织 |
| PDF | 无内置 | 零依赖做不了真 PDF——把上面的 Markdown 交给 pandoc / 浏览器打印即可 |
| 语音 → 文本 | `wd.py extras voice-export --id N` | 导出 SILK 音频；本地转写接外部转写器（如 silk-v3 解码 + whisper），本技能不内置转写模型 |

依赖：Python 3.8+ 标准库即可跑分析层；`export_chat` 等导出件需要 `zstandard`，图片还原层需要 `pycryptodome`（均为可选，`scripts/setup_deps.sh` 一键装进 skill 本地 venv）。wx-cli 在线功能需自装 wx 工具。

## 与完整内部版的差异

| | 完整内部版 | 本发布版 |
|---|---|---|
| 密钥提取（内存抓 key） | ✅ | ❌ 剔除 |
| SQLCipher 全量/增量解密 | ✅ | ❌ 剔除 |
| 已解密 vault 只读查询 | ✅ | ✅ |
| fts 全史检索 / 分析 / 成文 | ✅ | ✅ |
| wx-cli 在线桥 | ✅ | ✅ |
| 语音导出 / 转账红包 / 好友申请 | ✅ | ✅（`extras`） |
| `.dat` 图片离线还原 | ✅ | ✅（`extras images-*`，需 pycryptodome） |
| 语音 SILK→文本转写 | 外部工具 | 接口约定：`extras voice-export` 出 SILK，转写交给外部转写器 |

内部版另有独立仓库与迭代节奏，公开版以 `SYNC.md` 记录同步基线。数据接入契约详见 `SKILL.md` 的「数据接入」一节。

## 隐私边界

- 明文库与索引产物不进本仓库；`WECHAT_DIGGER_DATA` 自行指定
- 不输出完整 key / salt / wxid
- 自带 `health.py` 防泄漏 lint：任何 `.py/.md/.json` 出现绝对用户路径即报错
