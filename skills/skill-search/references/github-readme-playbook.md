# GitHub README Playbook

Use this when creating, refactoring, or publishing a skill. The README is the
public product page for humans; `SKILL.md` is the runtime instruction file for
agents. Do not dump internal agent rules into the README.

## README Goal

The README should make a stranger quickly answer:

1. What problem does this solve for me?
2. What will I get after installing it?
3. How do I install and verify it?
4. What can I say to trigger it?
5. What can go wrong, and how do I fix it?

## Required Shape

For public GitHub skills, default to Chinese first. Add English only when the
skill has a likely international audience.

Recommended order:

```markdown
# skill-name

> 一句话痛点/价值主张。

[badges if public and useful]

## 为什么值得用
## 一行安装
## 你可以这样说
## 它会做什么
## 前置条件
## 输出示例
## 配置
## Troubleshooting
## 致谢
## License
```

Keep the first screen focused: hook, install, natural-language examples, and one
concrete output preview. Long architecture notes belong later.

## Must-Have Checklist

- [ ] First sentence describes the user's pain or desired outcome, not the
      implementation.
- [ ] One-line install command appears near the top:
      `npx skills add owner/repo`.
- [ ] 3-5 natural-language trigger examples show what a user would actually say.
- [ ] Prerequisites use checkbox format and include verification commands.
- [ ] Output section shows concrete files, API calls, screenshots, or snippets.
- [ ] Configuration section lists environment variables without secrets.
- [ ] Troubleshooting has at least 3 rows: symptom, cause, fix.
- [ ] Risks and side effects are explicit for credentials, writes, costs, network
      calls, publishing, destructive actions, or account automation.
- [ ] Third-party tools and upstream projects are credited.
- [ ] README does not expose private domains, tokens, cookies, VPS paths, or
      user-specific absolute paths unless the skill is explicitly private.

## Skill README Template

```markdown
# skill-name

> 用户现在遇到的痛点，以及安装后能得到什么。

[![Last commit](https://img.shields.io/github/last-commit/OWNER/REPO?style=flat-square)](https://github.com/OWNER/REPO/commits/main)
[![License](https://img.shields.io/github/license/OWNER/REPO?style=flat-square)](LICENSE)

## 为什么值得用

用 2-4 句讲具体场景。避免“高效、智能、自动化”这类空词。

## 安装

```bash
npx skills add OWNER/REPO
```

验证：

```bash
ls ~/.agents/skills/skill-name
```

## 你可以这样说

- “把这个流程整理成一个 skill”
- “发布这个 skill 到 GitHub”
- “给这个 skill 补触发词和边界”

## 它会做什么

1. 读取输入和已有文件
2. 生成或更新 `SKILL.md`
3. 补 README、interface、scripts 或 references
4. 运行必要验证

## 前置条件

- [ ] Node.js / Python / CLI 依赖：安装命令和 `--version` 验证
- [ ] GitHub CLI：`gh auth status`
- [ ] 需要的环境变量：只写变量名，不写真实值

## 输出示例

```text
Created skill: ~/.agents/skills/example-skill
Validated: SKILL.md frontmatter OK
Published: https://github.com/OWNER/example-skill
```

## 配置

| 变量 | 必需 | 说明 |
|---|---:|---|
| `EXAMPLE_TOKEN` | 否 | 只在调用某 API 时需要 |

## Troubleshooting

| 问题 | 原因 | 解决 |
|---|---|---|
| `No valid skills found` | YAML frontmatter 不合法 | 使用 `description: |` 块标量 |
| `gh: not authenticated` | GitHub CLI 未登录 | 运行 `gh auth login` |
| 找不到 skill | 安装目录不一致 | 检查 `~/.agents/skills/<name>` |

## 致谢

列出依赖的开源工具、上游项目或参考方法。

## License

ISC
```

## Web Or Visual Project Extras

If a skill ships a website, visual tool, or generated media experience, README
must include:

- product screenshot near the first screen, preferably `docs/assets/product-screenshot.png`
- live demo or deploy button when available
- screenshot capture command such as `npm run capture:screenshots`
- example gallery or before/after output

Do not publish a visual project README without screenshots unless the user
explicitly asks for a minimal private package.

## Writing Rules

- Prefer short paragraphs and concrete examples.
- Start with user value, not architecture.
- Put prerequisites after the install/use examples unless missing credentials
  would be dangerous or expensive.
- Use natural-language examples instead of only CLI commands.
- Keep internal agent constraints out of README unless they affect users.
- If the skill is open source, replace private values with placeholders:
  `https://your-site.example`, `YOUR_TOKEN`, `OWNER/REPO`.

## Bad Smells

- README is just `SKILL.md` pasted into Markdown.
- First paragraph says “This skill uses...” instead of “You can...”.
- No install command.
- No examples of what to say to the agent.
- Private hostnames, passwords, cookies, or local paths appear in public docs.
- Troubleshooting is missing.
- Web/UI project has no screenshot.
