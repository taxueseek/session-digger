# Gate Selection

## 需要加 gate 的情况

- 描述容易跑偏
- 输出质量依赖规则
- 会被很多人复用
- 会公开发布、调用账号、读写文件、联网、执行脚本或影响团队流程
- README 或发布描述里会出现“已验证”“可安装”“生产级”等 claim

## 不必加 gate 的情况

- 只是个人草稿
- 逻辑很轻
- 失败代价很低

默认先轻，再按需要加重。

## 默认选择

- 所有 skill：`validate_skill.py`
- Production 以上：`trigger_eval.py`
- Library 以上：`export_skill_ir.py`
- Governed：secret scan、install proof、trust boundary、rollback boundary、public claim guard

缺证据时不要补漂亮话，写 `missing evidence`。
