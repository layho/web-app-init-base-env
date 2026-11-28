"""Server-side W02 authorization port.

A caller-built token is not a grant. ``SyntheticHost`` is a test double and
is not an authority. ``test_double=True`` on a grant only labels this
isolated package; it does not authorize anything. Decisions come from
``ServerPolicy`` and are default-deny. The issued grant binds the policy's
exact principal, scopes, app, repo, host, resource scope, and run scope.
"""

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class ServerPolicy:
    """Trusted server allow-list. This object is not a host probe."""

    principal: str
    app_id: str
    repo: str
    host_id: str
    scopes: frozenset
    run_scope: str
    resource_scope: str
    enabled: bool = True

    def __post_init__(self):
        object.__setattr__(self, "scopes", frozenset(self.scopes))

    def bound(self):
        return bool(
            self.enabled
            and self.principal
            and self.app_id
            and self.repo
            and self.host_id
            and self.scopes
            and self.run_scope == f"runs:{self.app_id}"
            and self.resource_scope == f"inventory:{self.app_id}"
        )

    def permits(self, principal, scopes, object_id):
        if not self.bound():
            return False
        wanted = frozenset(scopes)
        if not wanted or not wanted <= self.scopes:
            return False
        return principal == self.principal and object_id == self.app_id


@dataclass(frozen=True)
class Grant:
    principal: str
    scopes: frozenset
    object_id: str
    token: str
    test_double: bool = True
    authority: str = ""
    repo: str = ""
    host_id: str = ""
    resource_scope: str = ""
    run_scope: str = ""


class DenyAllAuthorizationPort:
    """Default port. No principal, scope, or object is issued."""

    def issue(self, principal, scopes, object_id):
        return None

    def authenticate(self, grant):
        return False


class W02AuthorizationPort:
    """Isolated stand-in for the W02 policy service.

    Only a ``ServerPolicy`` can issue. A host double passed in its place is
    not consulted. Production enablement stays off; signed grants remain
    labeled test doubles so they cannot be read as host or MacCI proof.
    """

    AUTHORITY = "w02-server-policy"

    def __init__(self, policy):
        self.policy = policy if isinstance(policy, ServerPolicy) else None
        self._secret = secrets.token_bytes(32)

    def issue(self, principal, scopes, object_id):
        if self.policy is None or not self.policy.permits(principal, scopes, object_id):
            return None
        scope_set = frozenset(scopes)
        fields = {
            "authority": self.AUTHORITY,
            "host_id": self.policy.host_id,
            "object_id": object_id,
            "principal": principal,
            "repo": self.policy.repo,
            "resource_scope": self.policy.resource_scope,
            "run_scope": self.policy.run_scope,
            "scopes": sorted(scope_set),
            "test_double": True,
        }
        return Grant(
            principal,
            scope_set,
            object_id,
            self._token(fields),
            True,
            self.AUTHORITY,
            self.policy.repo,
            self.policy.host_id,
            self.policy.resource_scope,
            self.policy.run_scope,
        )

    def authenticate(self, grant):
        if not isinstance(grant, Grant) or self.policy is None:
            return False
        if grant.authority != self.AUTHORITY or grant.test_double is not True:
            return False
        if not self.policy.permits(grant.principal, grant.scopes, grant.object_id):
            return False
        if (
            grant.repo != self.policy.repo
            or grant.host_id != self.policy.host_id
            or grant.resource_scope != self.policy.resource_scope
            or grant.run_scope != self.policy.run_scope
        ):
            return False
        fields = {
            "authority": grant.authority,
            "host_id": grant.host_id,
            "object_id": grant.object_id,
            "principal": grant.principal,
            "repo": grant.repo,
            "resource_scope": grant.resource_scope,
            "run_scope": grant.run_scope,
            "scopes": sorted(frozenset(grant.scopes)),
            "test_double": True,
        }
        try:
            expected = self._token(fields)
            return hmac.compare_digest(expected, grant.token)
        except (TypeError, ValueError):
            return False

    def _token(self, fields):
        body = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()
