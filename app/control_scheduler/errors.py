"""Scheduler errors. These are local decisions, not production HTTP routes."""


class SchedulerError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 409, extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
        self.extra = dict(extra or {})

    def public(self) -> dict:
        body = {
            "schema_version": 2,
            "http": self.status,
            "code": self.code,
            "message": self.message,
            "lease_token": None,
            "github_execution": False,
            "macci_proof": False,
            "production_wired": False,
            "production_enabled": False,
            "side_effect": False,
        }
        body.update(self.extra)
        body["lease_token"] = None
        body["github_execution"] = False
        body["macci_proof"] = False
        body["production_enabled"] = False
        return body
