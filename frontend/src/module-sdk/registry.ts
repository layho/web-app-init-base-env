import {
  FOUNDATION_UI_SDK,
  REGISTERED_ICONS,
  RESERVED_MODULE_KEYS,
  type AcceptedModule,
  type LoadResult,
  type ModuleInput,
  type ModuleRejection,
  type ModuleView,
  type RegisteredIcon,
  type RegistrationResult,
  type RejectionCode,
  type RenderResult,
} from './types.ts'

const MODULE_KEY = /^[a-z][a-z0-9-]{1,63}$/
const SDK_VERSION = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/
const PERMISSION_KEY = /^[a-z][a-z0-9:.-]{0,63}$/
const KNOWN_FIELDS = new Set(['id', 'routeKey', 'title', 'order', 'icon', 'sdk', 'permission', 'load'])
const RESERVED = new Set<string>(RESERVED_MODULE_KEYS)
const ICONS = new Set<string>(REGISTERED_ICONS)

function rejection(code: RejectionCode, detail: string): Pick<ModuleRejection, 'code' | 'detail'> {
  return { code, detail }
}

function compareVersion(left: string, right: string): number {
  const a = left.split('.').map(Number)
  const b = right.split('.').map(Number)
  for (let index = 0; index < 3; index += 1) {
    if (a[index] !== b[index]) return a[index] - b[index]
  }
  return 0
}

function readId(value: unknown): string | null {
  if (!value || typeof value !== 'object') return null
  const id = (value as { id?: unknown }).id
  return typeof id === 'string' ? id : null
}

function validate(input: unknown, hostSdk: string, seenIds: ReadonlySet<string>, seenRoutes: ReadonlySet<string>) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) {
    return rejection('invalid-module', '模块描述必须是对象')
  }
  const record = input as Record<string, unknown>
  const unknownField = Object.keys(record).find(key => !KNOWN_FIELDS.has(key))
  if (unknownField) return rejection('unknown-field', `未知字段 ${unknownField}`)
  if (!SDK_VERSION.test(hostSdk)) return rejection('invalid-sdk', '宿主 SDK 版本不合法')

  const id = record.id
  if (typeof id !== 'string' || !MODULE_KEY.test(id)) return rejection('invalid-id', 'id 必须是小写字母开头、2 到 64 位的模块标识')
  const routeKey = record.routeKey
  if (typeof routeKey !== 'string' || !MODULE_KEY.test(routeKey)) {
    return rejection('invalid-route', 'routeKey 必须是小写字母开头、2 到 64 位的页面键')
  }
  if (RESERVED.has(id) || RESERVED.has(routeKey)) {
    return rejection('reserved-key', '该 id 或 routeKey 属于基座保留名')
  }

  const title = record.title
  if (typeof title !== 'string' || title.trim().length === 0 || title.trim().length > 40) {
    return rejection('invalid-title', 'title 去空白后必须为 1 到 40 个字符')
  }
  const order = record.order
  if (typeof order !== 'number' || !Number.isSafeInteger(order)) {
    return rejection('invalid-order', 'order 必须是安全整数')
  }
  const icon = record.icon
  if (typeof icon !== 'string' || !ICONS.has(icon)) return rejection('unknown-icon', 'icon 必须是宿主登记过的图标名')

  const sdk = record.sdk
  if (!sdk || typeof sdk !== 'object' || Array.isArray(sdk)) return rejection('invalid-sdk', 'sdk 必须是 { min, max }')
  const range = sdk as Record<string, unknown>
  if (Object.keys(range).some(key => key !== 'min' && key !== 'max')) {
    return rejection('unknown-field', 'sdk 含未知字段')
  }
  const min = range.min
  const max = range.max
  if (typeof min !== 'string' || typeof max !== 'string' || !SDK_VERSION.test(min) || !SDK_VERSION.test(max) || compareVersion(min, max) > 0) {
    return rejection('invalid-sdk', 'sdk.min 与 sdk.max 必须是合法版本且 min 不大于 max')
  }
  if (compareVersion(hostSdk, min) < 0 || compareVersion(hostSdk, max) > 0) {
    return rejection('incompatible-sdk', `模块要求 ${min} 到 ${max}，宿主是 ${hostSdk}`)
  }

  if ('permission' in record && record.permission !== undefined) {
    const permission = record.permission
    if (typeof permission !== 'string' || !PERMISSION_KEY.test(permission)) {
      return rejection('invalid-permission', 'permission 只能是声明用的短标识，不能为空或任意文本')
    }
  }
  if (typeof record.load !== 'function') return rejection('missing-load', 'load 必须是返回 { default } 的函数')
  if (seenIds.has(id)) return rejection('duplicate-id', `id ${id} 已注册`)
  if (seenRoutes.has(routeKey)) return rejection('duplicate-route', `routeKey ${routeKey} 已注册`)
  return null
}

export function registerModules(inputs: readonly unknown[], hostSdk = FOUNDATION_UI_SDK): RegistrationResult {
  const rejected: ModuleRejection[] = []
  const accepted: AcceptedModule[] = []
  const seenIds = new Set<string>()
  const seenRoutes = new Set<string>()

  inputs.forEach((input, index) => {
    const problem = validate(input, hostSdk, seenIds, seenRoutes)
    if (problem) {
      rejected.push({ index, id: readId(input), ...problem })
      return
    }
    const moduleInput = input as ModuleInput
    seenIds.add(moduleInput.id)
    seenRoutes.add(moduleInput.routeKey)
    const icon = moduleInput.icon as RegisteredIcon
    accepted.push({
      id: moduleInput.id,
      routeKey: moduleInput.routeKey,
      title: moduleInput.title.trim(),
      order: moduleInput.order,
      icon,
      sdk: { min: moduleInput.sdk.min, max: moduleInput.sdk.max },
      permission: moduleInput.permission ?? null,
      load: moduleInput.load,
    })
  })

  accepted.sort((left, right) => left.order - right.order || (left.id < right.id ? -1 : left.id > right.id ? 1 : 0))
  return { sdk: hostSdk, modules: accepted, rejected }
}

function errorDetail(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback
}

export async function loadModule(module: AcceptedModule): Promise<LoadResult> {
  try {
    const loaded = await module.load()
    if (!loaded || typeof loaded.default !== 'function') {
      return { ok: false, code: 'load-failed', detail: '模块未导出 default 函数' }
    }
    return { ok: true, component: loaded.default }
  } catch (error) {
    return { ok: false, code: 'load-failed', detail: errorDetail(error, '模块加载失败') }
  }
}

export function renderModuleIsolated(component: ModuleView, routeKey: string): RenderResult {
  try {
    return { ok: true, value: component({ routeKey }) }
  } catch (error) {
    return { ok: false, code: 'render-failed', detail: errorDetail(error, '模块渲染失败') }
  }
}
