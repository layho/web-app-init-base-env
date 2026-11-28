"""Isolated V0.6.0 W08 Core/APP upgrade checks.

The package plans, diagnoses, and displays version state. It does not mount
``app.control_routes_w08``, does not edit shared entry points, and does not
perform an upgrade, merge, deploy, enrollment, or production change.

W03 through W07 are consumed only through the ports in ``ports.py``. Fixture
results are synthetic and are not runtime proof. The cited neighbor commits
are not in this tree.
"""

from app.control_upgrade.candidate import candidate_id
from app.control_upgrade.flags import CAPABILITIES
from app.control_upgrade.ports import Grant, ServerClock
from app.control_upgrade.service import UpgradeService

__all__ = [
    "CAPABILITIES",
    "Grant",
    "ServerClock",
    "UpgradeService",
    "candidate_id",
]
