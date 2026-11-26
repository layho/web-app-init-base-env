import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { createServer } from 'vite'

const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' })
after(() => server.close())
const sdk = await server.ssrLoadModule('/src/module-sdk/index.ts')
const host = await server.ssrLoadModule('/src/module-sdk/host/open.ts')

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

test('菜单只投影已接受模块，且不改注册结果', () => {
  const later = moduleInput({ id: 'zeta-page', routeKey: 'zeta-page', order: 30, title: '较后', permission: undefined })
  const earlier = moduleInput({ id: 'alpha-page', routeKey: 'alpha-page', order: 10, title: '较前' })
  const rejected = moduleInput({ id: 'workspace', routeKey: 'workspace-extra' })
  const nullPermission = moduleInput({ id: 'null-perm', routeKey: 'null-perm', permission: null })
  const result = sdk.registerModules([later.input, rejected.input, nullPermission.input, earlier.input])
  const before = JSON.stringify(result.modules.map(item => item.routeKey))
  const menu = host.moduleMenu(result)
  menu[0].title = '被改写'
  assert.deepEqual(menu.map(item => [item.routeKey, item.icon, item.permission]), [
    ['alpha-page', 'experiment', 'modules.alpha.read'],
    ['zeta-page', 'experiment', null],
  ])
  assert.equal(result.modules[0].title, '较前')
  assert.equal(JSON.stringify(result.modules.map(item => item.routeKey)), before)
  assert.deepEqual(result.rejected.map(item => item.code), ['reserved-key', 'invalid-permission'])
  for (const key of sdk.FOUNDATION_PAGE_KEYS) {
    assert.equal(menu.some(item => item.routeKey === key || item.id === key), false)
  }
})

test('未提供 granted 时声明权限仍可打开，示例模块返回视图值', async () => {
  const result = sdk.registerModules([sdk.exampleNotesModule()])
  const menu = host.moduleMenu(result)
  assert.deepEqual(menu.map(item => item.id), ['example-notes'])
  const opened = await host.openModule(result, 'example-notes')
  assert.equal(opened.ok, true)
  assert.deepEqual(opened.value, { routeKey: 'example-notes', title: '示例笔记' })
})

test('granted 只隐藏不匹配声明，空权限模块仍可见，且不能靠 routeKey 绕过', async () => {
  const declared = moduleInput({ id: 'declared-mod', routeKey: 'declared-mod', permission: 'modules.declared.read' })
  const open = moduleInput({ id: 'open-mod', routeKey: 'open-mod', permission: undefined, order: 5 })
  const result = sdk.registerModules([declared.input, open.input])
  const hidden = host.moduleMenu(result, [])
  assert.deepEqual(hidden.map(item => item.id), ['open-mod'])
  assert.equal(result.modules.length, 2)
  const denied = await host.openModule(result, 'declared-mod', ['modules.other.read'])
  assert.equal(denied.ok, false)
  assert.equal(denied.code, 'permission-hidden')
  assert.match(denied.detail, /不是后端鉴权/)
  assert.equal(host.OPEN_FAILURE_TEXT[denied.code], '当前声明的权限不包含该模块')
  assert.equal(declared.reads(), 0)
  const allowed = await host.openModule(result, 'open-mod', [])
  assert.equal(allowed.ok, true)
  assert.equal(allowed.value.routeKey, 'open-mod')
})

test('未知路由、被拒绝路由和非字符串不会加载任何模块', async () => {
  const good = moduleInput()
  const rejected = moduleInput({ id: 'future-mod', routeKey: 'future-mod', sdk: { min: '0.2.0', max: '0.2.0' } })
  const result = sdk.registerModules([good.input, rejected.input])
  const missing = await host.openModule(result, 'future-mod')
  const reserved = await host.openModule(result, 'tasks')
  const invalid = await host.openModule(result, null)
  assert.deepEqual([missing.code, reserved.code, invalid.code], ['unknown-route', 'unknown-route', 'unknown-route'])
  assert.equal(good.reads(), 0)
  assert.equal(host.OPEN_FAILURE_TEXT['unknown-route'], '没有可打开的模块页面')
})

test('单个模块加载或渲染失败时，相邻模块仍可打开', async () => {
  const brokenLoad = moduleInput({
    id: 'broken-load',
    routeKey: 'broken-load',
    order: 1,
    load: async () => { throw new Error('load exploded') },
  })
  const brokenRender = moduleInput({
    id: 'broken-render',
    routeKey: 'broken-render',
    order: 2,
    load: async () => ({ default: () => { throw new Error('render exploded') } }),
  })
  const healthy = moduleInput({ id: 'healthy-mod', routeKey: 'healthy-mod', order: 3 })
  const result = sdk.registerModules([brokenLoad.input, brokenRender.input, healthy.input])
  const loadFailure = await host.openModule(result, 'broken-load')
  const renderFailure = await host.openModule(result, 'broken-render')
  const ok = await host.openModule(result, 'healthy-mod')
  assert.deepEqual(loadFailure, { ok: false, code: 'load-failed', detail: 'load exploded' })
  assert.deepEqual(renderFailure, { ok: false, code: 'render-failed', detail: 'render exploded' })
  assert.equal(ok.ok, true)
  assert.equal(ok.value.routeKey, 'healthy-mod')
  assert.equal(host.OPEN_FAILURE_TEXT['load-failed'], '模块加载失败，其余模块不受影响')
  assert.equal(host.OPEN_FAILURE_TEXT['render-failed'], '模块渲染失败，其余模块不受影响')
})
