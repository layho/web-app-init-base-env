"""Explicit W03–W07 ports.

Default objects are not constructed here. Callers pass fixtures or, later, a
joint adapter. A fixture result is synthetic. It is not a build, deploy,
merge, or production proof. This module does not import those packages.
"""

from datetime import timedelta, timezone


class ServerClock:
    """Injectable server clock. A caller timestamp cannot move it."""

    def __init__(self, now):
        if now.tzinfo is None:
            raise ValueError("server clock requires an aware datetime")
        self._now = now.astimezone(timezone.utc)

    def now(self):
        return self._now

    def advance(self, seconds):
        self._now = self._now + timedelta(seconds=seconds)
        return self._now


class Grant:
    def __init__(self, principal_id, app_id, action):
        self.principal_id = principal_id
        self.app_id = app_id
        self.action = action


class _Fixture:
    """Test double. ``read`` is the only successful method."""

    def __init__(self, report):
        self.report = dict(report or {})
        self.calls = []

    def read(self, *, app_id, candidate_id=None):
        self.calls.append({"method": "read", "app_id": app_id, "candidate_id": candidate_id})
        data = dict(self.report)
        data["runtime_evidence"] = False
        data["synthetic"] = True
        data.setdefault("status", "test_double")
        data.setdefault("evidence_state", "present")
        return data

    def apply(self, **kwargs):
        self.calls.append({"method": "apply"})
        raise AssertionError("apply")

    def rollback(self, **kwargs):
        self.calls.append({"method": "rollback"})
        raise AssertionError("rollback")

    def merge(self, **kwargs):
        self.calls.append({"method": "merge"})
        raise AssertionError("merge")

    def enroll(self, **kwargs):
        self.calls.append({"method": "enroll"})
        raise AssertionError("enroll")

    def register(self, **kwargs):
        self.calls.append({"method": "register"})
        raise AssertionError("register")

    def overwrite(self, **kwargs):
        self.calls.append({"method": "overwrite"})
        raise AssertionError("overwrite")

    def switch(self, **kwargs):
        self.calls.append({"method": "switch"})
        raise AssertionError("switch")


class FixtureBuild(_Fixture):
    """W03 build and isolation observation."""


class FixtureSchedule(_Fixture):
    """W04 scheduler registration observation. This object cannot register."""


class FixtureDelivery(_Fixture):
    """W05 candidate and MacCI observation. This object cannot merge."""


class FixtureDiagnostics(_Fixture):
    """W06 read-only diagnostic observation."""


class FixtureDeploy(_Fixture):
    """W07 runtime observation. This object cannot deploy or roll back."""
