type BoardBody = {
  state?: string
  items?: Array<{
    task_id?: string
    owner?: { display?: string; evidence?: string }
    recorded_at?: string | null
    history?: Array<{ occurred_at?: string }>
    tests?: unknown[]
    failure_reasons?: unknown[]
    advisory_failures?: unknown[]
  }>
  history?: Array<{ event_id?: string; occurred_at?: string; state?: string; authoritative?: boolean }>
  failures?: Array<{ display?: string; text?: string; at?: string; authoritative?: boolean }>
  tests?: Array<{ task_id?: string; state?: string; authoritative?: boolean }>
  actions?: Array<{ id?: string; label?: string; task_id?: string; enabled?: boolean; calls_authority?: boolean } | null>
} | null | undefined

export type BoardTaskRow = {
  task_id: string
  owner: string
  owner_evidence: string
  at: string | null
  tests: number
  failures: number
}

function board(body: unknown): BoardBody {
  if (!body || typeof body !== 'object') return null
  return body as BoardBody
}

export function boardRows(body: unknown): BoardTaskRow[] {
  const source = board(body)
  const items = Array.isArray(source?.items) ? source.items : []
  return items.map(item => {
    const history = Array.isArray(item.history) ? item.history : []
    const stamped = history.filter(row => typeof row.occurred_at === 'string')
    return {
      task_id: item.task_id || '未记录',
      owner: item.owner?.display || '未记录',
      owner_evidence: item.owner?.evidence || 'missing',
      at: item.recorded_at || (stamped.length ? stamped[stamped.length - 1].occurred_at || null : null),
      tests: Array.isArray(item.tests) ? item.tests.length : 0,
      failures: (item.failure_reasons || []).length + (item.advisory_failures || []).length,
    }
  })
}

export function historyRows(body: unknown) {
  const source = board(body)
  const rows = Array.isArray(source?.history) ? source.history : []
  return rows.map(row => ({
    event_id: row.event_id || '未记录',
    at: row.occurred_at || '未记录',
    state: row.state || '未记录',
    authoritative: row.authoritative === true,
  }))
}

export function failureRows(body: unknown) {
  const source = board(body)
  const rows = Array.isArray(source?.failures) ? source.failures : []
  return rows.map(row => ({
    text: row.display || row.text || '未记录',
    at: row.at || '未记录',
    authoritative: row.authoritative === true,
  }))
}

export function testRows(body: unknown) {
  const source = board(body)
  const rows = Array.isArray(source?.tests) ? source.tests : []
  return rows.map(row => ({
    task_id: row.task_id || '未记录',
    state: row.state || '未记录',
    authoritative: row.authoritative === true,
  }))
}

export function enabledActions(body: unknown) {
  const source = board(body)
  const actions = Array.isArray(source?.actions) ? source.actions : []
  return actions.filter((action): action is NonNullable<typeof action> => Boolean(action && action.enabled === true && action.calls_authority === true))
}

export function emptyBecauseNoRows(body: unknown) {
  const source = board(body)
  if (!source || source.state === 'denied') return false
  return boardRows(body).length === 0 && historyRows(body).length === 0 && failureRows(body).length === 0
}
