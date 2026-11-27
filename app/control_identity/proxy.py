"""Credential proxy that stores generations and scopes, never secret values."""

from __future__ import annotations

from .errors import ControlError

SCOPES = ("feedback", "pull", "merge", "publish", "read")
OPERATION_SCOPE = {
    "feedback.write": "feedback",
    "pull.read": "pull",
    "preview.merge": "merge",
    "release.publish": "publish",
    "diagnostic.read": "read",
}


def check_proxy(proxy_state: dict, *, scope: str, generation: int, operation: str) -> None:
    required = OPERATION_SCOPE.get(operation)
    if required is None:
        raise ControlError("NOT_AUTHORIZED", 403, "operation has no credential scope", reason="cross_scope")
    if scope not in SCOPES or scope != required:
        raise ControlError("NOT_AUTHORIZED", 403, "operation scope is not interchangeable", reason="cross_scope")
    current = proxy_state.get(required)
    if not isinstance(current, int) or int(generation) != current:
        raise ControlError("NOT_AUTHORIZED", 403, "credential generation rejected", reason="stale_generation")


def rotate(proxy_state: dict, scope: str) -> int:
    if scope not in SCOPES:
        raise ControlError("NOT_AUTHORIZED", 403, "unknown credential scope", reason="cross_scope")
    proxy_state[scope] = int(proxy_state[scope]) + 1
    return proxy_state[scope]
