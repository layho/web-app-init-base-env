"""Isolated V0.6.0 W05 automatic development delivery loop.

W01 events, W02 authentication, W04 scheduling, and W07 deployment are
consumed only through ports defined in this package. The W00 route module
``app.control_routes_w05`` is not mounted. This package does not enable
production, does not merge, and does not deploy.
"""

from app.control_delivery.flags import (
    AUTOMATIC_PUBLISH_AUTHORIZATION,
    BLOCKED_INTERFACES,
    FORGED_USER_ACCEPTANCE,
    GITHUB_EXECUTION,
    JOINT_INTEGRATION_READY,
    MACCI_PROOF,
    MODULE_NAME,
    MOUNTED_IN_APP_MAIN,
    PRODUCTION_DB_ACCESS,
    PRODUCTION_ENABLEMENT,
    PRODUCTION_VERIFICATION_ENABLED,
    REAL_DEPLOY,
    REAL_MERGE,
    ROUTE_MODULE,
    W01_HEAD,
    W02_HEAD,
    W04_HEAD,
    W07_HEAD,
)
from app.control_delivery.service import DeliveryService

__all__ = [
    "MODULE_NAME",
    "ROUTE_MODULE",
    "MOUNTED_IN_APP_MAIN",
    "PRODUCTION_ENABLEMENT",
    "PRODUCTION_VERIFICATION_ENABLED",
    "PRODUCTION_DB_ACCESS",
    "REAL_MERGE",
    "REAL_DEPLOY",
    "GITHUB_EXECUTION",
    "MACCI_PROOF",
    "JOINT_INTEGRATION_READY",
    "FORGED_USER_ACCEPTANCE",
    "AUTOMATIC_PUBLISH_AUTHORIZATION",
    "BLOCKED_INTERFACES",
    "W01_HEAD",
    "W02_HEAD",
    "W04_HEAD",
    "W07_HEAD",
    "DeliveryService",
]
