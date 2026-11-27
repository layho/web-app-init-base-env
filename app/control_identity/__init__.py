"""Isolated V0.6.0 W02 identity, credential proxy, and policy package.

This package does not enable production verification and does not replace
``app.auth``. Test identities verify only when an isolated app is built
explicitly. S-auth and S-main stay untouched.
"""

from .service import ControlIdentityService

PRODUCTION_VERIFICATION_ENABLED = False

__all__ = ["ControlIdentityService", "PRODUCTION_VERIFICATION_ENABLED"]
