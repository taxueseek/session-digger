// session-digger 原生工具规格（手写版，对应 argo 的 gen_native_tools 产物）。
//
// 每个工具 = sd-recall.py 的一个子命令 + --json。参数即 CLI 参数，
// 语义与 CLI 逐字一致（同一份 find_sessions / 同一份索引），
// 工具面与 CLI 面因此不可能漂移。
//
// 隐私边界（默认保守）：返回的字段与 CLI 文本输出完全相同——
// 会话元数据 + 已裁剪摘要/消息片段（150/80/120 字窗口），
// 不是完整转录。要全文请走 skill 形态的 deep 检索。

export const NATIVE_TOOL_SPECS = {
  sd_search: {
    cli: 'search',
    description:
      'Search past agent sessions (Claude Code / Codex / Grok / Kimi / Cursor / Trae / dimcode / …) by keyword, across every installed environment. '
      + 'Returns session metadata (id, agent, modified time, message/tool/error counts, branch) plus clipped evidence: user message snippets, tool errors, and decision points. '
      + 'Use for "when did I discuss X", "which session touched Y", cross-session recall. Reads the prebuilt SQLite FTS index; the response Index line names its freshness — when it says stale, rebuild with index-builder.py build before retrying.',
    allowed: ['query', 'scope', 'limit', 'agent', 'decisions', 'include_tools'],
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['query'],
      properties: {
        query: { type: 'string', description: 'Keyword or CJK phrase to search (FTS5; punctuation-only queries are rejected with a hint).' },
        scope: { type: 'string', enum: ['current', 'all'], default: 'current', description: 'current = sessions under the working directory; all = every indexed session.' },
        limit: { type: 'number', minimum: 1, maximum: 20, default: 5, description: 'Max sessions returned (hard cap 20 — the CLI clips evidence per session anyway).' },
        agent: { type: 'string', default: 'cross', description: 'Restrict to one environment (claude, codex, grok, kimi, cursor, trae, dimcode, …); cross = all.' },
        decisions: { type: 'boolean', default: false, description: 'Also extract decision points (choices, rejections, trade-offs).' },
        include_tools: { type: 'boolean', default: false, description: 'Also match tool-result digests (default: prose only — tool digests took 35% of the top page for 10% recall).' },
      },
    },
  },

  sd_sessions: {
    cli: 'sessions',
    description:
      'List recent agent sessions with metadata: session id, created/modified time, message count, branch, source environment, and transcript path. '
      + 'Optionally filtered by a keyword (routes through the same FTS index as sd_search). '
      + 'Use to orient on "what have I worked on lately" or to find the session id a sd_search hit refers to.',
    allowed: ['scope', 'limit', 'agent', 'keyword'],
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        scope: { type: 'string', enum: ['current', 'all'], default: 'current', description: 'current = sessions under the working directory; all = every indexed session.' },
        limit: { type: 'number', minimum: 1, maximum: 50, default: 20, description: 'Max sessions listed.' },
        agent: { type: 'string', default: 'cross', description: 'Restrict to one environment; cross = all.' },
        keyword: { type: 'string', default: '', description: 'Optional: only sessions whose text matches this term (FTS-indexed, same as sd_search).' },
      },
    },
  },

  sd_stats: {
    cli: 'stats',
    description:
      'Aggregate usage statistics over the session-digger index: total sessions, messages, tool calls, tool errors, tokens, per-environment breakdown, and index freshness. '
      + 'Use for "how much have I used each agent" or to check whether the index is current before deep recall work.',
    allowed: ['agent'],
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        agent: { type: 'string', default: 'cross', description: 'Restrict to one environment; cross = all (adds the per-environment breakdown).' },
      },
    },
  },
}

export const DEFAULT_NATIVE_TOOLS = ['sd_search', 'sd_sessions', 'sd_stats']
