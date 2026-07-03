---
name: extract
description: |
  Extract knowledge from a conversation session — decisions, corrections, patterns, and references.
  Triggers: "extract", "从对话中学习", "记住这个", "提取经验", "提取知识", "save this insight".
argument-hint: [session-id] [--scope current|all] [--agent claude|cross]
allowed-tools: Bash, Read, Edit, Write, AskUserQuestion
---

Extract durable knowledge from a past conversation session.

Arguments: $ARGUMENTS

**Step 0: Script path discovery**

Set `SD_ROOT` before running any script:

```bash
SD_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
[[ -z "$SD_ROOT" ]] && SD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
```

**Step 1: Find the session**

If a session-id UUID is provided, locate it directly. Otherwise, list recent sessions:

```bash
bash "$SD_ROOT/scripts/list-sessions.sh" current --limit 7 --since "$(date -v-7d +%Y-%m-%d 2>/dev/null || date -d '7 days ago' +%Y-%m-%d)"
```

Use `--agent cross` to also include Grok Build and Kimi Code sessions.

Present the list and ask the user which session to extract from (or default to the most recent).

**Step 2: Run extraction**

```bash
bash "$SD_ROOT/scripts/extract-knowledge.sh" SESSION_JSONL_PATH
```

This outputs a JSON array of candidate extractable items, each with:
- `category`: value | decision | correction | pattern | lesson | reference
- `content`: summary text
- `timestamp`: when it occurred
- `suggested_destination`: memory | claude_md | knowledge_file | skip
- `suggested_type`: value | user | feedback | project | reference | insight

**Taxonomy mapping** (extract-knowledge categories → experience-synthesis taxonomy):

| extract-knowledge | experience-synthesis | 说明 |
|---|---|---|
| `decision` | Decisions | AskUserQuestion 的问答记录 |
| `lesson` | Mistakes | 工具调用失败 |
| `value` | Learned Values | 用户表达的偏好（"X 比 Y 好"） |
| `correction` | Mistakes + User Preferences | 用户纠正 AI 的错误 |
| `pattern` | Effective Patterns | 用户认可的方法 |
| `reference` | Architecture Knowledge | URL 引用 |

extract-knowledge 目前不覆盖的三类（需手动补充或用 `/lessons` agent）：
- Anti-patterns（失败的方法）
- Recurring Problems（反复出现的问题）
- Performance & Cost（token 使用趋势）

**Step 2.5: Dedup against existing memories**

Before presenting to user, check for duplicates:

```bash
# List existing memory files for the project
ls ~/.claude/projects/<project>/memory/*.md 2>/dev/null
```

For each candidate item, compare against existing memory files:
1. Same `name` in frontmatter → mark as [UPDATE] instead of [NEW]
2. >60% keyword overlap with an existing file body → mark as [DUPLICATE], recommend merge instead of new file
3. No match → [NEW]

Prescan result format:
```
[NEW] category=decision: "Use X for Y" → memory/UseCaseX.md
[UPDATE] category=value: "LongCat for analysis, DeepSeek for synthesis" → memory/ModelStrategy.md (exists, update body)
[DUPLICATE] category=pattern: "dbs vs taxue" → already in memory/dbsTaxueComparisons.md
```

**Step 3: Present items to user**

For each item, present:
1. The [NEW]/[UPDATE]/[DUPLICATE] badge
2. The category and content
3. The suggested destination and type
4. Ask the user to choose:
   - **Memory** — save as a memory file (ask for name if not obvious)
   - **CLAUDE.md** — add to project's CLAUDE.md instructions
   - **Knowledge file** — save as human-readable markdown (ask for path, default: docs/knowledge/)
   - **Merge** — for [DUPLICATE], append source reference to existing file
   - **Skip** — discard

**Step 4: Write approved items**

For each approved item, write to the chosen destination:

**Memory file:** Create `~/.claude/projects/<project>/memory/<name>.md` with frontmatter:

    ---
    name: <name>
    description: <one-line summary>
    type: <chosen type>
    ---

    <content>

Then append an entry to MEMORY.md. If MEMORY.md doesn't exist, create it.
If only a standalone MEMORY.md exists (no individual files), create the first individual file and convert MEMORY.md to an index.

**CLAUDE.md:** Append to the project's CLAUDE.md at the resolved project root.
If project root is unresolvable, offer memory file as fallback.

**Knowledge file:** Write markdown to the chosen path.

**Merge:** For [DUPLICATE] items, append a `Source: <session-date>` line to the existing file body instead of creating new.

**Step 5: Summary**

Report what was extracted, updated, merged, and where they were saved.
