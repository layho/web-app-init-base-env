"""Stable server-side authentication and authorization API.

W02 owns this package. W01 imports it and does not keep a second
implementation. Client actor, trusted_source, authorizer, test-proof, and
gate fields are not authority. Test identities authenticate only when the
server registry is isolated and the route class is ``isolated``.

Public types and signatures
---------------------------
``Principal`` is frozen. Every field is assigned by the server:

    Principal(
        principal_id: str,
        kind: str,
        repo_id: str,
        app_id: str,
        producer_id: str,
        stream_id: str,
        scopes: frozenset[str],
        trusted_source: str,
    )

``scopes`` is a ``frozenset[str]``. A tuple or list passed to the
constructor is copied into a frozenset and is not preserved as a tuple.
A string is rejected.

``AuthnError`` and ``AuthzError`` share one constructor:

    AuthnError(code: str, status: int, detail: str, *, reason: str = "", extra: dict | None = None)
    AuthzError(code: str, status: int, detail: str, *, reason: str = "", extra: dict | None = None)

Attributes are ``code``, ``status``, ``detail``, ``reason``, and ``extra``.
``AuthnError`` means the caller is not authenticated. ``AuthzError`` means
the caller is authenticated and not allowed. Unknown operations raise
``AuthzError``.

``PrincipalRegistry`` is server configuration, not a request body:

    PrincipalRegistry(*, isolated: bool)

``AuthzService`` is constructed only from that registry, the isolated flag,
and an integer policy revision. The revision matches the frozen contract
(an int, not a str). ``AuthzService.from_request(body)`` always raises
``AuthzError``.

    AuthzService(*, registry: PrincipalRegistry, isolated: bool, policy_revision: int)
    authenticate(self, *, operation: str, body: dict, route_class: str, now: int, state: dict, write: bool) -> Principal
    authorize(self, principal: Principal, operation: str, resource_binding: ResourceBinding) -> None
    trusted_source_for(self, principal: Principal) -> str

``trusted_source_for(principal: Principal) -> str`` is the same lookup.
The source comes from ``SOURCE_BY_KIND``, which includes ``policy_service``.
``MATRIX`` is the role map and includes ``event.append``, ``event.read``,
``diagnostic.cursor``, ``report.submit``, and ``report.read``.
``REQUIRED_SCOPES`` lists every scope an operation requires. A missing entry
denies. Authorization checks the presented scope and, for a conjunction, the
whole set.

Joint W01+W02 isolation tests run after W01 adapts its callers to these
signatures and drops its copy of this package. Point ``PYTHONPATH`` at one
tree that contains this ``app/control_authz`` and invoke W01's package tests
together with ``tests/test_v060_w02_identity.py``,
``tests/test_v060_w02_cryptography_lock.py``, and
``tests/test_v060_w00_contract.py``. Do not merge the two implementations
into one file and do not treat that joint run as done until W01 has adapted.
"""

from .authorize import MATRIX, REQUIRED_SCOPES, authorize
from .errors import AuthnError, AuthzError
from .principal import SOURCE_BY_KIND, Principal, PrincipalRegistry, ResourceBinding, trusted_source_for
from .service import READ_REPLAY_POLICY, WRITE_REPLAY_POLICY, AuthzService

__all__ = [
    "AuthnError",
    "AuthzError",
    "AuthzService",
    "MATRIX",
    "Principal",
    "PrincipalRegistry",
    "READ_REPLAY_POLICY",
    "REQUIRED_SCOPES",
    "ResourceBinding",
    "SOURCE_BY_KIND",
    "WRITE_REPLAY_POLICY",
    "authorize",
    "trusted_source_for",
]
