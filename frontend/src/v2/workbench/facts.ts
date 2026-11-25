import type { ProjectInfo } from '../../ProjectInfoPage'
import { HEALTH_ROWS, METRICS, MISSING, type HealthKey, type HealthState, type MetricKey } from './content.ts'

export type RunningVersionFacts = {
  version?: string | null
  running_sha?: string | null
}

export type WorkbenchMetrics = Partial<Record<MetricKey, number | null>>

export type WorkbenchHealth = Partial<Record<HealthKey, { state?: HealthState | null; detail?: string | null } | null>>

export type WorkbenchTrendPoint = {
  label: string
  created: number | null
  finished: number | null
}

export type WorkbenchActivity = {
  at: string
  text: string
}

export type WorkbenchVersions = {
  running?: string | null
  candidate?: string | null
  candidateState?: string | null
  publishedAt?: string | null
}

function text(value: unknown): string {
  if (typeof value !== 'string') return MISSING
  const trimmed = value.trim()
  return trimmed ? trimmed : MISSING
}

export function readHeader(project: ProjectInfo | null | undefined, running?: RunningVersionFacts | null) {
  const current = project?.foundation_version?.current
  return {
    version: text(running?.version ?? current?.version),
    sha: text(running?.running_sha ?? current?.sha),
  }
}

export function metricText(metrics: WorkbenchMetrics | null | undefined, key: MetricKey): string {
  const value = metrics?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : MISSING
}

export function progressParts(metrics: WorkbenchMetrics | null | undefined) {
  const keys = ['done', 'active', 'review', 'blocked'] as const
  const values = keys.map(key => metrics?.[key])
  if (values.some(value => typeof value !== 'number' || !Number.isFinite(value))) return null
  const numbers = values as number[]
  const total = numbers.reduce((sum, value) => sum + value, 0)
  if (total <= 0) return null
  return {
    total,
    done: numbers[0],
    percent: Math.round((numbers[0] / total) * 100),
  }
}

export function healthText(health: WorkbenchHealth | null | undefined, key: HealthKey): { label: string; text: string; state: HealthState } {
  const row = HEALTH_ROWS.find(item => item.key === key)!
  const item = health?.[key]
  const detail = text(item?.detail)
  const state = item?.state === 'ok' || item?.state === 'warn' || item?.state === 'error' ? item.state : 'unknown'
  return { label: row.label, text: detail === MISSING && state === 'unknown' ? MISSING : detail === MISSING ? stateLabel(state) : detail, state }
}

function stateLabel(state: HealthState): string {
  if (state === 'ok') return '正常'
  if (state === 'warn') return '关注'
  if (state === 'error') return '异常'
  return MISSING
}

export function versionText(versions: WorkbenchVersions | null | undefined) {
  return {
    running: text(versions?.running),
    candidate: text(versions?.candidate),
    candidateState: text(versions?.candidateState),
    publishedAt: text(versions?.publishedAt),
  }
}

export { METRICS }
