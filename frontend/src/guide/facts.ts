export const MISSING = '未记录'

/** Confirmed release order. Preview is deployed and accepted before main. */
export const RELEASE_ORDER = 'preview 部署 → 用户验收 → main 正式发布'

export const GUIDE_STEPS = [
  { title: '查看工作台', detail: '先看工作台里的真实运行版本、待处理、待验收和异常。没有这些数据时显示未记录。' },
  { title: '提交需求', detail: '需求、问题、优化建议和验收意见仍从现有意见反馈进入。本页不提供提交按钮。' },
  { title: '跟踪开发', detail: '在开发任务查看测试、合并和部署进度。本页不改开发任务看板。' },
  { title: '验收版本', detail: `发布顺序是${RELEASE_ORDER}。未验收的 preview 不得进入 main，也不能先合并 main 再验收。` },
  { title: '排查问题', detail: '到运行诊断查看真实异常和日志。没有诊断记录时显示未记录。' },
] as const

/** Descriptions of the five confirmed areas. They are not a module registry and do not navigate. */
export const GUIDE_MODULES = [
  { id: 'workspace', title: '工作台', detail: '集中查看运行版本和当前事项。计数只来自已有数据。' },
  { id: 'guide', title: '使用指南', detail: '说明快速开始和发布顺序，并保留原有操作指南与基座升级栏目。' },
  { id: 'tasks', title: '开发任务', detail: '跟踪开发、测试、合并和部署。提交仍走现有反馈，不在这里新增入口。' },
  { id: 'releases', title: '更新记录', detail: '查看版本、部署和验收证据。缺少证据时不能当成已验收。' },
  { id: 'diagnostics', title: '运行诊断', detail: '按真实异常排查。没有记录就显示未记录，不编造心跳或修复结果。' },
] as const

export const GUIDE_FAQ = [
  {
    key: 'order',
    question: '发布顺序是什么？',
    answer: `顺序固定为${RELEASE_ORDER}。不能先合并 main 再让用户验收。`,
  },
  {
    key: 'downstream',
    question: '下游应用从哪里升级？',
    answer: '正式基座来源是 main。preview 不是下游升级目标。具体版本和 SHA 在基座版本栏目，没有则显示未记录。',
  },
  {
    key: 'missing',
    question: '环境和公网地址为什么是未记录？',
    answer: '当前项目信息没有环境和公网地址字段。本页不从其他文字推测，也不编造地址。',
  },
  {
    key: 'submit',
    question: '怎样提交需求？',
    answer: '使用现有意见反馈。使用指南不新增提交、验收或导航按钮。',
  },
] as const

export type GuideSource = {
  name?: string | null
  summary?: string | null
  foundation_version?: {
    current?: { version?: string | null; sha?: string | null } | null
    checked_at?: string | null
  } | null
}

export type GuideFacts = {
  name: string
  summary: string
  version: string
  sha: string
  checkedAt: string
  environment: string
  publicAddress: string
}

function text(value: unknown): string {
  if (typeof value !== 'string') return MISSING
  const trimmed = value.trim()
  return trimmed ? trimmed : MISSING
}

export function readGuideFacts(source: GuideSource | null | undefined): GuideFacts {
  const current = source?.foundation_version?.current
  const version = text(current?.version)
  const checked = source?.foundation_version?.checked_at
  const parsed = typeof checked === 'string' ? Date.parse(checked) : Number.NaN
  return {
    name: text(source?.name),
    summary: text(source?.summary),
    version: version === MISSING ? MISSING : `v${version}`,
    sha: text(current?.sha),
    checkedAt: Number.isNaN(parsed) ? MISSING : new Date(parsed).toLocaleString('zh-CN', { hour12: false }),
    environment: MISSING,
    publicAddress: MISSING,
  }
}
