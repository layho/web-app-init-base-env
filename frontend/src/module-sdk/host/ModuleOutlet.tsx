import { Component, lazy, Suspense, useMemo, type ReactNode, type ComponentType } from 'react'
import type { AcceptedModule, ModuleViewProps, RegistrationResult } from '../types.ts'
import { moduleMenu } from './open.ts'

class ModuleBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  render() {
    return this.state.failed ? <div role="alert">模块运行失败，其余页面不受影响</div> : this.props.children
  }
}

/** Render via React, never call a module component as an ordinary function. */
function ModulePage({ module }: { module: AcceptedModule }) {
  const View = useMemo(() => lazy(async () => {
    const loaded = await module.load()
    if (!loaded || typeof loaded.default !== 'function') throw new Error('Invalid module export')
    return { default: loaded.default as ComponentType<ModuleViewProps> }
  }), [module])
  return <ModuleBoundary><Suspense fallback={<div role="status">正在加载模块</div>}><View routeKey={module.routeKey} /></Suspense></ModuleBoundary>
}

/** Only statically registered, explicitly visible routes can be mounted. */
export function ModuleOutlet({ registration, routeKey, granted }: {
  registration: RegistrationResult
  routeKey: string
  granted: readonly string[]
}) {
  const visible = moduleMenu(registration, granted).some(item => item.routeKey === routeKey)
  const module = visible ? registration.modules.find(item => item.routeKey === routeKey) : undefined
  if (!module) return <div role="alert">模块不存在或不可访问</div>
  return <ModulePage key={module.id + ':' + module.routeKey} module={module} />
}
