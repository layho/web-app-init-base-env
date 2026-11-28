"""Public error values for the isolated identity service."""

from __future__ import annotations

from .boundary import redact


class ControlError(Exception):
    def __init__(self, code: str, status: int, detail: str, *, reason: str = "", extra: dict | None = None):
        super().__init__(detail)
        self.code = code
        self.status = status
        self.detail = detail
        self.reason = reason
        self.extra = dict(extra or {})

    def public_body(self) -> dict:
        body = {
            "schema_version": 2,
            "http": self.status,
            "code": self.code,
            "detail": redact(self.detail),
            "side_effect": False,
        }
        if self.reason:
            body["reason"] = self.reason
        for key, value in self.extra.items():
            if key == "detail":
                body[key] = redact(str(value))
            else:
                body[key] = value
        body["side_effect"] = False
        return body


class Result:
    def __init__(self, status: int, body: dict):
        self.status = status
        self.body = body
