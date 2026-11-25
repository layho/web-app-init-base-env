import {
  CheckCircleOutlined,
  CodeOutlined,
  ExclamationCircleOutlined,
  HistoryOutlined,
  HomeOutlined,
  ReadOutlined,
  SyncOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import type { ComponentType } from 'react'
import type { ProjectInfo } from '../../ProjectInfoPage'
import { LIGHT_NAV, METRICS } from './content.ts'
import {
  healthText,
  metricText,
  progressParts,
  readHeader,
  versionText,
  type RunningVersionFacts,
  type WorkbenchActivity,
  type WorkbenchHealth,
  type WorkbenchMetrics,
  type WorkbenchTrendPoint,
  type WorkbenchVersions,
} from './facts.ts'
import './workbench.css'

const NAV_ICONS: Record<(typeof LIGHT_NAV)[number]['key'], ComponentType> = {
  workspace: HomeOutlined,
  guide: ReadOutlined,
  tasks: CodeOutlined,
  releases: HistoryOutlined,
  diagnostics: ThunderboltOutlined,
}

const METRIC_ICONS: Record<(typeof METRICS)[number]['key'], ComponentType> = {
  tasks: CodeOutlined,
  active: SyncOutlined,
  review: ReadOutlined,
  done: CheckCircleOutlined,
  blocked: ExclamationCircleOutlined,
}

export function WorkbenchPage({
  project,
  loading = false,
  username,
  running,
  metrics,
  trend,
  health,
  activity,
  versions,
}: {
  project: ProjectInfo | null
  loading?: boolean
  username?: string | null
  running?: RunningVersionFacts | null
  metrics?: WorkbenchMetrics | null
  trend?: WorkbenchTrendPoint[] | null
  health?: WorkbenchHealth | null
  activity?: WorkbenchActivity[] | null
  versions?: WorkbenchVersions | null
}) {
  const header = readHeader(project, running)
  const user = typeof username === 'string' && username.trim() ? username.trim() : '未记录'
  const progress = progressParts(metrics)
  const version = versionText(versions)
  const points = trend?.filter(point => point.label.trim()) ?? []
  const events = activity?.filter(item => item.text.trim()) ?? []
  const chart = chartPath(points)
  return <div className="v2-board" data-shell="unwired">
    <header className="v2-board-top">
      <div className="v2-board-brand"><strong>基座验收中心</strong><span>Web App Init Base Env</span></div>
      <div className="v2-board-version"><span>当前版本</span><strong>{header.version}</strong><span>SHA {header.sha}</span></div>
      <span className="v2-board-user">{user}</span>
    </header>
    <div className="v2-board-body">
      <nav className="v2-board-nav" aria-label="工作台预览导航">
        {LIGHT_NAV.map(item => {
          const Icon = NAV_ICONS[item.key]
          const current = item.key === 'workspace'
          return <div key={item.key} className={current ? 'is-current' : ''} aria-current={current ? 'page' : undefined}><Icon aria-hidden="true" /><span>{item.label}</span></div>
        })}
        <p>浅色导航预览，尚未接入现有壳。</p>
      </nav>
      <main className="v2-board-main">
        {loading && !project ? <div className="v2-board-loading" aria-busy="true">正在读取工作台</div> : <>
          <header className="v2-board-heading">
            <h1>工作台</h1>
            <p>系统运行总览、开发交付进度、版本状态与关键指标一目了然</p>
          </header>
          <section className="v2-board-metrics" aria-label="关键指标">
            {METRICS.map(item => {
              const Icon = METRIC_ICONS[item.key]
              return <article key={item.key} className={'tone-' + item.tone}><Icon aria-hidden="true" /><span>{item.label}</span><strong>{metricText(metrics, item.key)}</strong></article>
            })}
          </section>
          <div className="v2-board-mid">
            <section className="v2-board-card" aria-label="开发交付进度">
              <h2>开发交付进度</h2>
              <div className="v2-board-progress">
                <div className="v2-board-ring" style={progress ? { background: ringGradient(metrics) } : undefined}><b>{progress ? progress.percent + '%' : '未记录'}</b></div>
                <ul>
                  <li><i className="tone-success" />已完成 {metricText(metrics, 'done')}</li>
                  <li><i className="tone-brand" />进行中 {metricText(metrics, 'active')}</li>
                  <li><i className="tone-warning" />待验收 {metricText(metrics, 'review')}</li>
                  <li><i className="tone-danger" />阻塞 {metricText(metrics, 'blocked')}</li>
                </ul>
              </div>
            </section>
            <section className="v2-board-card" aria-label="最近7天任务趋势">
              <h2>最近7天任务趋势</h2>
              <div className="v2-board-legend"><span>新增任务</span><span>完成任务</span></div>
              {chart ? <svg viewBox="0 0 320 120" role="img" aria-label="任务趋势">{chart}</svg> : <p className="v2-board-empty">未记录</p>}
            </section>
          </div>
          <div className="v2-board-bottom">
            <section className="v2-board-card" aria-label="版本状态">
              <h2>版本状态</h2>
              <p><span>运行版本</span><strong>{version.running}</strong></p>
              <p><span>候选版本</span><strong>{version.candidate}</strong><em>{version.candidateState}</em></p>
              <p><span>上次发布</span><strong>{version.publishedAt}</strong></p>
            </section>
            <section className="v2-board-card" aria-label="系统健康">
              <h2>系统健康</h2>
              <ul className="v2-board-health">
                {(['github', 'macci', 'deploy', 'public'] as const).map(key => {
                  const row = healthText(health, key)
                  return <li key={key} className={'is-' + row.state}><span>{row.label}</span><strong>{row.text}</strong></li>
                })}
              </ul>
            </section>
            <section className="v2-board-card" aria-label="最近动态">
              <h2>最近动态</h2>
              {events.length ? <ul className="v2-board-activity">{events.map(item => <li key={item.at + item.text}><span>{item.at}</span><strong>{item.text}</strong></li>)}</ul> : <p className="v2-board-empty">未记录</p>}
            </section>
          </div>
        </>}
      </main>
    </div>
  </div>
}

function ringGradient(metrics: WorkbenchMetrics | null | undefined) {
  const done = metrics?.done ?? 0
  const active = metrics?.active ?? 0
  const review = metrics?.review ?? 0
  const blocked = metrics?.blocked ?? 0
  const total = done + active + review + blocked
  if (total <= 0) return undefined
  const stops = [
    ['#16a34a', done],
    ['#1677ff', active],
    ['#d97706', review],
    ['#dc2626', blocked],
  ] as const
  let cursor = 0
  const parts = stops.filter(([, value]) => value > 0).map(([color, value]) => {
    const start = cursor / total * 100
    cursor += value
    return `${color} ${start}% ${cursor / total * 100}%`
  })
  return `conic-gradient(${parts.join(',')})`
}

function chartPath(points: WorkbenchTrendPoint[]) {
  const usable = points.filter(point => typeof point.created === 'number' && typeof point.finished === 'number')
  if (usable.length < 2) return null
  const max = Math.max(1, ...usable.flatMap(point => [point.created ?? 0, point.finished ?? 0]))
  const line = (pick: (point: WorkbenchTrendPoint) => number) => usable.map((point, index) => {
    const x = usable.length === 1 ? 16 : 16 + index * (288 / (usable.length - 1))
    const y = 104 - ((pick(point) / max) * 80)
    return `${index === 0 ? 'M' : 'L'}${x.toFixed(1)} ${y.toFixed(1)}`
  }).join(' ')
  return <>
    <path d={line(point => point.created ?? 0)} className="is-created" />
    <path d={line(point => point.finished ?? 0)} className="is-finished" />
  </>
}
