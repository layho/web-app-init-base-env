import { loadModule, renderModuleIsolated } from '../registry.ts'
import type { AcceptedModule, RegisteredIcon, RegistrationResult } from '../types.ts'

export type ModuleMenuEntry = {
  id: string
  routeKey: string
  title: string
  order: number
  icon: RegisteredIcon
  permission: string | null
}

export type OpenFailureCode = 'unknown-route' | 'permission-hidden' | 'load-failed' | 'render-failed'

export type OpenResult =
  | { ok: true; routeKey: string; value: unknown }
  | { ok: false; code: OpenFailureCode; detail: string }

/** Stable labels for a future shell. They do not add buttons or claim the menu is mounted. */
export const OPEN_FAILURE_TEXT: Record<OpenFailureCode, string> = {
  'unknown-route': '没有可打开的模块页面',
  'permission-hidden': '当前声明的权限不包含该模块',
  'load-failed': '模块加载失败，其余模块不受影响',
  'render-failed': '模块渲染失败，其余模块不受影响',
}

/**
 * Menu projection for modules already accepted by registerModules.
 * Omit granted to show every accepted module. Pass granted only as a caller-supplied
 * declaration filter: null permission stays visible, and hiding is not backend authorization.
 */
export function moduleMenu(result: RegistrationResult, granted?: readonly string[]): ModuleMenuEntry[] {
  return result.modules.filter(item => isVisible(item, granted)).map(entry)
}

export async function openModule(
  result: RegistrationResult,
  routeKey: string,
  granted?: readonly string[],
): Promise<OpenResult> {
  if (typeof routeKey !== 'string') {
    return { ok: false, code: 'unknown-route', detail: 'routeKey 必须是字符串' }
  }
  const accepted = result.modules.find(item => item.routeKey === routeKey)
  if (!accepted) {
    return { ok: false, code: 'unknown-route', detail: '没有已接受的模块使用该 routeKey' }
  }
  if (!isVisible(accepted, granted)) {
    return {
      ok: false,
      code: 'permission-hidden',
      detail: '调用方提供的权限声明不包含该模块。隐藏不是后端鉴权。',
    }
  }
  const loaded = await loadModule(accepted)
  if (!loaded.ok) return { ok: false, code: 'load-failed', detail: loaded.detail }
  const rendered = renderModuleIsolated(loaded.component, accepted.routeKey)
  if (!rendered.ok) return { ok: false, code: 'render-failed', detail: rendered.detail }
  return { ok: true, routeKey: accepted.routeKey, value: rendered.value }
}

function isVisible(item: AcceptedModule, granted?: readonly string[]): boolean {
  if (granted === undefined) return true
  if (item.permission === null) return true
  return granted.includes(item.permission)
}

function entry(item: AcceptedModule): ModuleMenuEntry {
  return {
    id: item.id,
    routeKey: item.routeKey,
    title: item.title,
    order: item.order,
    icon: item.icon,
    permission: item.permission,
  }
}
