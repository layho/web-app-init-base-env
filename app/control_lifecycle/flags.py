"""Enablement flags for the isolated W09 lifecycle package.

Every production, mount, install, uninstall, privilege, merge, deploy,
GitHub Actions, MacCI, acceptance, and overwrite flag is false. This module
does not authorize a host command.
"""

MODULE_NAME = "app.control_lifecycle"
ROUTE_MODULE = "app.control_routes_w09"
MOUNTED_IN_APP_MAIN = False
PRODUCTION_ENABLEMENT = False
PRODUCTION_VERIFICATION_ENABLED = False
PRODUCTION_DB_ACCESS = False
REAL_INSTALL = False
REAL_UNINSTALL = False
PRIVILEGE_ESCALATION = False
REAL_MERGE = False
REAL_DEPLOY = False
GITHUB_ACTIONS = False
MACCI_PROOF = False
ACCEPTANCE_ENABLED = False
OVERWRITE_USER_DATA = False
HOST_MUTATION = False
DEPLOY_CONFIG_MUTATION = False
JOINT_INTEGRATION_READY = False

CAPABILITIES = ("I01", "I02", "I03", "I04", "I05", "I06", "I08", "I09", "I10", "I11")

# Cited neighbor commit. That tree is not imported and is not in this branch.
W06_HEAD = "2bc963e735072ec75751b106e4ed6fe929519c51"
W06_FD_SOURCE = "app/control_diagnostics/safe_io.py"
W06_IN_TREE = False

RESERVED_SHARED = (
    "app/main.py",
    "app/control_routes_w09.py",
    "deploy/app-init/bootstrap.sh",
    "deploy/app-init/app_init.py",
    "deploy/app-init/upgrade_plan.py",
    "deploy/app-init/upgrade_pr.py",
    "deploy/app-init/upgrade_scheduler.py",
    "deploy/app-init/upgrade_auto_merge.py",
    "deploy/app-init/upgrade_runtime_auto.py",
    "app/module_sdk.py",
    "app/foundation_version.py",
    "app/auth.py",
    "app/operations.py",
)
