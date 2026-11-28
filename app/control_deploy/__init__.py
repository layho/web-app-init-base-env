"""Isolated V0.6.0 W07 deploy transaction and recovery.

This package is not a deploy entry. It does not import or call
``deploy/macmini/manage.sh``, does not switch a host service, and does not
open a database. Production enablement stays off. W01 events, W02 leases,
and W03 artifacts are consumed only through the ports in ``ports.py``.
"""

PRODUCTION_ENABLEMENT = False
REAL_SERVICE_SWITCH = False
MANAGES_REAL_DEPLOY_ENTRY = False

from app.control_deploy.engine import DeployEngine, DeployResult, Interrupted

__all__ = [
    "PRODUCTION_ENABLEMENT",
    "REAL_SERVICE_SWITCH",
    "MANAGES_REAL_DEPLOY_ENTRY",
    "DeployEngine",
    "DeployResult",
    "Interrupted",
]
