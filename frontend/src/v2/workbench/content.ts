export const MISSING = '未记录'

export const LIGHT_NAV = [
  { key: 'workspace', label: '工作台' },
  { key: 'guide', label: '使用指南' },
  { key: 'tasks', label: '开发任务' },
  { key: 'releases', label: '更新记录' },
  { key: 'diagnostics', label: '运行诊断' },
] as const

export const METRICS = [
  { key: 'tasks', label: '开发任务', tone: 'brand' },
  { key: 'active', label: '进行中', tone: 'success' },
  { key: 'review', label: '待验收', tone: 'warning' },
  { key: 'done', label: '已完成', tone: 'success' },
  { key: 'blocked', label: '阻塞', tone: 'danger' },
] as const

export const HEALTH_ROWS = [
  { key: 'github', label: 'GitHub 同步' },
  { key: 'macci', label: 'MacCI 队列' },
  { key: 'deploy', label: '部署服务' },
  { key: 'public', label: '公网访问' },
] as const

export type MetricKey = (typeof METRICS)[number]['key']
export type HealthKey = (typeof HEALTH_ROWS)[number]['key']
export type HealthState = 'ok' | 'warn' | 'error' | 'unknown'
