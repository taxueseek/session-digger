// session-digger-dsh unit tests (node:test, mirrors argo-dsh's discipline).
//
// Everything here runs without DSH: apply() is exercised against a
// minimal fake ctx, and the CLI single-shot path is exercised against
// a fake sd-recall.py (a python script that emits a fixed envelope),
// so the spawn → parse → render chain is tested end to end.

import { mkdtempSync, writeFileSync, mkdirSync, rmSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  PLUGIN_VERSION,
  DEFAULTS,
  discoverRoot,
  buildArgv,
  makeSdTool,
  apply,
  dispatchDshBranch,
  renderSearch,
  renderSessions,
  renderStats,
} from '../dsh/index.js'
import { NATIVE_TOOL_SPECS } from '../dsh/native-tools.mjs'
import {
  resolveSessionQuery,
  searchDshSessions,
  listDshSessions,
  countDshSessions,
} from '../dsh/native/session-query.mjs'

// ── version resolution ─────────────────────────────────

test('PLUGIN_VERSION is read from package.json at runtime', () => {
  const manifest = JSON.parse(
    readFileSync(new URL('../package.json', import.meta.url), 'utf8'))
  assert.equal(PLUGIN_VERSION, manifest.version)
  assert.match(PLUGIN_VERSION, /^\d+\.\d+\.\d+$/)
})

// ── root discovery ─────────────────────────────────────

test('discoverRoot honors SESSION_DIGGER_ROOT', () => {
  const root = mkdtempSync(join(tmpdir(), 'sd-root-'))
  mkdirSync(join(root, 'scripts'), { recursive: true })
  writeFileSync(join(root, 'scripts', 'sd-recall.py'), '')
  try {
    assert.equal(
      discoverRoot({ SESSION_DIGGER_ROOT: root }, '/nonexistent-home'),
      root)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test('discoverRoot falls back to common skill locations', () => {
  const home = mkdtempSync(join(tmpdir(), 'sd-home-'))
  const root = join(home, '.agents', 'skills', 'session-digger')
  mkdirSync(join(root, 'scripts'), { recursive: true })
  writeFileSync(join(root, 'scripts', 'sd-recall.py'), '')
  try {
    assert.equal(discoverRoot({}, home), root)
  } finally {
    rmSync(home, { recursive: true, force: true })
  }
})

test('discoverRoot falls back to the checkout the package lives in (link: installs)', () => {
  // 第三优先级：本包在 <repo>/dsh-plugin/dsh 里时，仓库根
  // 自动生效——link: 安装不需要任何配置。
  const home = mkdtempSync(join(tmpdir(), 'sd-home-'))
  try {
    const fakeHere = join(import.meta.dirname, '..', 'dsh')
    assert.equal(discoverRoot({}, home, fakeHere),
      join(import.meta.dirname, '..', '..'))
  } finally {
    rmSync(home, { recursive: true, force: true })
  }
})

test('discoverRoot returns null when session-digger is absent', () => {
  const home = mkdtempSync(join(tmpdir(), 'sd-empty-home-'))
  const nowhere = mkdtempSync(join(tmpdir(), 'sd-nowhere-'))
  try {
    // here 指向一个与任何 checkout 无关的目录：三层探测全部落空。
    assert.equal(discoverRoot({}, home, join(nowhere, 'dsh')), null)
  } finally {
    rmSync(home, { recursive: true, force: true })
    rmSync(nowhere, { recursive: true, force: true })
  }
})

// ── argv construction ──────────────────────────────────

test('buildArgv pins the sd_search CLI contract', () => {
  const argv = buildArgv('sd_search', {
    query: '数据库', scope: 'all', limit: 99, agent: 'dimcode',
    decisions: true, include_tools: false,
  })
  assert.deepEqual(argv, [
    'search', '--json', '数据库', '--scope', 'all',
    '--limit', '20', '--agent', 'dimcode', '--decisions',
  ])
})

test('buildArgv defaults match the CLI defaults', () => {
  assert.deepEqual(buildArgv('sd_sessions', {}), [
    'sessions', '--json', '--scope', 'current', '--limit', '20',
    '--agent', 'cross',
  ])
  assert.deepEqual(buildArgv('sd_stats', {}), [
    'stats', '--json', '--agent', 'cross',
  ])
})

test('buildArgv loud-fails on an unknown tool', () => {
  assert.throws(() => buildArgv('sd_nope', {}), /unknown native tool/)
})

// ── tool factory ───────────────────────────────────────

test('makeSdTool loud-fails on an unknown tool', () => {
  assert.throws(() => makeSdTool('sd_nope', {}), /unknown native tool/)
})

// ── apply(): registration + config validation ─────────

function fakeCtx() {
  const ctx = {
    tools: { registered: [], register(tool) { this.registered.push(tool) } },
    systemPrompt: { sections: [], section(s) { this.sections.push(s) } },
    effect() {},
  }
  return ctx
}

test('apply registers the default three tools and the budget section', () => {
  const ctx = fakeCtx()
  apply(ctx, {})
  assert.deepEqual(ctx.tools.registered.map(t => t.name),
    ['sd_search', 'sd_sessions', 'sd_stats'])
  assert.equal(ctx.systemPrompt.sections.length, 1)
  assert.match(ctx.systemPrompt.sections[0].text(), /检索预算/)
})

test('apply honors an explicit nativeTools list', () => {
  const ctx = fakeCtx()
  apply(ctx, { nativeTools: ['sd_stats'] })
  assert.deepEqual(ctx.tools.registered.map(t => t.name), ['sd_stats'])
})

test('apply loud-fails on an unknown nativeTools entry', () => {
  assert.throws(() => apply(fakeCtx(), { nativeTools: ['sd_nope'] }),
    /unknown native tool/)
})

test('apply loud-fails on a non-positive timeout', () => {
  assert.throws(() => apply(fakeCtx(), { nativeTimeoutMs: 0 }),
    /nativeTimeoutMs/)
})

// ── execute(): CLI single-shot against a fake sd-recall ─

const FAKE_ENVELOPE = JSON.stringify({
  command: 'search', query: 'q', scope: 'all', limit: 5,
  agent: 'cross', index: 'HIT, data to 2026-10-01',
  found: 1, elapsed_s: 0.06,
  sessions: [{
    session_id: 'test:sess_a', agent: 'test', modified: '2026-10-01 10:00',
    messages: 3, tools: 1, errors: 0, branch: 'main',
    summary: 'fake summary',
    evidence: {
      user_messages: [{ timestamp: '2026-10-01T10:00:00', text: 'fake message' }],
      tool_errors: [],
    },
  }],
})

function fakeRoot(envelope = FAKE_ENVELOPE) {
  const root = mkdtempSync(join(tmpdir(), 'sd-fake-'))
  mkdirSync(join(root, 'scripts'), { recursive: true })
  writeFileSync(join(root, 'scripts', 'sd-recall.py'),
    `import json,sys\nprint(${JSON.stringify(envelope)})\n`)
  return root
}

test('execute spawns the CLI, parses the envelope, renders CLI-identical text', async () => {
  const root = fakeRoot()
  try {
    const tool = makeSdTool('sd_search', {}, { root, discover: () => root })
    const result = await tool.execute({ query: '数据库', scope: 'all' })
    const envelope = JSON.parse(result.stdout)
    assert.equal(envelope.found, 1)
    const rendered = tool.output.render({}, result)[0].text
    assert.match(rendered, /Found 1 session\(s\)\. Index: HIT/)
    assert.match(rendered, /test:sess_a/)
    assert.match(rendered, /Summary: fake summary/)
    assert.match(rendered, /User messages \(1\):/)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test('execute throws an actionable error when session-digger is absent', async () => {
  const tool = makeSdTool('sd_search', {}, { discover: () => null })
  await assert.rejects(
    () => tool.execute({ query: 'x' }),
    /session-digger is not installed/)
})

test('execute surfaces a non-zero exit with the stderr tail', async () => {
  const root = mkdtempSync(join(tmpdir(), 'sd-fail-'))
  mkdirSync(join(root, 'scripts'), { recursive: true })
  writeFileSync(join(root, 'scripts', 'sd-recall.py'),
    'import sys\nsys.stderr.write("boom: index locked")\nsys.exit(3)\n')
  try {
    const tool = makeSdTool('sd_stats', {}, { root, discover: () => root })
    await assert.rejects(() => tool.execute({}), /exited 3: boom: index locked/)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test('execute kills the CLI past the timeout', async () => {
  const root = mkdtempSync(join(tmpdir(), 'sd-hang-'))
  mkdirSync(join(root, 'scripts'), { recursive: true })
  writeFileSync(join(root, 'scripts', 'sd-recall.py'),
    'import time\ntime.sleep(60)\n')
  try {
    const tool = makeSdTool('sd_stats', { nativeTimeoutMs: 300 },
      { root, discover: () => root })
    await assert.rejects(() => tool.execute({}), /timed out/)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

// ── renderers: text parity with the CLI ────────────────

test('renderSearch renders the empty-search state with both widen axes', () => {
  const text = renderSearch({
    command: 'search', query: '第一性原理', scope: 'current',
    limit: 5, agent: 'cross', index: 'HIT', found: 0,
    empty: {
      keyword: '第一性原理', scope: 'current', agent: 'cross',
      index: 'HIT', rejection: '',
      facet_counts: { USER: 5844, ASSISTANT: 3520 }, facet_total: 9364,
    },
    elapsed_s: 0.05,
  })
  assert.match(text, /No match for '第一性原理' in scope 'current'\./)
  assert.match(text, /Index does hold 9364 row\(s\) elsewhere/)
  assert.match(text, /Widen: rerun with --scope all/)
})

test('renderSessions renders the TSV rows', () => {
  const text = renderSessions({
    command: 'sessions', found: 1,
    sessions: [{
      session_id: 'test:sess_a', created: '2026-10-01',
      modified: '2026-10-01 10:00', messages: 5,
      branch: 'main', agent: 'test', path: '/tmp/a.jsonl',
    }],
  })
  assert.match(text, /SESSION_ID\tCREATED\tMODIFIED\tMSGS\tBRANCH\tAGENT\tPATH/)
  assert.match(text, /test:sess_a\t2026-10-01\t2026-10-01 10:00\t5\tmain\ttest\t\/tmp\/a\.jsonl/)
})

test('renderStats renders totals and the env breakdown', () => {
  const text = renderStats({
    command: 'stats', agent: 'cross', sessions: 7329,
    messages: 180861, tool_calls: 95892, tool_errors: 3133,
    total_tokens: 9876543210,
    index: { sessions: 7329, messages_indexed: 111183 },
    env_breakdown: [{ agent: 'dimcode', sessions: 4648, tool_calls: 8, errors: 1 }],
  })
  assert.match(text, /Total sessions: 7329/)
  assert.match(text, /Total tokens: 9876543210/)
  assert.match(text, /Index: 7329 sessions, 111183 messages indexed/)
  assert.match(text, /dimcode: 4648 sessions, 8 tools, 1 errors/)
})

// ── spec sanity: the tool surface is the CLI surface ──

test('every native tool maps to a real sd-recall subcommand', () => {
  for (const [kind, spec] of Object.entries(NATIVE_TOOL_SPECS)) {
    assert.ok(spec.cli, `${kind} maps to a CLI subcommand`)
    assert.ok(spec.parameters.required === undefined
      || spec.parameters.required.length >= 0)
    assert.equal(spec.parameters.type, 'object')
  }
  assert.deepEqual(Object.keys(NATIVE_TOOL_SPECS), DEFAULTS.nativeTools)
})

// ── hybrid routing: ctx.sessionQuery 可选分支 ──────────

function engineStub({ search, list } = {}) {
  return {
    searchSessions: search ?? (async () => ({ items: [] })),
    listSessions: list ?? (async () => []),
  }
}

test('resolveSessionQuery accepts a ctx.get-served engine', () => {
  const engine = engineStub()
  assert.equal(resolveSessionQuery({ get: (key) => key === 'sessionQuery' ? engine : undefined }), engine)
})

test('resolveSessionQuery accepts a property-served engine', () => {
  const engine = engineStub()
  assert.equal(resolveSessionQuery({ sessionQuery: engine }), engine)
})

test('resolveSessionQuery returns null when the service is absent', () => {
  assert.equal(resolveSessionQuery({}), null)
  assert.equal(resolveSessionQuery({ get: () => undefined }), null)
  assert.equal(resolveSessionQuery(null), null)
  assert.equal(resolveSessionQuery({ get: () => { throw new Error('host mismatch') } }), null)
})

test('resolveSessionQuery rejects a half-shaped engine', () => {
  // 契约变了宁可降级：缺任一要用的方法都不算可用。
  assert.equal(resolveSessionQuery({ get: () => ({ searchSessions: async () => {} }) }), null)
  assert.equal(resolveSessionQuery({ get: () => ({ listSessions: async () => {} }) }), null)
  assert.equal(resolveSessionQuery({ get: () => 'engine' }), null)
})

test('searchDshSessions unwraps page items', async () => {
  const sq = engineStub({ search: async () => ({ items: [{ header: { id: 's1' } }] }) })
  assert.deepEqual(await searchDshSessions(sq, { query: 'x' }),
    { ok: true, items: [{ header: { id: 's1' } }] })
})

test('searchDshSessions reports failures instead of throwing', async () => {
  const sq = engineStub({ search: async () => { throw new Error('index locked') } })
  const result = await searchDshSessions(sq, { query: 'x' })
  assert.equal(result.ok, false)
  assert.match(result.error, /index locked/)
})

test('listDshSessions slices to the limit', async () => {
  const corpus = Array.from({ length: 30 }, (_, i) => ({ header: { id: `s${i}` } }))
  const sq = engineStub({ list: async () => corpus })
  const result = await listDshSessions(sq, { limit: 5 })
  assert.equal(result.ok, true)
  assert.equal(result.items.length, 5)
  assert.deepEqual(result.items.map(r => r.header.id), ['s0', 's1', 's2', 's3', 's4'])
})

test('countDshSessions counts the corpus', async () => {
  const sq = engineStub({ list: async () => [{ header: { id: 'a' } }, { header: { id: 'b' } }] })
  assert.deepEqual(await countDshSessions(sq), { ok: true, count: 2 })
})

test('countDshSessions reports failures instead of throwing', async () => {
  const sq = engineStub({ list: async () => { throw new Error('corpus boom') } })
  const result = await countDshSessions(sq)
  assert.equal(result.ok, false)
  assert.match(result.error, /corpus boom/)
})

test('dispatchDshBranch stays silent when the service is absent', () => {
  assert.equal(dispatchDshBranch('sd_search', { query: 'x', scope: 'all' }, null), null)
  assert.equal(dispatchDshBranch('sd_search', { query: 'x', scope: 'all' }, undefined), null)
  assert.equal(dispatchDshBranch('sd_stats', {}, undefined), null)
})

test('dispatchDshBranch marks scope=current as a rerun hint, not a query', () => {
  const sq = engineStub()
  assert.equal(dispatchDshBranch('sd_search', { query: 'x' }, sq), 'scope')
  assert.equal(dispatchDshBranch('sd_sessions', {}, sq), 'scope')
})

test('dispatchDshBranch skips when a foreign agent is requested', () => {
  const sq = engineStub()
  assert.equal(dispatchDshBranch('sd_search', { query: 'x', scope: 'all', agent: 'claude' }, sq), null)
  assert.equal(dispatchDshBranch('sd_stats', { agent: 'claude' }, sq), null)
})

test('dispatchDshBranch serves agent=dsh — the CLI FTS deny-lists DSH, only the native leg can hit', () => {
  const sq = engineStub()
  const promise = dispatchDshBranch('sd_search', { query: 'x', scope: 'all', agent: 'dsh' }, sq)
  assert.equal(typeof promise.then, 'function')
  return promise.then((result) => assert.equal(result.ok, true))
})

test('dispatchDshBranch skips an empty query for sd_search', () => {
  // CLI 对空查询以 rejection='empty' 解释性拒绝；DSH 腿跟在后面只会自相矛盾。
  const sq = engineStub()
  assert.equal(dispatchDshBranch('sd_search', { query: '   ', scope: 'all' }, sq), null)
})

test('dispatchDshBranch filters sd_sessions by keyword through session search', async () => {
  let searchedWith = null
  let listed = false
  const sq = {
    searchSessions: async (request) => { searchedWith = request; return { items: [] } },
    listSessions: async () => { listed = true; return [] },
  }
  const promise = dispatchDshBranch('sd_sessions', { keyword: '路由', scope: 'all', limit: 7 }, sq)
  assert.equal(typeof promise.then, 'function')
  await promise
  assert.deepEqual(searchedWith, { query: '路由', limit: 7 })
  assert.equal(listed, false) // keyword 在手时不得回未过滤的 newest-N
})

test('dispatchDshBranch returns live promises for scope=all', () => {
  const sq = engineStub()
  const promise = dispatchDshBranch('sd_search', { query: 'x', scope: 'all' }, sq)
  assert.equal(typeof promise.then, 'function')
  return promise.then((result) => assert.equal(result.ok, true))
})

// ── hybrid rendering: DSH 段是插件形态的显式附加 ───────

test('renderSearch appends a labeled DSH section after the CLI results', () => {
  const text = renderSearch({
    command: 'search', query: '路由', scope: 'all', limit: 5,
    agent: 'cross', index: 'HIT', found: 1, elapsed_s: 0.06,
    sessions: [{ session_id: 'test:sess_a', agent: 'test', modified: '2026-10-01 10:00',
      messages: 3, tools: 1, errors: 0, branch: 'main', evidence: {} }],
    dsh: { ok: true, items: [{
      header: { id: 'dsh:sess_9', cwd: '/workspace/GPT', createdAt: 1792814322369 },
      live: true, persisted: true,
      bestMatch: { seq: 42, time: 1792814322369, snippet: 'hybrid 路由的实现' },
    }] },
  })
  assert.match(text, /Found 1 session\(s\)/)
  assert.match(text, /DSH native \(sessionQuery, live — not covered by the Python index; includes tool events\): 1 session\(s\)/)
  assert.match(text, /dsh:sess_9 \(dsh, /)
  assert.match(text, /hybrid 路由的实现/)
})

test('renderSearch marks a zero-hit DSH side as no match, not a count line', () => {
  const text = renderSearch({
    command: 'search', query: 'x', scope: 'all', limit: 5,
    agent: 'cross', index: 'HIT', found: 1,
    sessions: [{ session_id: 'test:sess_a', agent: 'test', modified: '?',
      messages: 1, tools: 0, errors: 0, branch: '', evidence: {} }],
    dsh: { ok: true, items: [] },
  })
  assert.match(text, /DSH native \(sessionQuery[^)]*\): no match\./)
  assert.doesNotMatch(text, /: 0 session\(s\)/)
})

test('renderSearch surfaces DSH hits even when the Python index missed', () => {
  // hybrid 的核心场景：索引没建到的会话在 DSH 原生侧命中。
  const text = renderSearch({
    command: 'search', query: 'sessionQuery', scope: 'all', limit: 5,
    agent: 'cross', index: 'HIT', found: 0,
    empty: { keyword: 'sessionQuery', scope: 'all', agent: 'cross', index: 'HIT',
      rejection: '', facet_counts: {}, facet_total: 0 },
    dsh: { ok: true, items: [{
      header: { id: 'dsh:sess_1', cwd: '/tmp', createdAt: 1792814322369 },
      live: false, persisted: true,
      bestMatch: { seq: 3, time: 1792814322369, snippet: 'native hit' },
    }] },
  })
  assert.match(text, /No match for 'sessionQuery' in scope 'all'\./)
  assert.match(text, /DSH native \(sessionQuery, live/)
  assert.match(text, /native hit/)
})

test('renderSearch explains scope=current exclusion honestly', () => {
  const text = renderSearch({
    command: 'search', query: 'x', scope: 'current', limit: 5,
    agent: 'cross', index: 'HIT', found: 0,
    empty: { keyword: 'x', scope: 'current', agent: 'cross', index: 'HIT',
      rejection: '', facet_counts: {}, facet_total: 0 },
    dsh: 'scope',
  })
  assert.match(text, /not searched for scope='current' — native search covers every workspace; rerun with --scope all/)
})

test('renderSearch reports a failing DSH branch without losing CLI output', () => {
  const text = renderSearch({
    command: 'search', query: 'x', scope: 'all', limit: 5,
    agent: 'cross', index: 'HIT', found: 1,
    sessions: [{ session_id: 'test:sess_a', agent: 'test', modified: '?',
      messages: 1, tools: 0, errors: 0, branch: '', evidence: {} }],
    dsh: { ok: false, error: 'SESSION_QUERY_SEARCH_DISABLED' },
  })
  assert.match(text, /test:sess_a/)
  assert.match(text, /DSH native \(sessionQuery\): unavailable — SESSION_QUERY_SEARCH_DISABLED/)
})

test('renderSessions appends the DSH TSV block', () => {
  const text = renderSessions({
    command: 'sessions', found: 1,
    sessions: [{ session_id: 'test:sess_a', created: '2026-10-01',
      modified: '2026-10-01 10:00', messages: 5, branch: 'main', agent: 'test', path: '/tmp/a.jsonl' }],
    dsh: { ok: true, items: [
      { header: { id: 'dsh:s1', cwd: '/workspace/A', createdAt: 1792814322369 }, live: true, persisted: true },
      { header: { id: 'dsh:s2', cwd: '/workspace/B', createdAt: 1792814000000 }, live: false, persisted: true },
    ] },
  })
  assert.match(text, /--- 1 session\(s\) ---/)
  assert.match(text, /DSH native \(sessionQuery, live/)
  assert.match(text, /dsh:s1\t\d{4}-\d{2}-\d{2} \d{2}:\d{2}\t\d{4}-\d{2}-\d{2} \d{2}:\d{2}\t\?\t\tdsh\t\/workspace\/A/)
  assert.match(text, /--- 2 DSH session\(s\) ---/)
})

test('renderStats appends the DSH count line', () => {
  const text = renderStats({
    command: 'stats', agent: 'cross', sessions: 10, messages: 100,
    tool_calls: 50, tool_errors: 2, total_tokens: 1000,
    dsh: { ok: true, count: 675 },
  })
  assert.match(text, /dsh \(sessionQuery\): 675 sessions \(native, live index — not in the counts above\)/)
})

test('renderStats reports an unavailable DSH service', () => {
  const text = renderStats({
    command: 'stats', agent: 'cross', sessions: 10, messages: 100,
    tool_calls: 50, tool_errors: 2, total_tokens: 1000,
    dsh: { ok: false, error: 'service missing' },
  })
  assert.match(text, /dsh \(sessionQuery\): unavailable — service missing/)
})

// ── hybrid execute: fake engine 并行合并进包络 ─────────

test('execute merges DSH hits into the envelope when the engine is served', async () => {
  const root = fakeRoot()
  try {
    const engine = engineStub({
      search: async () => ({ items: [{
        header: { id: 'dsh:sess_9', cwd: '/tmp', createdAt: 1792814322369 },
        live: true, persisted: true,
        bestMatch: { seq: 1, time: 1792814322369, snippet: 'merged hit' },
      }] }),
    })
    const tool = makeSdTool('sd_search', {},
      { root, discover: () => root, query: () => engine })
    const result = await tool.execute({ query: '路由', scope: 'all' })
    assert.equal(JSON.parse(result.stdout).found, 1) // Python 路不因 hybrid 而变
    assert.equal(result.dsh.ok, true)
    const rendered = tool.output.render({}, result)[0].text
    assert.match(rendered, /test:sess_a/)
    assert.match(rendered, /dsh:sess_9/)
    assert.match(rendered, /merged hit/)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test('execute skips the DSH branch for scope=current', async () => {
  const root = fakeRoot()
  try {
    const tool = makeSdTool('sd_search', {},
      { root, discover: () => root, query: () => engineStub() })
    const result = await tool.execute({ query: 'x' }) // 默认 scope=current
    assert.equal(result.dsh, 'scope')
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test('execute keeps pure-CLI output when the resolver returns null', async () => {
  const root = fakeRoot()
  try {
    const tool = makeSdTool('sd_search', {},
      { root, discover: () => root, query: () => null })
    const result = await tool.execute({ query: 'x', scope: 'all' })
    assert.equal(result.dsh, undefined)
    const rendered = tool.output.render({}, result)[0].text
    assert.doesNotMatch(rendered, /DSH native/)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test('execute times out a stuck DSH leg and still delivers the CLI result', async () => {
  const root = fakeRoot()
  try {
    const stuck = { searchSessions: () => new Promise(() => {}) }
    const tool = makeSdTool('sd_search', { nativeTimeoutMs: 120 },
      { root, discover: () => root, query: () => stuck })
    const result = await tool.execute({ query: 'x', scope: 'all' })
    assert.equal(result.dsh.ok, false)
    assert.match(result.dsh.error, /sessionQuery timed out after 120ms/)
    assert.equal(JSON.parse(result.stdout).found, 1) // Python 结果不被 DSH 腿卡死
    const rendered = tool.output.render({}, result)[0].text
    assert.match(rendered, /test:sess_a/)
    assert.match(rendered, /unavailable — sessionQuery timed out/)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test('execute survives a hostile DSH-leg rejection and still delivers the CLI result', async () => {
  const root = fakeRoot()
  try {
    // 模块层 catch 负责把任何 rejection 转成 {ok:false, error}——
    // execute 层的 guarded catch 只是纵深防御，公共面到不了。
    const hostile = { searchSessions: () => Promise.reject('raw string failure') }
    const tool = makeSdTool('sd_search', {},
      { root, discover: () => root, query: () => hostile })
    const result = await tool.execute({ query: 'x', scope: 'all' })
    assert.equal(result.dsh.ok, false)
    assert.match(result.dsh.error, /raw string failure/)
    assert.equal(JSON.parse(result.stdout).found, 1)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test('execute merges keyword-filtered DSH hits into sd_sessions', async () => {
  const root = fakeRoot(JSON.stringify({
    command: 'sessions', scope: 'all', limit: 20, agent: 'cross',
    keyword: '路由', found: 0, sessions: [],
  }))
  try {
    const engine = engineStub({
      search: async () => ({ items: [{
        header: { id: 'dsh:sess_kw', cwd: '/tmp', createdAt: 1792814322369 },
        live: true, persisted: true,
        bestMatch: { seq: 1, time: 1792814322369, snippet: 'keyword hit' },
      }] }),
    })
    const tool = makeSdTool('sd_sessions', {},
      { root, discover: () => root, query: () => engine })
    const result = await tool.execute({ keyword: '路由', scope: 'all' })
    assert.equal(result.dsh.ok, true)
    const rendered = tool.output.render({}, result)[0].text
    assert.match(rendered, /--- 0 session\(s\) ---/)
    assert.match(rendered, /dsh:sess_kw\t/)
    assert.match(rendered, /--- 1 DSH session\(s\) ---/)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test('render keeps the DSH section when the CLI section alone would exhaust maxChars', () => {
  // 分节预算：整体截断会先牺牲垫底的 DSH 段；CLI 段必须让位。
  const longSummary = 'x'.repeat(30_000)
  const tool = makeSdTool('sd_search', { nativeMaxChars: 4000 }, {})
  const value = {
    stdout: JSON.stringify({
      command: 'search', query: 'q', scope: 'all', limit: 5, agent: 'cross',
      index: 'HIT', found: 1, elapsed_s: 0.06,
      sessions: [{ session_id: 'test:big', agent: 'test', modified: '?',
        messages: 1, tools: 0, errors: 0, branch: '', summary: longSummary, evidence: {} }],
    }),
    dsh: { ok: true, items: [{
      header: { id: 'dsh:must_survive', cwd: '/tmp', createdAt: 1792814322369 },
      live: true, persisted: true,
      bestMatch: { seq: 1, time: 1792814322369, snippet: 'fresh side' },
    }] },
  }
  const rendered = tool.output.render({}, value)[0].text
  assert.ok(rendered.length <= 4000)
  assert.match(rendered, /dsh:must_survive/)
  assert.match(rendered, /fresh side/)
})

test('execute still surfaces CLI failures when the DSH branch succeeds', async () => {
  const root = mkdtempSync(join(tmpdir(), 'sd-fail-'))
  mkdirSync(join(root, 'scripts'), { recursive: true })
  writeFileSync(join(root, 'scripts', 'sd-recall.py'),
    'import sys\nsys.stderr.write("boom")\nsys.exit(3)\n')
  try {
    const tool = makeSdTool('sd_stats', {},
      { root, discover: () => root, query: () => engineStub() })
    await assert.rejects(() => tool.execute({}), /exited 3/)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test('execute merges the DSH count into sd_stats', async () => {
  const root = fakeRoot(JSON.stringify({
    command: 'stats', agent: 'cross', sessions: 10, messages: 100,
    tool_calls: 50, tool_errors: 2, total_tokens: 1000,
  }))
  try {
    const tool = makeSdTool('sd_stats', {},
      { root, discover: () => root, query: () => engineStub({ list: async () => new Array(7).fill({ header: {} }) }) })
    const result = await tool.execute({})
    assert.deepEqual(result.dsh, { ok: true, count: 7 })
    const rendered = tool.output.render({}, result)[0].text
    assert.match(rendered, /dsh \(sessionQuery\): 7 sessions/)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test('apply passes the hybrid resolver and validates dshNative', () => {
  const ctx = fakeCtx()
  const engine = engineStub()
  ctx.get = (key) => key === 'sessionQuery' ? engine : undefined
  apply(ctx, {})
  for (const tool of ctx.tools.registered) {
    assert.equal(typeof tool.execute, 'function')
  }
  assert.throws(() => apply(fakeCtx(), { dshNative: 'yes' }), /dshNative must be a boolean/)
})
