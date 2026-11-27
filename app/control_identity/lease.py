"""Opaque lease tokens bound to principal, run, scope, and generation."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from .errors import ControlError

LEASE_TTL = 60


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def fence(run: dict | None, reason: str) -> ControlError:
    state = run["state"] if run else None
    return ControlError(
        "LEASE_FENCE_REJECTED",
        409,
        "lease rejected",
        reason=reason,
        extra={"changed": False, "state": state},
    )


def assert_bound(run: dict | None, *, principal_id: str, token: str, generation: int, now: int,
                 repo: str, operation_scope: str) -> None:
    if run is None:
        raise fence(None, "missing")
    if token is None or token == "" or str(token) == str(generation) or str(token) == str(run.get("generation")):
        raise fence(run, "token")
    try:
        requested = int(generation)
    except (TypeError, ValueError):
        raise fence(run, "generation") from None
    if requested != int(run["generation"]):
        raise fence(run, "generation")
    if not hmac.compare_digest(token_hash(str(token)), str(run.get("token_hash") or "")):
        raise fence(run, "token")
    if run.get("principal_id") != principal_id:
        raise fence(run, "principal")
    if run.get("scope") != repo or run.get("operation_scope") != operation_scope:
        raise fence(run, "scope")
    if int(now) >= int(run["expires_at"]):
        raise fence(run, "expired")
