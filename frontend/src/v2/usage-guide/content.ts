export const MISSING = '未记录'

/** Confirmed in #152. The usage-guide drawing's last loop step says merge to main first; that order is not used. */
export const RELEASE_ORDER = 'preview 部署 → 用户验收 → main 正式发布'

export const LIGHT_NAV = [
  { key: 'workspace', label: '工作台' },
  { key: 'guide', label: '使用指南' },
  { key: 'tasks', label: '开发任务' },
  { key: 'releases', label: '更新记录' },
  { key: 'diagnostics', label: '运行诊断' },
] as const

export const GUIDE_STEPS = [
  { title: '查看工作台', detail: '了解当前运行状态和待处理事项。没有数据时显示未记录。' },
  { title: '提交需求', detail: '在开发任务中使用提交需求入口，保留原有反馈记录和同步机制。' },
  { title: '跟踪开发', detail: '在开发任务查看进度、测试结果和合并状态。' },
  { title: '验收版本', detail: '候选先部署到 preview，由用户验收。未验收不得进入 main。' },
  { title: '排查问题', detail: '在运行诊断查看真实异常。没有诊断记录时显示未记录。' },
] as const

export const GUIDE_MODULES = [
  { key: 'workspace', title: '工作台', detail: '运行总览与交付情况。计数只来自已有数据。' },
  { key: 'guide', title: '使用指南', detail: '系统说明、操作流程和发布顺序。当前页。' },
  { key: 'tasks', title: '开发任务', detail: '提交后的需求在这里跟踪测试、合并和部署。' },
  { key: 'releases', title: '更新记录', detail: '查看版本、部署证据和验收结果。缺少证据时不能验收。' },
  { key: 'diagnostics', title: '运行诊断', detail: '查看错误、阻塞和执行链路。不编造心跳或修复结果。' },
] as const

export const RELEASE_LOOP = [
  { title: '需求提交', detail: '提出需求或问题' },
  { title: '开发实现', detail: '创建分支并开发' },
  { title: '自动测试', detail: 'MacCI 执行测试' },
  { title: '代码审查', detail: '审查变更' },
  { title: 'preview 部署', detail: '候选部署到 preview' },
  { title: '用户验收', detail: '验收后才进入 main' },
] as const

export const UPGRADE_STEPS = [
  { title: '检查更新', detail: '使用现有顶栏的检查更新。本页不再放一个无动作按钮。' },
  { title: '发布候选版本', detail: '候选部署到 preview，不把 preview 当作下游正式来源。' },
  { title: '功能验收', detail: '在更新记录中对照证据验收。没有部署证据时不能验收。' },
  { title: '正式发布', detail: `顺序是${RELEASE_ORDER}。不能先合并 main 再验收。` },
] as const

export const GUIDE_FAQ = [
  {
    key: 'submit',
    question: '如何提交一个新的开发需求？',
    answer: '进入开发任务，点击提交需求。原有反馈记录与交付链路继续保留。',
  },
  {
    key: 'accept',
    question: '为什么这里没有「验收通过」按钮？',
    answer: '验收留在更新记录。未实际部署、SHA 不一致或缺少证据时，原流程会禁用验收并显示原因。',
  },
  {
    key: 'draft',
    question: 'PR 一直处于 Draft 状态怎么办？',
    answer: '以开发任务里的真实 PR 状态为准。本页不能把 Draft 改成可合并。',
  },
  {
    key: 'sha',
    question: '运行版本与最新版本不一致怎么办？',
    answer: '只显示已取得的版本和 SHA。没有记录时显示未记录，不用设计图里的示例值填上。',
  },
  {
    key: 'deploy',
    question: '如何查看部署是否成功？',
    answer: '查看更新记录中的部署证据和当前运行 SHA。没有回执时不能当成已部署。',
  },
] as const
