# Backend module SDK v1 (#156)

Application-owned Python code lives in app_modules/. Explicitly register trusted Module objects in app_modules/registry.py. Core mounts their APIRouters under /api/modules/<id> before the frontend static mount, with the existing administrator session/CSRF dependency. No remote dynamic import is supported. IDs, SDK versions, route collisions and duplicate modules are checked before mounting. Use table_name(module_id, local_name) for a PostgreSQL-safe table namespace.

This is an in-process extension contract, not a sandbox or database permission boundary. Modules have the same process privileges as Core. No automatic business migration or production database write is performed by the SDK. Independently scoped database roles, migration checks, dependency locks and trusted ownership checks remain necessary. Invalid module registration fails startup rather than silently exposing an unvalidated route.

app-init seeds the entry point for new applications. Upgrade tooling must preserve the application-owned registry and module files; that protection must be verified before release. Tests use isolated app instances, not production data.

## Upgrade ownership
Application-owned paths app_modules/, frontend/src/app-modules/ and migrations/modules/ are excluded from routine template merges. A first upgrade may add only the two exact missing Python package seed files; existing regular files are retained. Symlinks, parent/file collisions and case aliases fail closed. The planner, inventory and commit creation enforce the no-overwrite rule. This is an upgrade ownership guard, not independent OS or database isolation; deployment and auto-promotion still require the existing trust gates.


## Database and dependency contract
Module(...) may declare exact installed distribution pins via dependencies=(("package","1.2.3"),). Registration fails closed for missing/mismatched or duplicate pins; the SDK never installs packages. For database routes use Depends(module_db("notes")); configure APP_MODULE_NOTES_DATABASE_URL with an explicit PostgreSQL role distinct from Core. There is no Core fallback. Provision a non-superuser, non-owner role with access only to the module schema/tables, revoke public grants and verify ACLs independently before production. Restart the process after credential rotation. The SDK is not an OS sandbox; shared interpreter dependencies cannot support incompatible package versions, and database URL checks do not prove ACL isolation. No production database or role is created by this change.
