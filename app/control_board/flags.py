"""Isolation flags for the W10 task board.

Production, mount, merge, deploy, and acceptance stay off. The route module
name is reserved and is not included from ``app.main``.
"""

MODULE_NAME = "app.control_board"
ROUTE_MODULE = "app.control_routes_w10"
MOUNTED_IN_APP_MAIN = False
PRODUCTION_ENABLEMENT = False
PRODUCTION_VERIFICATION_ENABLED = False
PRODUCTION_DB_ACCESS = False
REAL_MERGE = False
REAL_DEPLOY = False
GITHUB_EXECUTION = False
MACCI_PROOF = False
JOINT_INTEGRATION_READY = False
ACCEPTANCE_ENABLED = False
AUTHORITATIVE_MUTATION = False
SUBSCRIBE_PUSH = False

W01_HEAD = "33e65dc0925f03e60518a5c91a0e2254fd0fd12f"
W02_HEAD = "5dd0eb38e175a8a905836fbdbac5633ff9eb19ae"
W04_HEAD = "33c9d88595cf2d18e32ab78c22d5d8499131547e"
W05_HEAD = "29b3dbf227c561f530d17d21cc8f3751e29bd0af"
W06_HEAD = "2bc963e735072ec75751b106e4ed6fe929519c51"

PHASES = (
    "feedback_sync",
    "claim",
    "develop",
    "review",
    "test",
    "integrate",
    "preview_merge",
    "deploy",
    "accept",
    "publish",
    "close",
)
