import type { ProjectInfo } from '../../ProjectInfoPage'
import { MISSING } from './content.ts'

export type RunningVersionFacts = {
  version?: string | null
  running_sha?: string | null
}

export type GuideFacts = {
  name: string
  summary: string
  version: string
  sha: string
  environment: string
  publicAddress: string
  checkedAt: string
}

function text(value: unknown): string {
  if (typeof value !== 'string') return MISSING
  const trimmed = value.trim()
  return trimmed ? trimmed : MISSING
}

export function readGuideFacts(project: ProjectInfo | null | undefined, running?: RunningVersionFacts | null): GuideFacts {
  const current = project?.foundation_version?.current
  const version = text(running?.version ?? current?.version)
  const sha = text(running?.running_sha ?? current?.sha)
  const checked = project?.foundation_version?.checked_at
  const parsed = typeof checked === 'string' ? Date.parse(checked) : Number.NaN
  return {
    name: text(project?.name),
    summary: text(project?.summary),
    version,
    sha,
    environment: MISSING,
    publicAddress: MISSING,
    checkedAt: Number.isNaN(parsed) ? MISSING : new Date(parsed).toLocaleString('zh-CN', { hour12: false }),
  }
}
