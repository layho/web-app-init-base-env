# Backend module SDK v1 (#156)

Application-owned Python code lives in app_modules/. Explicitly register trusted Module objects in app_modules/registry.py. Core mounts their APIRouters under /api/modules/<id> before the frontend static mount, with the existing administrator session/CSRF dependency. No remote dynamic import is supported. IDs, SDK versions, route collisions and duplicate modules are checked before mounting. Use table_name(module_id, local_name) for a PostgreSQL-safe table namespace.

This is an in-process extension contract, not a sandbox or database permission boundary. Modules have the same process privileges as Core. No automatic business migration or production database write is performed by the SDK. Independently scoped database roles, migration checks, dependency locks and trusted ownership checks remain necessary. Invalid module registration fails startup rather than silently exposing an unvalidated route.

app-init seeds the entry point for new applications. Upgrade tooling must preserve the application-owned registry and module files; that protection must be verified before release. Tests use isolated app instances, not production data.
