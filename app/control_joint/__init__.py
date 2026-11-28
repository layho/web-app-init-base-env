"""Joint V0.6.0 adapters. Production enablement stays off.

This package binds the isolated W01–W10 modules on one tree. It does not
install services, open a production database, or mint a MacCI receipt.
"""

from app.control_joint.capability import host_capability

PRODUCTION_ENABLED = False
PREVIEW_MERGE_AUTHORIZED = False
MAIN_RELEASE_AUTHORIZED = False

__all__ = [
    "MAIN_RELEASE_AUTHORIZED",
    "PREVIEW_MERGE_AUTHORIZED",
    "PRODUCTION_ENABLED",
    "host_capability",
]
