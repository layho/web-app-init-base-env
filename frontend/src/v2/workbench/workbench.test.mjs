import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' })
after(() => server.close())
const page = await server.ssrLoadModule('/src/v2/workbench/WorkbenchPage.tsx')
const facts = await server.ssrLoadModule('/src/v2/workbench/facts.ts')

const project = {
  name: '夹具项目', summary: '仅测试', architecture: [], workflow: [], tasks: [],
  foundation_version: {
    repository: 'fixture', role: 'foundation', current: { version: '0.0.0-fixture', sha: 'f'.repeat(40) },
    latest: null, checked_at: null, stale: false, status: 'current', auto_upgrade_enabled: false,
  },
}

function render(props) {
  return renderToStaticMarkup(createElement(page.WorkbenchPage, props))
}

test('空数据全部未记录，不出现设计图示例值', () => {
  const header = facts.readHeader({ name: ' ', summary: '', architecture: [], workflow: [], tasks: [] }, null)
  assert.equal(header.version, '未记录')
  assert.equal(header.sha, '未记录')
  assert.equal(facts.progressParts(null), null)
  const html = render({ project: null })
  for (const sample of ['V0.1.41', '1842244', 'base.layho.com', '78%', 'V0.1.42', '11-19']) {
    assert.equal(html.includes(sample), false, sample)
  }
  assert.ok(html.includes('未记录'))
  assert.equal(html.includes('is-created'), false)
})

test('浅色五项导航当前是工作台，且没有假操作按钮', () => {
  const html = render({
    project, username: 'fixture-user', running: { version: '0.0.0-fixture', running_sha: 'a'.repeat(40) },
    metrics: { tasks: 3, active: 1, review: 1, done: 2, blocked: 0 },
    trend: [{ label: '甲', created: 1, finished: 0 }, { label: '乙', created: 2, finished: 1 }],
    health: { github: { state: 'ok', detail: '正常' } },
    activity: [{ at: '夹具时间', text: '夹具动态' }],
    versions: { running: '0.0.0-fixture', candidate: '未记录', candidateState: '未记录', publishedAt: '未记录' },
  })
  const nav = html.match(/<nav[\s\S]*?<\/nav>/)?.[0] ?? ''
  for (const label of ['工作台', '使用指南', '开发任务', '更新记录', '运行诊断']) assert.ok(nav.includes(label), label)
  for (const absent of ['项目信息', '系统日志', '意见反馈', '系统设置']) assert.equal(nav.includes(absent), false, absent)
  assert.ok(nav.includes('aria-current="page"'))
  assert.ok(html.includes('data-shell="unwired"'))
  assert.ok(html.includes('系统运行总览、开发交付进度、版本状态与关键指标一目了然'))
  assert.ok(html.includes('GitHub 同步') && html.includes('MacCI 队列') && html.includes('部署服务') && html.includes('公网访问'))
  const buttons = html.match(/<button\b[^>]*>[\s\S]*?<\/button>/g) ?? []
  for (const label of ['检查更新', '立即部署', '提交需求', '验收通过', '新建任务']) {
    assert.equal(buttons.some(button => button.includes(label)), false, label)
  }
})

test('加载中不渲染指标正文', () => {
  const html = render({ project: null, loading: true })
  assert.ok(html.includes('正在读取工作台'))
  assert.equal(html.includes('开发交付进度'), false)
})


test('embedded workbench has no duplicate navigation or header', () => {
  const html = render({project, embedded: true})
  assert.ok(html.includes('data-shell="embedded"'))
  assert.equal(html.includes('<nav'), false)
  assert.equal(html.includes('v2-board-top'), false)
})

test('workbench uses the shared task classifier and excludes stale snapshots', async () => {
  const {workbenchMetrics} = await server.ssrLoadModule('/src/DevelopmentTasksPage.tsx')
  const snapshot = {synced_at: '2026-11-25T00:00:00Z', stale: false, tasks: [
    {status: '已完成'}, {status: '开发中'}, {status: '未记录开发状态'},
    {status: '受阻'}, {status: '已关闭'}, {status: '待验收', merge_state: 'merged', deployment_state: 'running'},
    {status: '开发中', test_state: 'pending'},
  ]}
  assert.deepEqual(workbenchMetrics(snapshot), {tasks: 6, active: 3, review: 1, done: 1, blocked: 1})
  assert.equal(workbenchMetrics({...snapshot, stale: true}), null)
  assert.equal(workbenchMetrics(null), null)
})
