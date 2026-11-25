import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { after, test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' })
after(() => server.close())
const guide = await server.ssrLoadModule('/src/guide/facts.ts')
const overview = await server.ssrLoadModule('/src/guide/GuideOverview.tsx')
const page = await server.ssrLoadModule('/src/ProjectInfoPage.tsx')

const filled = {
  name: 'Web App Foundation',
  summary: '应用基座已初始化。',
  foundation_version: {
    current: { version: '0.1.40', sha: 'a'.repeat(40) },
    checked_at: '2026-11-25T07:00:00Z',
  },
  architecture: [{ layer: '浏览器', technology: 'React', role: '界面' }],
  workflow: [{ title: '登记需求', description: '在意见反馈提交。' }],
  operating_guide: {
    scope: 'foundation', title: '原有操作指南', summary: '保留原入口',
    cautions: ['不要跳过验收'], sections: [{ id: 'ops', title: '日常操作', summary: '原栏目', steps: [{ title: '打开页面', detail: '使用现有菜单' }] }],
  },
  development_loop: [{ id: 'feedback', title: '网页反馈转 GitHub Issue', actor: '用户', cadence: '按需', description: '保留闭环', mode: 'input' }],
  tasks: [{ module: '示例模块', scope: '范围', acceptance: '可验收', issue: 152 }],
}

function renderPage(data, loading = false) {
  return renderToStaticMarkup(createElement(page.ProjectInfoPage, {
    data, loading, issueBase: 'https://github.com/layho/web-app-init-base-env/issues',
    onOpenTasks: () => {}, onRefresh: () => {},
  }))
}

test('缺失字段显示未记录，环境和公网地址不从其他文字推测', () => {
  const facts = guide.readGuideFacts({ name: '  ', summary: null, foundation_version: { current: null, checked_at: '不是时间' } })
  assert.equal(facts.name, '未记录')
  assert.equal(facts.summary, '未记录')
  assert.equal(facts.version, '未记录')
  assert.equal(facts.sha, '未记录')
  assert.equal(facts.checkedAt, '未记录')
  assert.equal(facts.environment, '未记录')
  assert.equal(facts.publicAddress, '未记录')
  assert.equal(guide.readGuideFacts(null).name, '未记录')
})

test('已有版本和 SHA 原样进入摘要', () => {
  const facts = guide.readGuideFacts(filled)
  assert.equal(facts.version, 'v0.1.40')
  assert.equal(facts.sha, 'a'.repeat(40))
  assert.equal(facts.name, 'Web App Foundation')
  assert.notEqual(facts.checkedAt, '未记录')
})

test('五步、五模块、升级顺序和可折叠问题都在，且没有提交按钮', () => {
  const html = renderToStaticMarkup(createElement(overview.GuideOverview, { source: filled }))
  for (const title of ['查看工作台', '提交需求', '跟踪开发', '验收版本', '排查问题']) {
    assert.ok(html.includes(title), title)
  }
  for (const title of ['工作台', '使用指南', '开发任务', '更新记录', '运行诊断']) {
    assert.ok(html.includes(title), title)
  }
  const order = html.indexOf('preview 部署')
  const accept = html.indexOf('用户验收')
  const main = html.indexOf('main 正式发布')
  assert.ok(order >= 0 && order < accept && accept < main)
  assert.ok(html.includes('不能先合并 main 再验收'))
  assert.ok(html.includes('preview 不是下游升级目标'))
  for (const item of guide.GUIDE_FAQ) assert.ok(html.includes(item.question))
  const buttons = html.match(/<button\b[^>]*>[\s\S]*?<\/button>/g) ?? []
  assert.equal(buttons.some(button => button.includes('提交需求')), false)
  assert.equal(buttons.some(button => button.includes('验收通过')), false)
  assert.ok(html.includes('不是新的业务模块'))
})

test('空数据页面仍显示未记录，加载中不渲染指南正文', () => {
  const empty = renderPage({
    name: '', summary: '', architecture: [], workflow: [], tasks: [],
  })
  assert.ok(empty.includes('未记录'))
  assert.ok(empty.includes('快速开始'))
  const loading = renderPage(null, true)
  assert.equal(loading.includes('快速开始'), false)
  assert.equal(loading.includes('提交需求'), false)
  const absent = renderPage(null, false)
  assert.equal(absent, '')
})

test('原操作指南、开发闭环和基座版本栏目仍然保留', () => {
  const html = renderPage(filled)
  const tabs = [...html.matchAll(/role="tab"[^>]*>([^<]+)</g)].map(match => match[1])
  assert.deepEqual(tabs, ['架构设计', '开发流程', '操作指南', '开发闭环', '基座版本', '任务拆解'])
  assert.ok(html.includes('data-node-key="operating-guide"'))
  assert.ok(html.includes('data-node-key="foundation-version"'))
  const source = readFileSync(new URL('../ProjectInfoPage.tsx', import.meta.url), 'utf8')
  assert.ok(source.includes('刷新版本状态'))
  assert.ok(source.includes('查看开发进度'))
  assert.ok(source.includes('OperatingGuidePage'))
  assert.ok(source.includes('FoundationVersionPanel'))
  assert.ok(html.includes('快速开始'))
  const longName = '很长的项目名称'.repeat(12)
  const longHtml = renderPage({ ...filled, name: longName })
  assert.ok(longHtml.includes(longName))
})
