import type { ModuleInput, ModuleViewProps } from '../types.ts'

export function ExampleNotes({ routeKey }: ModuleViewProps) {
  return { routeKey, title: '示例笔记' }
}

/** Static example. The host calls this factory; the module does not edit navigation. */
export function exampleNotesModule(): ModuleInput {
  return {
    id: 'example-notes',
    routeKey: 'example-notes',
    title: '示例笔记',
    order: 100,
    icon: 'experiment',
    sdk: { min: '0.1.0', max: '0.1.0' },
    permission: 'modules.example-notes.read',
    load: async () => ({ default: ExampleNotes }),
  }
}
