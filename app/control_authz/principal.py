"""Server-assigned principals.

Every field is filled by the server from the registry. A request body cannot
construct a Principal or choose trusted_source.
"""

from __future__ import annotations

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
}


@dataclass(frozen=True)
class Principal:
    principal_id: str
    kind: str
    repo_id: str
    app_id: str
    producer_id: str
    stream_id: str
    scopes: tuple[str, ...]
    trusted_source: str


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
    """Server-side registry switch. Records live in durable state, not in a request."""

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

    expected = SOURCE_BY_KIND.get(principal.kind)
    if not expected or principal.trusted_source != expected:
        raise AuthzError("NOT_AUTHORIZED", 403, "trusted source is assigned by the server", reason="trusted_source")
    return expected


def server_source(kind: str) -> str:
    return SOURCE_BY_KIND.get(kind, "")
