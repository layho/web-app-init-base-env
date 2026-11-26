export {
  FOUNDATION_PAGE_KEYS,
  FOUNDATION_UI_SDK,
  REGISTERED_ICONS,
  RESERVED_MODULE_KEYS,
} from './types.ts'
export type {
  AcceptedModule,
  FoundationPageKey,
  LoadResult,
  ModuleInput,
  ModuleLoader,
  ModuleRejection,
  ModuleView,
  RegisteredIcon,
  RegistrationResult,
  RejectionCode,
  RenderResult,
  ReservedModuleKey,
} from './types.ts'
export { loadModule, registerModules, renderModuleIsolated } from './registry.ts'
export { ExampleNotes, exampleNotesModule } from './examples/notes.ts'
