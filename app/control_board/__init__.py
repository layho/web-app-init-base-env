"""Isolated V0.6.0 W10 unified task board.

W01 events, W02 authorization, W04 scheduling, W05 delivery, and W06
diagnostics are consumed only through ports. This package does not mount a
route, does not edit shared UI files, and does not enable production.
Test doubles are not GitHub, MacCI, merge, or deploy proof.
"""

from app.control_board.flags import (
    ACCEPTANCE_ENABLED,
    AUTHORITATIVE_MUTATION,
    GITHUB_EXECUTION,
    JOINT_INTEGRATION_READY,
    MACCI_PROOF,
    MODULE_NAME,
    MOUNTED_IN_APP_MAIN,
    PHASES,
    PRODUCTION_DB_ACCESS,
    PRODUCTION_ENABLEMENT,
    PRODUCTION_VERIFICATION_ENABLED,
    REAL_DEPLOY,
    REAL_MERGE,
    ROUTE_MODULE,
    SUBSCRIBE_PUSH,
    W01_HEAD,
    W02_HEAD,
    W04_HEAD,
    W05_HEAD,
    W06_HEAD,
)
from app.control_board.ports import (
    BlockedAuthorizationPort,
    BlockedDeliveryPort,
    BlockedDiagnosticPort,
    BlockedEventPort,
    BlockedSchedulePort,
    FixtureAuthorizationPort,
    FixtureDeliveryPort,
    FixtureDiagnosticPort,
    FixtureEventPort,
    FixtureSchedulePort,
    Grant,
)
from app.control_board.service import Board

__all__ = [
    "ACCEPTANCE_ENABLED",
    "AUTHORITATIVE_MUTATION",
    "GITHUB_EXECUTION",
    "JOINT_INTEGRATION_READY",
    "MACCI_PROOF",
    "MODULE_NAME",
    "MOUNTED_IN_APP_MAIN",
    "PHASES",
    "PRODUCTION_DB_ACCESS",
    "PRODUCTION_ENABLEMENT",
    "PRODUCTION_VERIFICATION_ENABLED",
    "REAL_DEPLOY",
    "REAL_MERGE",
    "ROUTE_MODULE",
    "SUBSCRIBE_PUSH",
    "W01_HEAD",
    "W02_HEAD",
    "W04_HEAD",
    "W05_HEAD",
    "W06_HEAD",
    "Board",
    "Grant",
    "BlockedAuthorizationPort",
    "BlockedDeliveryPort",
    "BlockedDiagnosticPort",
    "BlockedEventPort",
    "BlockedSchedulePort",
    "FixtureAuthorizationPort",
    "FixtureDeliveryPort",
    "FixtureDiagnosticPort",
    "FixtureEventPort",
    "FixtureSchedulePort",
]
