"""Isolated V0.6.0 W06 diagnostics package.

The W00 route module ``app.control_routes_w06`` is not mounted. Shared
routing and ``app.main`` stay untouched until that reservation exists.
This package does not own an authority store or an authentication store.
"""

from app.control_diagnostics.extract import external_report_evidence, summarize_failure
from app.control_diagnostics.ports import (
    BlockedAuthorizationPort,
    BlockedDeploymentPort,
    BlockedEventPort,
    BlockedSchedulePort,
    FixtureAuthorizationPort,
    FixtureDeploymentPort,
    FixtureEventPort,
    FixtureSchedulePort,
    Grant,
    ServerClock,
    SideEffectProbe,
)
from app.control_diagnostics.service import (
    DISPATCH_EXPANSION,
    MODULE_NAME,
    MOUNTED_IN_APP_MAIN,
    PRODUCTION_DB_ACCESS,
    PRODUCTION_ENABLEMENT,
    PRODUCTION_VERIFICATION_ENABLED,
    ROUTE_MODULE,
    SUBSCRIBE_PUSH,
    DiagnosticService,
    Limits,
)

__all__ = [
    "MODULE_NAME",
    "ROUTE_MODULE",
    "MOUNTED_IN_APP_MAIN",
    "PRODUCTION_ENABLEMENT",
    "PRODUCTION_VERIFICATION_ENABLED",
    "PRODUCTION_DB_ACCESS",
    "DISPATCH_EXPANSION",
    "SUBSCRIBE_PUSH",
    "DiagnosticService",
    "Limits",
    "Grant",
    "ServerClock",
    "SideEffectProbe",
    "BlockedAuthorizationPort",
    "BlockedEventPort",
    "BlockedSchedulePort",
    "BlockedDeploymentPort",
    "FixtureAuthorizationPort",
    "FixtureEventPort",
    "FixtureSchedulePort",
    "FixtureDeploymentPort",
    "summarize_failure",
    "external_report_evidence",
]
