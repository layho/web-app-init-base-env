"""Shared server-side authentication and authorization.

W01 and W02 import this package. Proof verification belongs behind
``PrincipalRegistry.resolve`` (W02). This module does not trust client
actor, trusted_source, authorizer, test-proof, or gate fields.

The public surface is ``Principal``, ``AuthnError``, ``AuthzError``,
and ``AuthzService``.
"""

from .errors import AuthnError, AuthzError
from .principal import Principal
from .service import AuthzService, PrincipalRegistry, isolated_registry

__all__ = [
    "AuthnError",
    "AuthzError",
    "AuthzService",
    "Principal",
    "PrincipalRegistry",
    "isolated_registry",
]
