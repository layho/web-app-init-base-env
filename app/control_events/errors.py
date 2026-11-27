"""Error values for the isolated V0.6.0 event engine.

Production protocol enablement is not represented here. Callers map these
codes onto HTTP responses; nothing in this package mounts a production route.
"""


class ControlError(Exception):
    def __init__(self, code: str, http: int, message: str):
        super().__init__(message)
        self.code = code
        self.http = http
        self.message = message

    def public(self) -> dict:
        return {
            "schema_version": 2,
            "http": self.http,
            "code": self.code,
            "message": self.message,
            "lease_token": None,
            "side_effect": False,
        }


class BadRecord(ControlError):
    def __init__(self, message: str = "bad delivery record"):
        super().__init__("QUARANTINED", 422, message)


class DeliveryUnknown(ControlError):
    def __init__(self, message: str = "delivery outcome is unknown"):
        super().__init__("SIDE_EFFECT_UNKNOWN", 409, message)
