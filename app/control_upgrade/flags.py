"""Isolation flags for W08. Every enablement flag stays false."""

MODULE_NAME = "app.control_upgrade"
ROUTE_MODULE = "app.control_routes_w08"
MOUNTED_IN_APP_MAIN = False
PRODUCTION_ENABLEMENT = False
PRODUCTION_VERIFICATION_ENABLED = False
PRODUCTION_DB_ACCESS = False
REAL_MERGE = False
REAL_DEPLOY = False
REAL_UPGRADE = False
ACCEPTANCE_ENABLED = False
UPGRADE_APPLY_ENABLED = False
GITHUB_EXECUTION = False
MACCI_PROOF = False
JOINT_INTEGRATION_READY = False
AUTO_RESOLVE_AMBIGUOUS = False
OVERWRITE_APP_DATA = False
OVERWRITE_APP_CONFIG = False
OVERWRITE_CUSTOM_CODE = False

CAPABILITIES = ("U01", "U03", "U04", "U05", "U06", "U07", "U08")

W03_HEAD = "1bc3f83c67cee103365a3dcfaed9e31db4793422"
W04_HEAD = "33c9d88595cf2d18e32ab78c22d5d8499131547e"
W05_HEAD = "29b3dbf227c561f530d17d21cc8f3751e29bd0af"
W06_HEAD = "2bc963e735072ec75751b106e4ed6fe929519c51"
W07_HEAD = "8f1ad70fd331ae18d8ea1a82d3c1b3be84f1feda"

BLOCKED_INTERFACES = (
    "deploy.macci.isolated_build",
    "app.control_scheduler",
    "app.control_delivery",
    "app.control_diagnostics",
    "app.control_deploy",
    "app.control_routes_w08",
    "app.main",
    "deploy.app-init.app_init",
    "deploy.app-init.upgrade_plan",
    "deploy.app-init.upgrade_scheduler",
    "deploy.app-init.upgrade_auto_merge",
    "deploy.app-init.upgrade_runtime_auto",
    "deploy.app-init.upgrade_pr",
    "app.foundation_version",
    "app.module_sdk",
)

FOUNDATION_REPOSITORY = "layho/web-app-init-base-env"
CACHE_TTL_SECONDS = 300
