"""Isolation flags. Production enablement stays off in this package."""

MODULE_NAME = "app.control_delivery"
ROUTE_MODULE = "app.control_routes_w05"
MOUNTED_IN_APP_MAIN = False
PRODUCTION_ENABLEMENT = False
PRODUCTION_VERIFICATION_ENABLED = False
PRODUCTION_DB_ACCESS = False
REAL_MERGE = False
REAL_DEPLOY = False
GITHUB_EXECUTION = False
MACCI_PROOF = False
JOINT_INTEGRATION_READY = False
FORGED_USER_ACCEPTANCE = False
AUTOMATIC_PUBLISH_AUTHORIZATION = False

W01_HEAD = "33e65dc0925f03e60518a5c91a0e2254fd0fd12f"
W02_HEAD = "5dd0eb38e175a8a905836fbdbac5633ff9eb19ae"
W04_HEAD = "33c9d88595cf2d18e32ab78c22d5d8499131547e"
W07_HEAD = "8f1ad70fd331ae18d8ea1a82d3c1b3be84f1feda"

BLOCKED_INTERFACES = (
    "app.control_events",
    "app.control_identity",
    "app.control_authz",
    "app.control_scheduler",
    "app.control_deploy",
    "app.control_routes_w05",
)
