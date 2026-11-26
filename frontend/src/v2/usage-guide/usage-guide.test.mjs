import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' })
after(() => server.close())
const page = await server.ssrLoadModule('/src/v2/usage-guide/UsageGuidePage.tsx')
const facts = await server.ssrLoadModule('/src/v2/usage-guide/facts.ts')

const project = {
  name: '夹具项目',
  summary: '仅测试摘要',
  architecture: [],
  workflow: [],
  tasks: [],
  foundation_version: {
    repository: 'fixture', role: 'foundation', current: { version: '0.1.40', sha: 'a'.repeat(40) },
    latest: null, checked_at: '2026-11-25T07:00:00Z', stale: false, status: 'current', auto_upgrade_enabled: false,
  },
}

function render(props) {
  return renderToStaticMarkup(createElement(page.UsageGuidePage, props))
}

test('空字段显示未记录，不用设计图示例值', () => {
  const value = facts.readGuideFacts({ name: ' ', summary: '', architecture: [], workflow: [], tasks: [] }, null)
  assert.equal(value.name, '未记录')
  assert.equal(value.version, '未记录')
  assert.equal(value.environment, '未记录')
  assert.equal(value.publicAddress, '未记录')
  const html = render({ project: { name: '', summary: '', architecture: [], workflow: [], tasks: [] } })
  assert.equal(html.includes('base.layho.com'), false)
  assert.equal(html.includes('1842244'), false)
  assert.ok(html.includes('未记录'))
})

test('使用指南整页使用浅色五项导航，并写明确认后的发布顺序', () => {
  const html = render({ project, username: 'admin', running: { version: '0.1.40', running_sha: 'b'.repeat(40) } })
  const nav = html.match(/<nav[\s\S]*?<\/nav>/)?.[0] ?? ''
  for (const label of ['工作台', '使用指南', '开发任务', '更新记录', '运行诊断']) assert.ok(nav.includes(label))
  for (const absent of ['项目信息', '系统日志', '意见反馈']) assert.equal(nav.includes(absent), false)
  for (const step of ['查看工作台', '提交需求', '跟踪开发', '验收版本', '排查问题']) assert.ok(html.includes(step))
  assert.ok(html.includes('preview 部署 → 用户验收 → main 正式发布'))
  assert.ok(html.includes('不能先合并 main 再验收'))
  assert.ok(html.includes('data-shell="unwired"'))
  const buttons = html.match(/<button\b[^>]*>[\s\S]*?<\/button>/g) ?? []
  for (const label of ['提交需求', '验收通过', '立即部署', '检查更新', '新建任务']) {
    assert.equal(buttons.some(button => button.includes(label)), false, label)
  }
})

test('加载中不渲染指南正文', () => {
  const html = render({ project: null, loading: true })
  assert.ok(html.includes('正在读取项目信息'))
  assert.equal(html.includes('快速开始'), false)
})
