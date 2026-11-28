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


class ServerPrincipal:
    """W02 server principal.

    The server builds this object. A request actor id is not a principal and
    cannot fill these fields. ``server_assigned`` stays false unless this
    constructor sets it.
    """

    def __init__(self, *, principal_id, kind, repo_id, tasks, trusted_source, app_id="", app_ids=None, producer_id="w08", stream_id="upgrade"):
        self.principal_id = principal_id
        self.kind = kind
        self.repo_id = repo_id
        self.producer_id = producer_id
        self.stream_id = stream_id
        self.tasks = frozenset(tasks)
        self.trusted_source = trusted_source
        self.server_assigned = True
        if app_ids is None:
            app_ids = () if app_id == "" else (app_id,)
        self.app_ids = frozenset(app_ids)
        self.app_id = app_id if app_id else (next(iter(self.app_ids)) if len(self.app_ids) == 1 else "")


class ServerManifest:
    """Server-held ownership, diff, and isolation snapshot.

    Callers cannot submit this object. It is not repository proof of a live
    tree and it is not runtime proof.
    """

    def __init__(self, *, candidate, allowlist, changes, observed_files, peers, repository_sha=None, repository_version=None, source_id="w08-server-manifest", observed_at=None):
        self.candidate = {
            "head": None if not isinstance(candidate, dict) else candidate.get("head"),
            "base": None if not isinstance(candidate, dict) else candidate.get("base"),
            "merge": None if not isinstance(candidate, dict) else candidate.get("merge"),
            "tree": None if not isinstance(candidate, dict) else candidate.get("tree"),
        }
        self.allowlist = list(allowlist or [])
        self.changes = list(changes or [])
        self.observed_files = list(observed_files or [])
        self.peers = peers
        self.repository_sha = repository_sha
        self.repository_version = repository_version
        self.source_id = source_id
        self.observed_at = observed_at
        self.server_controlled = True
        self.not_runtime_proof = True


class Grant:
    def __init__(self, principal_id, app_id, action, *, repo=None, task_id=None, candidate_id=None, plan_id=None):
        self.principal_id = principal_id
        self.app_id = app_id
        self.action = action
        self.repo = repo
        self.task_id = task_id
        self.candidate_id = candidate_id
        self.plan_id = plan_id


class _Fixture:
    """Test double. ``read`` is the only successful method."""

    def __init__(self, report):
        self.report = dict(report or {})
        self.calls = []

    def read(self, *, app_id, candidate_id=None):
        self.calls.append({"method": "read", "app_id": app_id, "candidate_id": candidate_id})
        data = dict(self.report)
        data["runtime_evidence"] = False
        data["runtime_proof"] = False
        data["macci_proof"] = False
        data["synthetic"] = True
        data["test_double"] = True
        data["not_runtime_proof"] = True
        data["proof_label"] = "test_double_not_runtime_proof"
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
