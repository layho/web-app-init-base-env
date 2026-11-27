"""Default-deny authorization against a server-built principal registry.

``AuthzService`` is constructed with the registry, the isolated flag, and the
policy revision. It has no constructor that reads a request body. W02 may
subclass ``PrincipalRegistry`` and verify proofs inside ``resolve``; callers
still only see ``authenticate``, ``authorize``, and ``trusted_source_for``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from .errors import AuthnError, AuthzError
from .principal import Principal

POLICY_REVISION = "w01-isolated-1"

OPERATIONS = frozenset({
    "event.append",
    "event.read",
    "diagnostic.read",
    "diagnostic.cursor",
    "report.submit",
    "report.read",
})

_ISOLATED_PRODUCERS = (
    ("fixture", "task", "", ()),
    ("fixture_b", "task", "", ()),
    ("fixture_c", "task", "", ()),
    ("host_deployer", "deployment", "host_deployer", ()),
    ("other_deployer", "deployment", "host_deployer", ()),
    ("side_observer", "deployment", "", ()),
    ("policy_service", "authorization", "policy_service", ("preview.merge", "preview.observe")),
    ("policy_service_b", "authorization", "policy_service", ("preview.merge", "preview.observe")),
    ("hostops", "diagnostic", "hostops_readonly", ("diagnostic.read", "diagnostic.cursor")),
    ("hostops_b", "diagnostic", "hostops_readonly", ("diagnostic.read", "diagnostic.cursor")),
)


def _credential(repo_id: str, producer_id: str, policy_revision: str) -> str:
    material = f"v060-w01-authz\n{policy_revision}\n{repo_id}\n{producer_id}".encode()
    return hashlib.sha256(material).hexdigest()


class PrincipalRegistry:
    """Server-side credential to principal map.

    Subclasses override ``resolve`` to verify a proof and then return the
    server principal. The request body is not a source of principal fields.
    """

    def __init__(self, principals: Mapping[str, Principal], *, allow_test_identities: bool):
        self._principals = dict(principals)
        self.allow_test_identities = bool(allow_test_identities)
        self._by_producer = {principal.producer_id: credential for credential, principal in self._principals.items()}

    def resolve(self, credential: str) -> Principal | None:
        if not isinstance(credential, str) or not credential:
            return None
        principal = self._principals.get(credential)
        if principal is None:
            return None
        if principal.kind == "test" and not self.allow_test_identities:
            return None
        return principal

    def credential_for(self, producer_id: str) -> str:
        credential = self._by_producer.get(producer_id)
        if credential is None:
            raise AuthnError("producer is not in the server registry")
        return credential


class AuthzService:
    def __init__(self, *, registry: PrincipalRegistry, isolated: bool, policy_revision: str):
        if not isinstance(registry, PrincipalRegistry):
            raise AuthzError("registry must be server-side configuration")
        if type(isolated) is not bool:
            raise AuthzError("isolated flag must be server-side configuration")
        if not isinstance(policy_revision, str) or not policy_revision:
            raise AuthzError("policy revision must be server-side configuration")
        self.registry = registry
        self.isolated = isolated
        self.policy_revision = policy_revision

    @classmethod
    def from_request(cls, _body: object) -> "AuthzService":
        raise AuthzError("AuthzService cannot be configured from the request body")

    def authenticate(self, material: object) -> Principal:
        if isinstance(material, Mapping) and "authorizer" in material and material.get("authorizer") in (None, "", {}):
            raise AuthnError("empty authorizer")
        credential = material.get("credential") if isinstance(material, Mapping) else material
        if not isinstance(credential, str) or credential.strip() == "":
            raise AuthnError("empty authorizer")
        principal = self.registry.resolve(credential)
        if principal is None:
            raise AuthnError("unauthenticated")
        if principal.kind == "test" and not self.isolated:
            raise AuthnError("test identity is rejected on this route")
        if principal.kind == "test" and not self.registry.allow_test_identities:
            raise AuthnError("test identity registry is disabled")
        return principal

    def authorize(self, principal: Principal, operation: str, resource_binding: Mapping) -> None:
        if not isinstance(principal, Principal):
            raise AuthzError("unauthorized")
        if operation not in OPERATIONS or operation not in principal.scopes:
            raise AuthzError("unknown operation")
        if not isinstance(resource_binding, Mapping):
            raise AuthzError("unauthorized")
        for field in ("repo_id", "app_id", "producer_id", "stream_id"):
            if resource_binding.get(field) != getattr(principal, field):
                raise AuthzError("unauthorized")
        tasks = {item.removeprefix("task:") for item in principal.scopes if item.startswith("task:")}
        requested = []
        if resource_binding.get("task_id"):
            requested.append(resource_binding["task_id"])
        scope = resource_binding.get("scope")
        if isinstance(scope, Mapping) and scope.get("task_id"):
            requested.append(scope["task_id"])
        for task_id in requested:
            if task_id not in tasks:
                raise AuthzError("cross-task")
        business = resource_binding.get("business_operation")
        if business is not None and business not in principal.scopes:
            raise AuthzError("unauthorized")

    def trusted_source_for(self, principal: Principal) -> str:
        if not isinstance(principal, Principal):
            raise AuthzError("unauthorized")
        return principal.trusted_source


def isolated_registry(repo_id: str, *, app_id: str = "base", task_id: str = "tsk_w01") -> PrincipalRegistry:
    """Test identities for an isolated engine. Disabled when the registry flag is off."""
    principals = {}
    for producer_id, channel, source, extra in _ISOLATED_PRODUCERS:
        credential = _credential(repo_id, producer_id, POLICY_REVISION)
        scopes = frozenset({
            "event.append",
            "event.read",
            "report.submit",
            "report.read",
            f"task:{task_id}",
            *extra,
        })
        principals[credential] = Principal(
            principal_id=f"prn_{producer_id}",
            kind="test",
            repo_id=repo_id,
            app_id=app_id,
            producer_id=producer_id,
            stream_id=f"{producer_id}:{channel}",
            scopes=scopes,
            trusted_source=source,
        )
    return PrincipalRegistry(principals, allow_test_identities=True)
