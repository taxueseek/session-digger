# Agent Transcript Format Signatures

Living catalogue of "what a transcript from agent X looks like."
`scripts/format-detector.py` implements these as scoring rules. When you meet
a transcript from a new/unrecognized agent:

1. Read a sample of the raw file.
2. Note distinguishing field names, file naming conventions, or text markers.
3. Add a new entry below AND add a matching signature function in
   `format-detector.py`.
4. This keeps recognition improving over time instead of silently mis-parsing
   new formats as "generic."

---

## claude_code_jsonl (confirmed, high confidence)

- **File shape**: `.jsonl`, one JSON object per line.
- **Distinguishing fields**: `sessionId` or `uuid`, `cwd`, `gitBranch`,
  `toolUseResult`. `type` field is one of `user`/`assistant`/`system`.
- **Tool calls**: nested in `message.content[]` as blocks with
  `"type": "tool_use"` (has `name`, `id`, `input`) and corresponding
  `"type": "tool_result"` blocks (has `tool_use_id`, `is_error`, `content`).
- **Typical location**: `~/.claude/projects/<project-hash>/*.jsonl`
- **Parser**: `echolib.session_stats()` / `echolib.extract_tools()`
- **Adapter**: `claude`

## grok_jsonl (confirmed, high confidence)

- **File shape**: `.jsonl`, one JSON object per line.
- **Distinguishing fields**: `role` (user/assistant/system), `content` (string),
  `model` field containing "grok".
- **Typical location**: `~/.grok/sessions/<encoded-cwd>/<session-id>/chat_history.jsonl`
- **Parser**: `echolib` grok adapter
- **Adapter**: `grok`

## kimi_code_jsonl (confirmed, high confidence)

- **File shape**: `.jsonl`, one JSON object per line.
- **Distinguishing fields**: `event` field, `wire` event type, `model` field
  containing "kimi".
- **Typical location**: `~/.kimi-code/sessions/<project>/<session-id>/agents/main/wire.jsonl`
- **Parser**: `echolib` kimi_code adapter
- **Adapter**: `kimi_code`

## generic_markdown (fallback, medium confidence)

- **File shape**: `.md` or `.txt`, human-readable transcript export.
- **Distinguishing markers**: lines starting with `Human:`, `User:`,
  `Assistant:`, `AI:`, or `### ` headers separating turns; code fences.
- **Tool calls**: usually NOT structured — may appear as prose descriptions.
  Don't trust tool-call counts from this format; treat as rough estimates.
- **Parser**: `scripts/dialog-adapter.py` (import path) → then standard pipeline

## cline_like (partial signature — needs real samples to confirm)

- **Hypothesis**: Cline/Roo-Code style logs are JSON with fields like `say`,
  `ask`, `ts` (timestamp), often stored per-task under a `tasks/<id>/`
  directory.
- **Status**: `format-detector.py` has a placeholder check for `say`/`ask`+`ts`
  co-occurrence. **Not yet validated against a real Cline export.**
- **Parser**: none yet — falls through to generic import.

## aider_like (partial signature — needs real samples to confirm)

- **Hypothesis**: Aider writes `.aider.chat.history.md` files, markdown with
  `#### ` as the user-turn marker, and diffs shown as unified diff blocks.
- **Status**: placeholder only, unconfirmed.
- **Parser**: none yet — falls through to generic import.

## Other agents not yet covered

Cursor, Windsurf, GitHub Copilot Workspace, OpenHands/Devin-style agents,
custom in-house agents, etc. If you bring a transcript from one of these:

- `format-detector.py` will return `"format": "unknown"` with evidence and
  `sample_keys` to help you spot the distinguishing fields quickly.
- Treat this as a prompt to extend the signature library, not a dead end.

---

## Adding a new signature — checklist

- [ ] Get at least one real sample file (not a hypothetical/synthetic one)
- [ ] Identify 2-3 fields/markers that reliably distinguish this format from
      the others already in this file (avoid single weak signals)
- [ ] Add a scoring function to `format-detector.py` following the existing
      pattern (`_is_claude_code_jsonl` etc.)
- [ ] Add a corresponding entry to this file
- [ ] If the format needs custom parsing logic, add an adapter to `echolib/`'s
      `ADAPTER_REGISTRY` rather than relying on generic import
