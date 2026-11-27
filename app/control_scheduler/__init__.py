"""Isolated V0.6.0 task, candidate, and version scheduler.

This package is preparation only. It does not mount a route, does not edit
the integration queue, preview journal, merge path, runner, operations, or
manage.sh, and does not enable production. Authentication, event storage, and
lease fencing stay behind the W02 and W01 ports. Tests supply doubles for
those ports. A double result is not a GitHub execution and is not a MacCI
receipt.

Formal integration still requires one tree that contains both:

- W01 ``33e65dc0925f03e60518a5c91a0e2254fd0fd12f``
- W02 ``5dd0eb38e175a8a905836fbdbac5633ff9eb19ae``
"""

from .scheduler import Scheduler

W01_HEAD = "33e65dc0925f03e60518a5c91a0e2254fd0fd12f"
W02_HEAD = "5dd0eb38e175a8a905836fbdbac5633ff9eb19ae"
PRODUCTION_WIRED = False
JOINT_INTEGRATION_READY = False
GITHUB_EXECUTION = False
MACCI_PROOF = False

__all__ = [
    "Scheduler",
    "W01_HEAD",
    "W02_HEAD",
    "PRODUCTION_WIRED",
    "JOINT_INTEGRATION_READY",
    "GITHUB_EXECUTION",
    "MACCI_PROOF",
]
