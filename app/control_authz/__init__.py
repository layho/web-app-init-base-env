"""Shared server-side authentication and authorization API.

W01 imports this package. W02 implements proof verification, nonce
consumption, and the principal registry behind it. Client-supplied actor,
trusted_source, authorizer, test-proof, and gates are never authority.
"""

from .authorize import authorize
from .errors import AuthnError, AuthzError
from .principal import Principal, PrincipalRegistry, ResourceBinding, trusted_source_for
from .service import READ_REPLAY_POLICY, WRITE_REPLAY_POLICY, AuthzService

__all__ = [
    "AuthnError",
    "AuthzError",
    "AuthzService",
    "Principal",
    "PrincipalRegistry",
    "READ_REPLAY_POLICY",
    "ResourceBinding",
    "WRITE_REPLAY_POLICY",
    "authorize",
    "trusted_source_for",
]
