---
name: extract
description: |
  Extract knowledge from a conversation session — decisions, corrections, patterns, and references.
  Triggers: "extract", "从对话中学习", "记住这个", "提取经验", "提取知识", "save this insight".
argument-hint: [session-id] [--scope current|all]
allowed-tools: Bash, Read, Edit, Write, AskUserQuestion
---

Extract durable knowledge from a past Claude Code conversation session.

Arguments: $ARGUMENTS

**Step 1: Find the session**

If a session-id UUID is provided, locate it directly. Otherwise, list recent sessions:

bash "${CLAUDE_PLUGIN_ROOT}/scripts/list-sessions.sh" current --limit 7 --since "$(date -v-7d +%Y-%m-%d 2>/dev/null || date -d '7 days ago' +%Y-%m-%d)"

Present the list and ask the user which session to extract from (or default to the most recent).

**Step 2: Run extraction**

bash "${CLAUDE_PLUGIN_ROOT}/scripts/extract-knowledge.sh" SESSION_JSONL_PATH

This outputs a JSON array of candidate extractable items, each with:
- `category`: value | decision | correction | pattern | lesson | reference
- `content`: summary text
- `timestamp`: when it occurred
- `suggested_destination`: memory | claude_md | knowledge_file | skip
- `suggested_type`: value | user | feedback | project | reference

**Step 3: Present items to user**

For each item, present:
1. The category and content
2. The suggested destination and type
3. Ask the user to choose:
   - **Memory** — save as a memory file (ask for name if not obvious)
   - **CLAUDE.md** — add to project's CLAUDE.md instructions
   - **Knowledge file** — save as human-readable markdown (ask for path, default: docs/knowledge/)
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

**Step 5: Summary**

Report what was extracted and where it was saved.
