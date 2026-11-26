export const FOUNDATION_UI_SDK = '0.1.0'

/** Host-owned page keys. Modules cannot register these; the shell still owns them. */
export const FOUNDATION_PAGE_KEYS = [
  'workspace',
  'project',
  'tasks',
  'releases',
  'logs',
  'feedback',
] as const

/** Extra keys reserved for login, diagnostics and settings, which are not module pages. */
export const RESERVED_MODULE_KEYS = [
  ...FOUNDATION_PAGE_KEYS,
  'login',
  'diagnostics',
  'settings',
] as const

/** Closed icon vocabulary. Modules pass a name; they do not pass a component or URL. */
export const REGISTERED_ICONS = [
  'appstore',
  'project',
  'unordered-list',
  'history',
  'profile',
  'comment',
  'experiment',
] as const

export type FoundationPageKey = (typeof FOUNDATION_PAGE_KEYS)[number]
export type ReservedModuleKey = (typeof RESERVED_MODULE_KEYS)[number]
export type RegisteredIcon = (typeof REGISTERED_ICONS)[number]

export type ModuleViewProps = { routeKey: string }
export type ModuleView = (props: ModuleViewProps) => unknown
export type ModuleLoader = () => Promise<{ default: ModuleView }>

export type ModuleInput = {
  id: string
  routeKey: string
  title: string
  order: number
  icon: string
  sdk: { min: string; max: string }
  permission?: string
  load: ModuleLoader
}

export type AcceptedModule = {
  id: string
  routeKey: string
  title: string
  order: number
  icon: RegisteredIcon
  sdk: { min: string; max: string }
  permission: string | null
  load: ModuleLoader
}

export type RejectionCode =
  | 'duplicate-id'
  | 'duplicate-route'
  | 'reserved-key'
  | 'invalid-id'
  | 'invalid-route'
  | 'invalid-title'
  | 'invalid-order'
  | 'unknown-icon'
  | 'invalid-sdk'
  | 'incompatible-sdk'
  | 'unknown-field'
  | 'invalid-permission'
  | 'missing-load'
  | 'invalid-module'

export type ModuleRejection = {
  index: number
  id: string | null
  code: RejectionCode
  detail: string
}

export type RegistrationResult = {
  sdk: string
  modules: AcceptedModule[]
  rejected: ModuleRejection[]
}

export type LoadResult =
  | { ok: true; component: ModuleView }
  | { ok: false; code: 'load-failed'; detail: string }

export type RenderResult =
  | { ok: true; value: unknown }
  | { ok: false; code: 'render-failed'; detail: string }
