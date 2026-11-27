"""Server-assigned principals.

Every field is filled by the server. A request body cannot construct a
Principal or choose trusted_source. ``scopes`` is a frozenset of strings.
``SOURCE_BY_KIND`` is the only trusted-source map, including
``policy_service`` for authorization events.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .errors import AuthzError

SOURCE_BY_KIND = {
    "core_session": "core_session",
    "app_service": "app_service",
    "agent": "agent",
    "macci": "macci",
    "publisher": "publisher",
    "host": "host_deployer",
    "hostops": "hostops_readonly",
    "user_acceptance": "user_acceptance",
    "candidate": "candidate",
    "policy_service": "policy_service",
}


@dataclass(frozen=True)
class Principal:
    principal_id: str
    kind: str
    repo_id: str
    app_id: str
    producer_id: str
    stream_id: str
    scopes: frozenset[str]
    trusted_source: str

    def __post_init__(self) -> None:
        for name in ("principal_id", "kind", "repo_id", "app_id", "producer_id", "stream_id", "trusted_source"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(name)
        scopes = self.scopes
        if isinstance(scopes, str) or not isinstance(scopes, Iterable):
            raise TypeError("scopes")
        copied = frozenset(scopes)
        if any(not isinstance(item, str) or item == "" for item in copied):
            raise TypeError("scopes")
        object.__setattr__(self, "scopes", copied)


@dataclass(frozen=True)
class ResourceBinding:
    repo_id: str
    app_id: str
    producer_id: str
    stream_id: str
    operation: str
    scope: str
    task_id: str
    target_app_id: str


class PrincipalRegistry:
    """Server-side registry switch.

    ``isolated`` is a bool supplied by server configuration. Records live in
    durable state, not in a request, and this object is not built from a
    request body.
    """

    def __init__(self, *, isolated: bool):
        if not isinstance(isolated, bool):
            raise AuthzError("NOT_AUTHORIZED", 403, "registry isolation flag must be server-side", reason="server_configuration")
        self.isolated = isolated

    @property
    def test_identities_enabled(self) -> bool:
        return self.isolated


def trusted_source_for(principal: Principal) -> str:
    """Return the server-mapped source for this principal.

    The value is looked up from the principal kind. It is not read from a
    client field, and a mismatched trusted_source on the record is denied.
    """

    if not isinstance(principal, Principal):
        raise AuthzError("NOT_AUTHORIZED", 403, "trusted source is assigned by the server", reason="trusted_source")
    expected = SOURCE_BY_KIND.get(principal.kind)
    if not expected or principal.trusted_source != expected:
        raise AuthzError("NOT_AUTHORIZED", 403, "trusted source is assigned by the server", reason="trusted_source")
    return expected


def server_source(kind: str) -> str:
    return SOURCE_BY_KIND.get(kind, "")
