"""Server-issued object grants. A caller-built token is not a grant."""

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class Grant:
    principal: str
    scopes: frozenset
    object_id: str
    token: str
    test_double: bool


class Authorizer:
    """In-process server authorizer. Tokens from this object are test doubles."""

    def __init__(self):
        self._secret = secrets.token_bytes(32)

    def issue(self, principal, scopes, object_id):
        scope_set = frozenset(scopes)
        token = self._token(principal, scope_set, object_id, True)
        return Grant(principal, scope_set, object_id, token, True)

    def authenticate(self, grant):
        if not isinstance(grant, Grant) or grant.test_double is not True:
            return False
        expected = self._token(grant.principal, frozenset(grant.scopes), grant.object_id, True)
        return hmac.compare_digest(expected, grant.token)

    def _token(self, principal, scopes, object_id, test_double):
        body = json.dumps(
            {
                "object_id": object_id,
                "principal": principal,
                "scopes": sorted(scopes),
                "test_double": test_double,
            },
            separators=(",", ":"),
        )
        return hmac.new(self._secret, body.encode("utf-8"), hashlib.sha256).hexdigest()
