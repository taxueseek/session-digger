// session-digger — DSH plugin（skill + 插件双形态的插件面）。
//
// 形态纪律（同 argo-dsh）：
//   - Python CLI（sd-recall.py）永远是唯一真源；本插件是薄封装，
//     每次工具调用单发执行 `python3 sd-recall.py <cmd> --json`。
//     本地索引查询是低频动作：单发成本 = 进程启动 ~20-40 ms
//     （实测热路径 0.01-0.13 s），常驻 MCP 连接在这里是纯负债，
//     所以本插件 deliberately 没有连接池。
//   - 版本号运行期从 package.json 读取——硬编码的版本号必然漂移。
//   - 不 import @deepseek-ai/*：cordis 只承诺公共 ctx 契约，
//     内部 API 随时可改。
//   - session-digger 安装位在调用时探测（不在插件加载时）：
//     用户可以先装插件、后装 skill，工具届时自然可用。
//
// hybrid 路由（0.9.35 起，dshNative 配置可关）：
//   - 宿主挂了 @deepseek-ai/dsh-session-query-sqlite 时，DSH 自己的
//     会话经 ctx.sessionQuery 进程内检索（毫秒级、零 spawn、实时新鲜），
//     其余 16+ 环境照走 Python 索引；两路在 execute 里并行，合并渲染。
//   - CLI 是外部进程到不了 ctx.sessionQuery，所以 DSH 行只出现在插件
//     形态且以 agent='dsh' 标记来源——双形态间唯一的有意分歧。
//   - 服务未挂载或调用失败一律静默降级或如实标注，Python 结果永不丢。
//
// 依赖：Python ≥ 3.9（session-digger 本体，纯 stdlib 零 pip 依赖）。

import { spawn } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, isAbsolute, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { DEFAULT_NATIVE_TOOLS, NATIVE_TOOL_SPECS } from './native-tools.mjs'
import { countDshSessions, listDshSessions, resolveSessionQuery, searchDshSessions } from './native/session-query.mjs'

export const name = 'session-digger'
export const inject = ['tools', 'systemPrompt']

/** 版本号运行期从 package.json 读取（单源，永不手写）。 */
function resolvePluginVersion() {
  try {
    const manifest = JSON.parse(
      readFileSync(new URL('../package.json', import.meta.url), 'utf8'))
    return typeof manifest.version === 'string' && manifest.version !== ''
      ? manifest.version
      : 'unknown'
  } catch {
    return 'unknown'
  }
}

export const PLUGIN_VERSION = resolvePluginVersion()

export const DEFAULTS = {
  /** 默认注册的原生工具（空数组 = 全关；未知名 loud fail）。 */
  nativeTools: DEFAULT_NATIVE_TOOLS,
  /** Python 解释器（session-digger 需 ≥ 3.9）。 */
  pythonCommand: process.env.SESSION_DIGGER_PYTHON || 'python3',
  /** 单次 CLI 调用超时（ms）。文件扫描回退路径实测 1-2 s，30 s 宽裕。 */
  nativeTimeoutMs: 30_000,
  /** 原生工具返回文本上限（字符），兜底防上下文膨胀。 */
  nativeMaxChars: 24_000,
  /** hybrid 路由开关：宿主挂了 sessionQuery 后端时把 DSH 原生会话并进结果。 */
  dshNative: true,
}

// session-digger 安装位探测（与 skills/common_paths.py 同一优先级）：
// 环境变量 > 常见安装位 > 本包所在仓库根（link: 安装时自动生效）。
// 探测在每次工具调用时做——安装位是运行时状态，不是加载时状态。
const COMMON_ROOTS = [
  '.agents/skills/session-digger',
  '.claude/plugins/session-digger',
  '.claude/skills/session-digger',
  '.grok/skills/session-digger',
]

function looksLikeRoot(candidate) {
  return typeof candidate === 'string' && candidate !== ''
    && existsSync(join(candidate, 'scripts', 'sd-recall.py'))
}

export function discoverRoot(env = process.env, home = homedir(),
                              here = dirname(fileURLToPath(import.meta.url))) {
  if (looksLikeRoot(env.SESSION_DIGGER_ROOT)) {
    return isAbsolute(env.SESSION_DIGGER_ROOT)
      ? env.SESSION_DIGGER_ROOT
      : resolve(env.SESSION_DIGGER_ROOT)
  }
  for (const candidate of COMMON_ROOTS) {
    const root = join(home, candidate)
    if (looksLikeRoot(root)) return root
  }
  // 本包位于 <repo>/dsh-plugin/dsh/index.js → 仓库根是向上两层。
  // npm 安装时该位置没有 scripts/，探测自然落空回退上一优先级。
  const checkout = resolve(here, '..', '..')
  if (looksLikeRoot(checkout)) return checkout
  return null
}

const text = (value) => [{ type: 'text', text: value }]
const clip = (value, maximum) => value.length <= maximum ? value : `${value.slice(0, maximum - 1)}…`
const asString = (value, fallback = '') => typeof value === 'string' ? value.trim() : fallback
const clampInt = (value, fallback, minimum, maximum) => Number.isFinite(value)
  ? Math.max(minimum, Math.min(maximum, Math.floor(value)))
  : fallback

// ── DSH 原生行归一化 ─────────────────────────────────────────
// ctx.sessionQuery 的 hit 记录（SessionHeader/bestMatch.snippet）落到
// 与 CLI 行同名的字段上；缺的字段如实填 '?'，不编造。片段窗口与
// CLI 的 user_messages 预算同为 150 字（隐私边界对齐）。

const DSH_SNIPPET_CHARS = 150

function formatLocalTime(epochMs) {
  const n = Number(epochMs)
  if (!Number.isFinite(n) || n <= 0) return '?'
  const date = new Date(n)
  if (Number.isNaN(date.getTime())) return '?'
  const pad = (x) => String(x).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** searchSessions 的 hit → 渲染块；snippet 是唯一证据，诚实单条。 */
function dshSearchRows(items) {
  return items.map((hit) => ({
    session_id: asString(hit?.header?.id, '?'),
    modified: formatLocalTime(hit?.bestMatch?.time ?? hit?.header?.createdAt),
    path: asString(hit?.header?.cwd, ''),
    live: hit?.live === true,
    snippet: clip(asString(hit?.bestMatch?.snippet), DSH_SNIPPET_CHARS),
    match_time: formatLocalTime(hit?.bestMatch?.time),
  }))
}

/** listSessions 的 record → TSV 行字段（同 renderSessions 的列序）。 */
function dshListRows(items) {
  return items.map((record) => ({
    session_id: asString(record?.header?.id, '?'),
    created: formatLocalTime(record?.header?.createdAt),
    modified: formatLocalTime(record?.header?.createdAt),
    messages: '?',
    branch: '',
    agent: 'dsh',
    path: asString(record?.header?.cwd, ''),
  }))
}

// ── CLI 单发执行 ─────────────────────────────────────────────
// 每次调用一个进程：搜索本身是本地 SQLite 读，进程启动成本
// 可忽略；超时与错误都会杀进程，不留常驻。

function runCli(command, args, timeoutMs) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(command, args, { stdio: ['ignore', 'pipe', 'pipe'] })
    let stdout = ''
    let stderr = ''
    const timer = setTimeout(() => {
      child.kill('SIGKILL')
      reject(new Error(
        `sd-recall ${args[1]} timed out after ${timeoutMs}ms `
        + `(index rebuild in progress? rerun index-builder.py build)`))
    }, timeoutMs)
    child.stdout.on('data', (chunk) => { stdout += chunk })
    child.stderr.on('data', (chunk) => { stderr += chunk })
    child.on('error', (err) => {
      clearTimeout(timer)
      reject(err)
    })
    child.on('close', (code) => {
      clearTimeout(timer)
      if (code === 0) { resolvePromise({ stdout, stderr }); return }
      reject(new Error(
        `sd-recall exited ${code}: ${clip(stderr.trim() !== '' ? stderr : stdout, 500)}`))
    })
  })
}

/** 把工具的 JSON 参数落成 sd-recall.py 的 argv。 */
export function buildArgv(kind, args) {
  const spec = NATIVE_TOOL_SPECS[kind]
  if (!spec) {
    throw new Error(
      `session-digger-dsh: unknown native tool "${kind}" `
      + `(known: ${Object.keys(NATIVE_TOOL_SPECS).join(', ')})`)
  }
  const argv = [spec.cli, '--json']
  if (kind === 'sd_search') {
    argv.push(asString(args.query))
    argv.push('--scope', asString(args.scope, 'current'))
    argv.push('--limit', String(clampInt(args.limit, 5, 1, 20)))
    argv.push('--agent', asString(args.agent, 'cross'))
    if (args.decisions === true) argv.push('--decisions')
    if (args.include_tools === true) argv.push('--include-tools')
  } else if (kind === 'sd_sessions') {
    argv.push('--scope', asString(args.scope, 'current'))
    argv.push('--limit', String(clampInt(args.limit, 20, 1, 50)))
    argv.push('--agent', asString(args.agent, 'cross'))
    const keyword = asString(args.keyword)
    if (keyword !== '') argv.push(keyword)
  } else if (kind === 'sd_stats') {
    argv.push('--agent', asString(args.agent, 'cross'))
  }
  return argv
}

// ── 渲染：JSON 包络 → 与 CLI 文本输出逐行同构的文本 ──────────
// 模型经工具看到的与 skill 路由到 CLI 看到的完全一致——
// 两条形态同一份信息，不会出现「插件里查到、CLI 里查不到」。
// 例外（有意分歧）：DSH 原生会话只存在于插件形态（CLI 够不到
// ctx.sessionQuery），渲染为明确标记来源的附加段。

/** DSH 段的公共框架：来源行 + 可用性声明，永不吞错误。 */
function renderDshPreamble(dsh, envelope) {
  if (dsh === 'scope') {
    return [
      `DSH native (sessionQuery): not searched for scope='${envelope.scope}' — native search covers every workspace; rerun with --scope all.`,
    ]
  }
  if (dsh === null || dsh.ok === false) {
    const reason = dsh === null ? 'service absent' : clip(asString(dsh.error, 'unknown error'), 160)
    return [`DSH native (sessionQuery): unavailable — ${reason}`]
  }
  return null
}

function renderDshSearch(dsh, envelope) {
  const preamble = renderDshPreamble(dsh, envelope)
  if (preamble !== null) return preamble
  const rows = dshSearchRows(dsh.items ?? [])
  // includes tool events：官方 searchSessions 默认搜全部表层事件，与
  // CLI 的 prose-only 默认 recall 组成不同，明示不让模型误读对齐。
  const header = 'DSH native (sessionQuery, live — not covered by the Python index; includes tool events)'
  if (rows.length === 0) return [`${header}: no match.`]
  const lines = [`${header}: ${rows.length} session(s)`]
  rows.forEach((row, i) => {
    lines.push(`--- [${i + 1}/${rows.length}] ${row.session_id} (dsh, ${row.modified}) ---`)
    lines.push(`Path: ${row.path || '?'} | Live: ${row.live ? 'yes' : 'no'}`)
    lines.push(`Match: [${row.match_time}] ${row.snippet || '(no snippet)'}`)
  })
  return lines
}

function renderDshSessions(dsh, envelope) {
  const preamble = renderDshPreamble(dsh, envelope)
  if (preamble !== null) return preamble
  const rows = dshListRows(dsh.items ?? [])
  const lines = [`DSH native (sessionQuery, live — not covered by the Python index):`]
  for (const row of rows) {
    lines.push([row.session_id, row.created, row.modified,
      row.messages, row.branch, row.agent, row.path].join('\t'))
  }
  lines.push(`--- ${rows.length} DSH session(s) ---`)
  return lines
}

function renderDshStats(dsh) {
  if (dsh === undefined || dsh === null || dsh.ok === false) {
    const reason = dsh === null || dsh === undefined
      ? 'service absent'
      : clip(asString(dsh.error, 'unknown error'), 160)
    return [`  dsh (sessionQuery): unavailable — ${reason}`]
  }
  return [`  dsh (sessionQuery): ${dsh.count} sessions (native, live index — not in the counts above)`]
}

function renderSearch(envelope) {
  if (envelope.found === 0) {
    const empty = envelope.empty || {}
    const lines = [
      `No match for '${envelope.query}' in scope '${envelope.scope}'.`,
      `  Index: ${envelope.index}`,
    ]
    const rejection = empty.rejection
    if (rejection === 'empty') {
      lines.push('  This term has no searchable content (punctuation or whitespace only) — try a word.')
    } else if (rejection === 'too_many') {
      lines.push('  This query has more than the searchable word limit — keep the two or three that matter.')
    } else if (empty.facet_total > 0) {
      const where = Object.entries(empty.facet_counts || {})
        .map(([facet, n]) => `${facet}=${n}`).sort().join(', ')
      lines.push(`  Index does hold ${empty.facet_total} row(s) elsewhere (${where}).`)
      if (envelope.scope !== 'all') lines.push('  Widen: rerun with --scope all')
      if (envelope.agent !== 'cross') lines.push('  Widen: rerun with --agent cross')
      if (empty.facet_counts && empty.facet_counts.TOOL) {
        lines.push(`  ${empty.facet_counts.TOOL} of them are tool result digests, not prose.`)
      }
    } else {
      lines.push('  The index has no row for this term in any scope.')
    }
    if (envelope.dsh !== undefined) {
      const dshLines = renderDshSearch(envelope.dsh, envelope)
      if (dshLines.length) {
        lines.push(...dshLines)
        lines.push('')
      }
    }
    return lines.join('\n').trimEnd()
  }
  const lines = [
    `Found ${envelope.found} session(s). Index: ${envelope.index} (${envelope.elapsed_s ?? 0}s)`,
  ]
  envelope.sessions.forEach((row, i) => {
    lines.push(`--- [${i + 1}/${envelope.found}] ${row.session_id} (${row.agent}, ${row.modified}) ---`)
    lines.push(`Messages: ${row.messages ?? '?'} | Tools: ${row.tools ?? '?'} | Errors: ${row.errors ?? '?'} | Branch: ${row.branch || ''}`)
    if (row.summary) lines.push(`Summary: ${row.summary.replace(/\n/g, ' ')}`)
    const evidence = row.evidence || {}
    if (evidence.user_messages && evidence.user_messages.length) {
      lines.push(`User messages (${evidence.user_messages.length}):`)
      for (const m of evidence.user_messages) lines.push(`  [${m.timestamp}] ${m.text}`)
    }
    if (evidence.tool_errors && evidence.tool_errors.length) {
      lines.push(`Tool errors (${evidence.tool_errors.length}):`)
      for (const t of evidence.tool_errors) lines.push(`  [${t.timestamp}] ${t.name}: ${t.result_preview}`)
    }
    if (evidence.decisions && evidence.decisions.length) {
      lines.push(`Decision points (${evidence.decisions.length}):`)
      for (const d of evidence.decisions) lines.push(`  [${d.timestamp}] ${d.role}: ${d.text}`)
    }
    if (evidence.full_excerpt && evidence.full_excerpt.length) {
      lines.push(`Full excerpt (${evidence.full_excerpt.length} msgs):`)
      for (const m of evidence.full_excerpt) lines.push(`  [${m.timestamp}] ${m.role}: ${m.text}`)
    }
    lines.push('')
  })
  if (envelope.dsh !== undefined) {
    const dshLines = renderDshSearch(envelope.dsh, envelope)
    if (dshLines.length) {
      lines.push(...dshLines)
      lines.push('')
    }
  }
  return lines.join('\n').trimEnd()
}

function renderSessions(envelope) {
  const lines = ['SESSION_ID\tCREATED\tMODIFIED\tMSGS\tBRANCH\tAGENT\tPATH']
  for (const row of envelope.sessions) {
    lines.push([row.session_id, row.created, row.modified,
      row.messages, row.branch, row.agent, row.path].join('\t'))
  }
  lines.push(`--- ${envelope.found} session(s) ---`)
  if (envelope.dsh !== undefined) {
    const dshLines = renderDshSessions(envelope.dsh, envelope)
    if (dshLines.length) {
      lines.push('')
      lines.push(...dshLines)
    }
  }
  return lines.join('\n')
}

function renderStats(envelope) {
  const lines = [
    `Total sessions: ${envelope.sessions}`,
    `Total messages: ${envelope.messages}`,
    `Total tool calls: ${envelope.tool_calls}`,
    `Tool errors: ${envelope.tool_errors} (failed tool calls, not index parse errors)`,
    `Total tokens: ${envelope.total_tokens}`,
  ]
  if (envelope.index) {
    lines.push(`Index: ${envelope.index.sessions} sessions, ${envelope.index.messages_indexed} messages indexed`)
    for (const env of envelope.env_breakdown || []) {
      lines.push(`  ${env.agent}: ${env.sessions} sessions, ${env.tool_calls} tools, ${env.errors} errors`)
    }
  }
  if (envelope.dsh !== undefined) {
    lines.push(...renderDshStats(envelope.dsh))
  }
  return lines.join('\n')
}

function renderEnvelope(kind, envelope) {
  if (kind === 'sd_search') return renderSearch(envelope)
  if (kind === 'sd_sessions') return renderSessions(envelope)
  return renderStats(envelope)
}

// ── 工具工厂 ────────────────────────────────────────────────

/**
 * DSH 原生分支的派发：kind × 参数决定调不调 sessionQuery。
 * 返回 null = 本调用不接 DSH 分支（服务缺席/开关关）；返回 'scope' =
 * 命中 rerun 提示；返回 promise = 并行执行后把结果挂上包络。
 * 服务缺席时保持静默（与 0.9.34 前行为一致），不刷「unavailable」噪音。
 */
export function dispatchDshBranch(kind, args, sq) {
  if (sq === null || sq === undefined) return null
  const agent = asString(args.agent, 'cross')
  // cross = 两路都要。dsh 也要接：Python FTS 对 DSH 是 deny-list 必然
  // 0 命中，agent=dsh 时唯一有意义的命中只能来自 DSH 腿（CLI 照跑，
  // 诚实回 0）——否则这个 agent 值是一条无提示的死路。
  if (agent !== 'cross' && agent !== 'dsh') return null
  if (kind === 'sd_search') {
    // CLI 对空查询会以 rejection='empty' 解释性拒绝；DSH 腿没有这层
    // 语义，跟在「no searchable content」后面只会自相矛盾。
    const query = asString(args.query)
    if (query === '') return null
    if (asString(args.scope, 'current') !== 'all') return 'scope'
    return searchDshSessions(sq, { query, limit: clampInt(args.limit, 5, 1, 20) })
  }
  if (kind === 'sd_sessions') {
    if (asString(args.scope, 'current') !== 'all') return 'scope'
    // 带 keyword 时走会话分组搜索：让 DSH 段与 CLI 段同受关键词过滤，
    // 否则未过滤的 newest-N 顶在关键词查询后面会被当命中引用。
    const keyword = asString(args.keyword)
    if (keyword !== '') {
      return searchDshSessions(sq, { query: keyword, limit: clampInt(args.limit, 20, 1, 50) })
    }
    return listDshSessions(sq, { limit: clampInt(args.limit, 20, 1, 50) })
  }
  if (kind === 'sd_stats') return countDshSessions(sq)
  return null
}

export function makeSdTool(kind, config, deps = {}) {
  const spec = NATIVE_TOOL_SPECS[kind]
  if (!spec) {
    throw new Error(
      `session-digger-dsh: unknown native tool "${kind}" `
      + `(known: ${Object.keys(NATIVE_TOOL_SPECS).join(', ')})`)
  }
  const run = deps.run ?? runCli
  const discover = deps.discover ?? discoverRoot
  const root = config.sessionDiggerRoot
    ?? (deps.root !== undefined ? deps.root : undefined)
  const timeoutMs = config.nativeTimeoutMs ?? DEFAULTS.nativeTimeoutMs
  const maxChars = config.nativeMaxChars ?? DEFAULTS.nativeMaxChars
  const pythonCommand = config.pythonCommand ?? DEFAULTS.pythonCommand

  return {
    name: kind,
    description: spec.description,
    parameters: spec.parameters,
    output: {
      schema: { type: 'object' },
    render: (_args, value) => {
      // execute 回 {stdout: sd-recall JSON 字符串, dsh?: 分支结果}；
      // 解析失败回退原始 stdout，截断防上下文膨胀（与 argo-dsh 同一兜底）。
      let body = ''
      try {
        const envelope = JSON.parse(value.stdout)
        if (value.dsh === undefined) {
          body = renderEnvelope(kind, envelope)
        } else {
          envelope.dsh = value.dsh
          const full = renderEnvelope(kind, envelope)
          if (full.length <= maxChars) {
            body = full
          } else {
            // 分节预算：整体截断会牺牲垫底的 DSH 段（最新鲜的信息最先
            // 没了）。先给 DSH 段留足长度，再把 CLI 段裁到余量。
            delete envelope.dsh
            const base = renderEnvelope(kind, envelope)
            const dshPart = full.slice(base.length)
            const budget = maxChars - dshPart.length
            body = budget > 40 ? clip(base, budget) + dshPart : dshPart
          }
        }
      } catch {
        body = value.stdout
      }
      return text(clip(body, maxChars))
    },
    },
    isConcurrencySafe: () => true,
    async execute(args) {
      const resolved = root ?? discover()
      if (!resolved) {
        throw new Error(
          'session-digger is not installed (no scripts/sd-recall.py found via '
          + 'SESSION_DIGGER_ROOT, common skill locations, or this checkout). '
          + 'Install it first: github:taxueseek/session-digger')
      }
      const argv = buildArgv(kind, args)
      // hybrid：两路并行。DSH 腿三重兜底——分支内 try/catch 不 throw、
      // 意外 rejection 转 {ok:false}、超过 nativeTimeoutMs 让位（首搜
      // 可能在建索引，openAt: first-search 的对账耗时无界）——保证
      // Python 结果永不被 DSH 腿卡死或吞掉。
      const sq = typeof deps.query === 'function' ? deps.query() : null
      const branch = dispatchDshBranch(kind, args, sq)
      let dshPromise = null
      if (branch === 'scope') {
        dshPromise = Promise.resolve('scope')
      } else if (branch !== null) {
        const guarded = branch.catch((error) => ({
          ok: false,
          error: asString(error?.message, '') !== ''
            ? error.message
            : 'unexpected DSH branch failure',
        }))
        let timer = null
        const budget = new Promise((settle) => {
          timer = setTimeout(() => settle({
            ok: false,
            error: `sessionQuery timed out after ${timeoutMs}ms (first-search index build in progress?)`,
          }), timeoutMs)
          if (typeof timer.unref === 'function') timer.unref()
        })
        void guarded.finally(() => clearTimeout(timer))
        dshPromise = Promise.race([guarded, budget])
      }
      const [cliResult, dsh] = await Promise.all([
        run(pythonCommand, [join(resolved, 'scripts', 'sd-recall.py'), ...argv], timeoutMs),
        dshPromise === null ? Promise.resolve(null) : dshPromise,
      ])
      // sd-recall 的 --json 只在 stdout 输出结构化结果；
      // 诊断行走 stderr。stdout 非 JSON = 契约破坏，响亮失败。
      JSON.parse(cliResult.stdout)
      return dshPromise === null
        ? cliResult
        : { stdout: cliResult.stdout, dsh }
    },
  }
}

const BUDGET_SECTION = [
  'session-digger 检索预算（sd_search / sd_sessions / sd_stats 返回已按此裁剪，勿擅自加宽）：',
  '- sd_search：limit 默认 5、上限 20；消息摘要每条 150 字、最多 8 条；工具错误 80 字 × 5；决策点 120 字 × 5。',
  '- sd_sessions：limit 默认 20、上限 50。',
  '- DSH native 段（agent=dsh，sessionQuery 实时检索）：每命中一条附 150 字片段；要全量上下文走 skill 形态的 deep。',
  '- 需要全文或跨会话深挖时，走 skill 形态的 sd-recall deep（--deep），不要在工具参数里堆 limit。',
  '- 返回的 Index 行是新鲜度契约：出现 stale 时先 index-builder.py build 再重试，否则「没有结果」描述的其实是旧世界。DSH native 段不走索引、永远新鲜。',
  '- 隐私边界：工具只回元数据与裁剪后的片段，不回完整转录；涉及密钥/隐私的会话内容不要追问全文。',
].join('\n')

export function apply(ctx, providedConfig = {}) {
  const config = { ...DEFAULTS, ...(typeof providedConfig === 'object' && providedConfig !== null ? providedConfig : {}) }

  // 配置 loud fail（官方「配置错误要响亮」）：开关与超时的类型
  // 错误必须在插件加载时报出，而不是第一次调用时静默降级。
  const nativeTools = Array.isArray(config.nativeTools) ? config.nativeTools : []
  for (const kind of nativeTools) {
    if (!NATIVE_TOOL_SPECS[kind]) {
      throw new Error(
        `session-digger-dsh: unknown native tool "${kind}" in config `
        + `(known: ${Object.keys(NATIVE_TOOL_SPECS).join(', ')})`)
    }
  }
  if (!Number.isFinite(config.nativeTimeoutMs) || config.nativeTimeoutMs <= 0) {
    throw new Error('session-digger-dsh: nativeTimeoutMs must be a positive number')
  }
  if (!Number.isFinite(config.nativeMaxChars) || config.nativeMaxChars <= 0) {
    throw new Error('session-digger-dsh: nativeMaxChars must be a positive number')
  }
  if (typeof config.pythonCommand !== 'string' || config.pythonCommand === '') {
    throw new Error('session-digger-dsh: pythonCommand must be a non-empty string')
  }
  if (typeof config.dshNative !== 'boolean') {
    throw new Error('session-digger-dsh: dshNative must be a boolean')
  }

  // hybrid 路由的 lazy resolver：调用时才碰 ctx（服务是运行时状态，
  // 加载时未必已激活）。开关关掉时连 ctx.get 都不做。
  const query = config.dshNative ? () => resolveSessionQuery(ctx) : null

  // 原生一等工具：CLI 单发形态。未知名在 makeSdTool 内 loud fail。
  for (const kind of nativeTools) {
    ctx.tools.register(makeSdTool(kind, config, { query }))
  }

  if (ctx.systemPrompt && typeof ctx.systemPrompt.section === 'function') {
    ctx.systemPrompt.section({
      name: 'tool:session-digger',
      order: 116.7,
      text: () => BUDGET_SECTION,
    })
  }
}

// Exported for unit tests; DSH loads the bundle through apply() only.
export { renderSearch, renderSessions, renderStats, renderEnvelope }
