import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { createServer } from 'vite'

const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' })
after(() => server.close())
const sdk = await server.ssrLoadModule('/src/module-sdk/index.ts')

function moduleInput(overrides = {}) {
  let loaded = 0
  const input = {
    id: 'alpha-notes',
    routeKey: 'alpha-notes',
    title: '笔记',
    order: 20,
    icon: 'experiment',
    sdk: { min: '0.1.0', max: '0.1.0' },
    permission: 'modules.alpha.read',
    load: async () => {
      loaded += 1
      return { default: ({ routeKey }) => ({ routeKey, loaded }) }
    },
    ...overrides,
  }
  return { input, reads: () => loaded }
}

test('合法模块按 order 再按 id 排序，注册阶段不调用 load', () => {
  const later = moduleInput({ id: 'zeta-page', routeKey: 'zeta-page', order: 10, title: ' 较后 ' })
  const earlier = moduleInput({ id: 'alpha-page', routeKey: 'alpha-page', order: 10, title: '较前' })
  const result = sdk.registerModules([later.input, earlier.input])
  assert.deepEqual(result.rejected, [])
  assert.deepEqual(result.modules.map(item => item.id), ['alpha-page', 'zeta-page'])
  assert.equal(result.modules[0].title, '较前')
  assert.equal(result.modules[1].title, '较后')
  assert.equal(result.sdk, sdk.FOUNDATION_UI_SDK)
  assert.equal(later.reads(), 0)
  assert.equal(earlier.reads(), 0)
})

test('重复 id、重复 route、保留名、非法图标、未知字段和版本不兼容只拒绝该模块', () => {
  const good = moduleInput()
  const duplicateId = moduleInput({ routeKey: 'other-route' })
  const duplicateRoute = moduleInput({ id: 'other-id', routeKey: 'alpha-notes' })
  const reserved = moduleInput({ id: 'workspace', routeKey: 'workspace-extra' })
  const badIcon = moduleInput({ id: 'icon-bad', routeKey: 'icon-bad', icon: 'https://example.invalid/icon' })
  const unknown = moduleInput({ id: 'unknown-field', routeKey: 'unknown-field', extra: true })
  const incompatible = moduleInput({ id: 'future-mod', routeKey: 'future-mod', sdk: { min: '0.2.0', max: '0.2.0' } })
  const result = sdk.registerModules([
    good.input, duplicateId.input, duplicateRoute.input, reserved.input, badIcon.input, unknown.input, incompatible.input,
  ])
  assert.deepEqual(result.modules.map(item => item.id), ['alpha-notes'])
  assert.deepEqual(result.rejected.map(item => item.code), [
    'duplicate-id', 'duplicate-route', 'reserved-key', 'unknown-icon', 'unknown-field', 'incompatible-sdk',
  ])
  for (const key of sdk.FOUNDATION_PAGE_KEYS) {
    assert.equal(result.modules.some(item => item.id === key || item.routeKey === key), false)
  }
})

test('加载失败和渲染抛错被隔离，其他已接受模块仍可加载', async () => {
  const healthy = moduleInput({ id: 'healthy-mod', routeKey: 'healthy-mod' })
  const brokenLoad = moduleInput({
    id: 'broken-load',
    routeKey: 'broken-load',
    load: async () => { throw new Error('load exploded') },
  })
  const brokenRender = moduleInput({
    id: 'broken-render',
    routeKey: 'broken-render',
    load: async () => ({ default: () => { throw new Error('render exploded') } }),
  })
  const result = sdk.registerModules([brokenLoad.input, healthy.input, brokenRender.input])
  assert.equal(result.rejected.length, 0)
  const loaded = []
  for (const item of result.modules) {
    const outcome = await sdk.loadModule(item)
    loaded.push([item.id, outcome.ok, outcome.ok ? null : outcome.detail])
    if (item.id === 'broken-render') {
      assert.equal(outcome.ok, true)
      const rendered = sdk.renderModuleIsolated(outcome.component, item.routeKey)
      assert.deepEqual(rendered, { ok: false, code: 'render-failed', detail: 'render exploded' })
    }
    if (item.id === 'healthy-mod') {
      assert.equal(outcome.ok, true)
      const rendered = sdk.renderModuleIsolated(outcome.component, item.routeKey)
      assert.equal(rendered.ok, true)
      assert.equal(rendered.value.routeKey, 'healthy-mod')
    }
  }
  assert.deepEqual(loaded, [
    ['broken-load', false, 'load exploded'],
    ['broken-render', true, null],
    ['healthy-mod', true, null],
  ])
})

test('示例模块可注册且 permission 只是声明', () => {
  const result = sdk.registerModules([sdk.exampleNotesModule()])
  assert.equal(result.rejected.length, 0)
  assert.equal(result.modules[0].id, 'example-notes')
  assert.equal(result.modules[0].permission, 'modules.example-notes.read')
  assert.equal(result.modules[0].icon, 'experiment')
})

test('空 permission、非法版本区间和缺失 load 被拒绝', () => {
  const emptyPermission = moduleInput({ id: 'empty-perm', routeKey: 'empty-perm', permission: '' })
  const badRange = moduleInput({ id: 'bad-range', routeKey: 'bad-range', sdk: { min: '0.2.0', max: '0.1.0' } })
  const missingLoad = moduleInput({ id: 'no-load', routeKey: 'no-load', load: undefined })
  const result = sdk.registerModules([emptyPermission.input, badRange.input, missingLoad.input])
  assert.deepEqual(result.modules, [])
  assert.deepEqual(result.rejected.map(item => item.code), ['invalid-permission', 'invalid-sdk', 'missing-load'])
})
