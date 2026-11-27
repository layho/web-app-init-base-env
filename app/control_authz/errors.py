"""Authentication and authorization failures. Default deny."""

from __future__ import annotations


class AuthError(Exception):
    def __init__(self, code: str, status: int, detail: str, *, reason: str = "", extra: dict | None = None):
        super().__init__(detail)
        self.code = code
        self.status = status
        self.detail = detail
        self.reason = reason
        self.extra = dict(extra or {})


class AuthnError(AuthError):
    """The caller is not authenticated. Empty authorizer is this failure."""


class AuthzError(AuthError):
    """The caller is authenticated and not allowed. Unknown operations fail here."""
