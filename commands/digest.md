---
name: digest
description: |
  Full-session digest: topic-scan + experience synthesis in one pass.
  Triggers: "digest", "会话摘要", "full digest", "完整回顾", "全局分析", "综合回顾".
argument-hint: [--days N] [--topic <编号>] [--scope current|all]
allowed-tools: Bash, Read, Task
---

Run a full session digest: first scan topics, then synthesize experience for the selected area.

Arguments: $ARGUMENTS

**Script path discovery:**
```bash
SD_ROOT="${SESSION_DIGGER_ROOT:-${CLAUDE_PLUGIN_ROOT:-${HERDR_PLUGIN_ROOT:-}}}"
[[ -z "$SD_ROOT" ]] && SD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." 2>/dev/null && pwd)"
if [[ -z "$SD_ROOT" || ! -f "$SD_ROOT/scripts/sd-recall.py" ]]; then
  for _c in \
    "$HOME/.agents/skills/session-digger" \
    "$HOME/.claude/plugins/session-digger" \
    "$HOME/.claude/skills/session-digger" \
    "$HOME/.grok/skills/session-digger"
  do
    [[ -f "$_c/scripts/sd-recall.py" ]] && SD_ROOT="$_c" && break
  done
fi
# Use $SD_ROOT/scripts/<script> in subsequent commands. Prefer SESSION_DIGGER_ROOT when multiple installs exist.
```

## Mode detection

### 1. Full digest (default)
Run topic scan to see an overview, then ask the user which topic to deep-dive:

```bash
bash "$SD_ROOT/scripts/topic-scan.sh" --days "${DAYS:-30}" --format text
```

Present the topic list and ask the user to choose a topic number. Then run:

```bash
bash "$SD_ROOT/scripts/topic-scan.sh" --topic <编号>
```

Then launch the `experience-synthesis` workflow on the extracted context.

### 2. Topic-specific digest
If `--topic <编号>` is provided, skip the overview and directly extract context for that topic:

```bash
bash "$SD_ROOT/scripts/topic-scan.sh" --topic <编号>
```

### 3. Scope-limited digest
If `--scope current` is specified, limit the analysis to the current project's sessions.
