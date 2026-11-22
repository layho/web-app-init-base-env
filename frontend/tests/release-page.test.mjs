import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' })
after(() => server.close())
const { ReleasePage } = await server.ssrLoadModule('/src/OperationsPage.tsx')

function renderGate(state, reason, releaseVersion = '0.1.18') {
  return renderToStaticMarkup(createElement(ReleasePage, {
    data: {
      version: '0.1.18', running_sha: 'a'.repeat(40), deployments: [],
      releases: [{ version: releaseVersion, title: '候选', acceptance_required: true }],
      acceptance: { enabled: true, ready: false, state, reason },
    }, loading: false,
  }))
}

for (const [reason, version] of [
  ['preview 已更新，当前页面仍是旧版', '0.1.18'],
  ['部署请求尚未完成', '0.1.18'],
  ['运行版本与最新版本记录不一致', '0.1.19'],
]) {
  test(reason, () => {
    const html = renderGate('not_requested', reason, version)
    assert.ok(html.includes('当前页面暂不可验收'))
    assert.ok(html.includes(reason))
    const buttons = html.match(/<button\b[^>]*>(?:(?!<\/button>)[\s\S])*?<\/button>/g) ?? []
    const accept = buttons.filter(button => button.includes('验收通过'))
    assert.ok(accept.length > 0)
    assert.ok(accept.every(button => /<button\b[^>]*\bdisabled(?:=""|\s|>)/.test(button)))
  })
}

test('已发布的旧提交同时显示新候选提示', () => {
  const html = renderGate('published', 'preview 已更新，当前页面仍是旧版')
  assert.ok(html.includes('验收通过 · 已发布 main'))
  assert.ok(html.includes('当前页面暂不可验收'))
  assert.ok(html.includes('preview 已更新，当前页面仍是旧版'))
})
