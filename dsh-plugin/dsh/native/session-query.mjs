// DSH 原生会话分支（hybrid 路由的可选加速层）。
//
// ctx.sessionQuery 是宿主的可信查询服务（@deepseek-ai/dsh-session-query，
// FTS5 后端为 @deepseek-ai/dsh-session-query-sqlite）。它只覆盖 DSH 自己
// 的会话——Python 索引覆盖其余 16+ 环境但够不到 DSH（zstd 事件存储，
// scripts/index_builder/_session_analysis.py 的 deny-list 有实测记录：
// 盲读 610 条消息报 0）。两路互补不重叠。
//
// 形态纪律的注记：CLI 是外部进程，到不了宿主进程内服务，所以 DSH 行
// 只出现在插件形态，渲染时以 agent='dsh' 明确标记来源。这是双形态间
// 唯一的有意分歧，其余输出仍与 CLI 逐行同构。
//
// 防御规则（与 argo-dsh 的 ctx.get 模式同款）：
//   - 服务未挂载/不是引擎形状 → 返回 null，插件保持纯 Python 行为；
//   - 服务存在但调用失败 → 返回 { ok: false, error }，绝不 throw，
//     Python 路径的结果永不因 DSH 分支失败而丢失。

/** 从插件 ctx 解析 sessionQuery；不可用返回 null，绝不 throw。 */
export function resolveSessionQuery(ctx) {
  let service
  try {
    service = typeof ctx?.get === 'function' ? ctx.get('sessionQuery') : ctx?.sessionQuery
  } catch {
    return null
  }
  if (service === null || typeof service !== 'object') return null
  // 只认长得像引擎的对象：本插件要用的每个方法都在才算可用，
  // 半个方法缺失说明宿主版本契约变了，宁可降级也不半路崩。
  if (typeof service.searchSessions !== 'function') return null
  if (typeof service.listSessions !== 'function') return null
  return service
}

/** 跨会话全文搜索（按最强命中事件分组排序）。 */
export async function searchDshSessions(sq, { query, limit = 5 } = {}) {
  try {
    const page = await sq.searchSessions({ query, limit })
    return { ok: true, items: Array.isArray(page?.items) ? page.items : [] }
  } catch (error) {
    return { ok: false, error: errorMessage(error) }
  }
}

/** 最新会话列表（header 级，newest-first）。 */
export async function listDshSessions(sq, { limit = 20 } = {}) {
  try {
    const records = await sq.listSessions()
    return { ok: true, items: Array.isArray(records) ? records.slice(0, limit) : [] }
  } catch (error) {
    return { ok: false, error: errorMessage(error) }
  }
}

/** 会话总数（stats 用；listSessions 回的是 header 级记录，枚举即计数）。 */
export async function countDshSessions(sq) {
  try {
    const records = await sq.listSessions()
    return { ok: true, count: Array.isArray(records) ? records.length : 0 }
  } catch (error) {
    return { ok: false, error: errorMessage(error) }
  }
}

function errorMessage(error) {
  try {
    const message = String(error?.message ?? error).trim()
    return message === '' ? 'unknown error' : message
  } catch {
    return 'unprintable error'
  }
}
