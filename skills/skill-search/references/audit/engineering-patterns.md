# 工程执行模式库

从标准诊断流程提炼的标准化修复操作。每种模式可直接执行。

## 模式 1：断链修复

**症状**：SKILL.md 引用了不存在的文件。

**检测**：

```
grep -oP 'references/[^ )\]]+' <skill>/SKILL.md | while read f; do
  [ ! -f "<skill>/$f" ] && echo "❌ 断链: $f → 实际: $(ls <skill>/references/ | grep ${f#references/})"
done
```

**修复**：

```
Edit: file_path=<skill>/SKILL.md
old_string: references/错误名.md
new_string: references/正确名.md
```

--------

## 模式 2：版本号统一

**症状**：SKILL.md frontmatter、脚本变量、备份目录名三处版本号不一致。

**检测**：

```
echo "=== SKILL.md ===" && grep "version:" <skill>/SKILL.md
echo "=== 脚本 ===" && grep -r "VERSION" <skill>/scripts/*.{py,sh} 2>/dev/null
echo "=== 备份 ===" && ls -d <skill>-v*.bak 2>/dev/null
```

**修复**：以脚本中的版本为基准，更新 SKILL.md frontmatter。

--------

## 模式 3：SKILL.md 瘦身

**症状**：SKILL.md >80 行，包含大量可下沉到路由表或参考文档的内容。

**瘦身规则**：

1. 路由表保留，规则移到路由表每行的备注列
2. "关键规则"中可执行的命令保留，纯文本说明删除
3. 参考文档索引保留，"必读"标注改为"按需读取"
4. 工作流中重复的 CLI 命令保留一行，其余删除
5. 版本号移到文件末尾一行

**目标**：SKILL.md <50 行（不含参考文档索引）

--------

## 模式 4：必读改按需

**症状**：参考文档标注"必读"，每次触发都加载。

**修复**：

1. 删除 SKILL.md 路由表中的"必读"标注
2. 在路由表的"需读取"列精确标注每条命令需要读哪些条目
3. 将最关键的 3-5 条陷阱直接内联到 SKILL.md 关键规则中

**示例**：

```
# 修复前
| readdata | references/troubleshooting.md（必读）|

# 修复后
| readdata | troubleshooting.md#陷阱1,4,8（时长单位、评分转换、进度语义）|
```

--------

## 模式 5：消除重复内容

**症状**：多个参考文档包含相同段落。

**检测**：

```
# 比较两个文件的相似度
diff --side-by-side --width=200 <skill>/references/<file1>.md <skill>/references/<file2>.md | grep "|"
```

**修复**：

1. 在引用方删除重复内容，改为 > 详见 references/XX.md
2. 在被引用方保留完整内容
3. 如果重复内容 >30 行，提取为独立文件 references/shared/XX.md，两处引用

--------

## 模式 6：添加 DO NOT use when

**症状**：SKILL.md 没有边界声明。

**修复**：在 SKILL.md 末尾版本号前添加：

```markdown
## DO NOT use when
- 场景A → 使用 XX skill
- 场景B → 使用 XX skill
- 超出本 skill 范围的需求 → 直接用模型原生能力
```

**原则**：3-5 条，覆盖最常见的误触发场景。

--------

## 模式 7：真源与注册副本同步

**症状**：skill 只存在于一个目录。

**检测**：

```bash
[ -d <真源>/<skill> ] && echo "✅ 真源" || echo "❌ 真源"
[ -d <全局注册目录>/<skill> ] && echo "✅ 注册" || echo "❌ 注册"
```

**修复**（推荐符号链接或同步脚本）：

```bash
# 以真源为源，注册目录创建链接
ln -sf <真源>/<skill> <全局注册目录>/<skill>

# 验证
ls -la <全局注册目录>/<skill>
```

--------

## 模式 8：清理备份

**症状**：`*.bak/` 目录留在 skills 路径下。

**修复**：

```bash
mkdir -p <skills-root>/.archive
mv <skills-root>/<skill>-v*.bak/ <skills-root>/.archive/
```

--------

## 模式 9：硬编码路径解耦

**症状**：脚本中硬编码 `~/.agents/` 或 `~/.claude/` 绝对路径。

**检测**：

```bash
grep -rn "~/.agents/\|~/.claude/" <skill>/scripts/ 2>/dev/null
```

**修复**：改为动态发现：

```python
import os
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 或
import pathlib
SKILL_DIR = pathlib.Path(__file__).parent.parent
```

--------

## 模式 10：CLI 冗余调用合并

**症状**：工作流中多次调用同一 CLI 获取相同数据。

**检测**：分析 SKILL.md 工作流部分，找重复的 CLI 命令。

**修复**：

1. 将多次调用合并为一次，输出到临时文件
2. 或让 CLI 增加一个合并命令（如合并多次数据读取为一次报告）
3. 利用 CLI 内置缓存（TTL）避免重复 API 请求
