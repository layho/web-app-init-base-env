"""Ports for W01 events, W02 authorization, W04 scheduling, W05 delivery, and W06 diagnostics.

This package does not import those trees. ``Blocked*`` ports report
INTERFACE_PENDING. ``Fixture*`` ports are test doubles: they are not GitHub,
MacCI, merge, or deploy proof, and their results must not be promoted.
"""

from __future__ import annotations


def _blocked(operation):
    return {
        "ok": False,
        "allowed": False,
        "allow": False,
        "blocked": True,
        "pending": True,
        "applied": False,
        "changed": False,
        "performed": False,
        "running": False,
        "side_effect": False,
        "http": 409,
        "code": "INTERFACE_PENDING",
        "status": "BLOCKED",
        "operation": operation,
        "interface": "pending",
        "evidence_state": "missing",
        "runtime_evidence": False,
        "test_double": False,
        "github_proof": False,
        "macci_proof": False,
        "real_merge": False,
        "real_deploy": False,
        "items": None,
        "streams": None,
        "tasks": None,
        "chains": None,
    }


class Grant:
    def __init__(self, principal_id, app_id, action):
        self.principal_id = principal_id
        self.app_id = app_id
        self.action = action


class BlockedAuthorizationPort:
    calls = ()
    action_calls = ()

    def evaluate(self, principal, *, app_id, action, task_id=None, candidate_id=None):
        del principal, app_id, task_id, candidate_id
        return _blocked(action)

    def request_action(self, principal, *, app_id, task_id, action, idempotency_key, expected_revision):
        del principal, app_id, task_id, action, idempotency_key, expected_revision
        return _blocked("task.request_action")


class BlockedEventPort:
    def __init__(self):
        self.calls = []

    def catalog(self, *, request, app_id):
        del request, app_id
        self.calls.append("catalog")
        return _blocked("event.catalog")

    def query(self, *, request, stream_id, stream_epoch, after_seq=0, task_id=None, limit=50):
        del request, stream_id, stream_epoch, after_seq, task_id, limit
        self.calls.append("query")
        return _blocked("event.query")


class BlockedSchedulePort:
    def __init__(self):
        self.calls = []
        self.intervene_calls = []

    def snapshot(self, principal, *, app_id, version):
        del principal, app_id, version
        self.calls.append("snapshot")
        return _blocked("schedule.snapshot")

    def intervene(self, principal, *, action, subject_id, evidence_ref, successor_id="", note=""):
        del principal, action, subject_id, evidence_ref, successor_id, note
        self.intervene_calls.append(action)
        return _blocked("schedule.intervene")


class BlockedDeliveryPort:
    def __init__(self):
        self.calls = []

    def project(self, principal, *, app_id, task_id, scope):
        del principal, app_id, task_id, scope
        return _blocked("delivery.project")

    def coordinate(self, principal, *, action, task_id, chain_id, idempotency_key, expected_revision, scope):
        del principal, action, task_id, chain_id, idempotency_key, expected_revision, scope
        self.calls.append(f"coordinate:{action}")
        return _blocked("delivery.coordinate")

    def request_publish(self, principal, *, task_id, chain_id, idempotency_key, expected_revision, scope):
        del task_id, chain_id, idempotency_key, expected_revision, scope, principal
        self.calls.append("request_publish")
        return _blocked("delivery.request_publish")


class BlockedDiagnosticPort:
    def poll(self, principal, *, app_id, subject=None, after_seq=0):
        del principal, app_id, subject, after_seq
        return _blocked("diagnostic.poll")


class FixtureAuthorizationPort:
    """In-memory authorization double. Not a signature oracle."""

    def __init__(self, grants, responses=None):
        self.grants = list(grants)
        self.responses = dict(responses or {})
        self.calls = []
        self.action_calls = []
        self.allow_any = False

    def evaluate(self, principal, *, app_id, action, task_id=None, candidate_id=None):
        del task_id, candidate_id
        self.calls.append(("evaluate", action, app_id))
        if self.allow_any:
            return self._allow(action)
        principal_id = principal.get("id") if isinstance(principal, dict) else None
        for grant in self.grants:
            if grant.principal_id == principal_id and grant.app_id == app_id and grant.action == action:
                return self._allow(action)
        return {
            "ok": False,
            "allowed": False,
            "allow": False,
            "blocked": False,
            "http": 403,
            "code": "POLICY_DENIED",
            "side_effect": False,
            "test_double": True,
            "github_proof": False,
            "macci_proof": False,
            "interface": "test_double",
            "action": action,
        }

    def request_action(self, principal, *, app_id, task_id, action, idempotency_key, expected_revision):
        del principal, app_id, idempotency_key, expected_revision
        self.action_calls.append(("request_action", {"action": action, "task_id": task_id}))
        body = {
            "schema_version": 2,
            "http": 409,
            "state": "READY",
            "running": False,
            "ok": False,
            "code": "UNKNOWN_EXECUTOR",
            "side_effect": False,
            "test_double": True,
            "github_proof": False,
            "macci_proof": False,
            "connected_executor": False,
            "performed": False,
        }
        override = self.responses.get(action)
        if isinstance(override, dict):
            body.update(override)
        body["test_double"] = True
        body["github_proof"] = False
        body["macci_proof"] = False
        body["connected_executor"] = False
        return body

    def _allow(self, action):
        return {
            "ok": True,
            "allowed": True,
            "allow": True,
            "blocked": False,
            "http": 200,
            "code": None,
            "side_effect": False,
            "test_double": True,
            "github_proof": False,
            "macci_proof": False,
            "interface": "test_double",
            "action": action,
        }


class FixtureEventPort:
    def __init__(self, events):
        self.events = list(events)
        self.calls = []
        self.side_effect = False

    def catalog(self, *, request, app_id):
        del request, app_id
        self.calls.append("catalog")
        if self.side_effect:
            return {"side_effect": True, "streams": None, "code": "SIDE_EFFECT_UNKNOWN", "test_double": True}
        streams = []
        seen = set()
        for item in self.events:
            stream_id = item.get("stream_id")
            epoch = item.get("stream_epoch", 1)
            key = (stream_id, epoch)
            if not stream_id or key in seen:
                continue
            seen.add(key)
            streams.append({"stream_id": stream_id, "stream_epoch": epoch})
        return {
            "side_effect": False,
            "streams": streams,
            "test_double": True,
            "github_proof": False,
            "macci_proof": False,
            "runtime_evidence": False,
        }

    def query(self, *, request, stream_id, stream_epoch, after_seq=0, task_id=None, limit=50):
        del request, task_id
        self.calls.append("query")
        if self.side_effect:
            return {
                "side_effect": True,
                "items": [dict(item) for item in self.events],
                "code": "SIDE_EFFECT_UNKNOWN",
                "test_double": True,
            }
        matched = []
        watermark = 0
        for item in self.events:
            if item.get("stream_id") != stream_id or item.get("stream_epoch", 1) != stream_epoch:
                continue
            seq = item.get("producer_seq")
            if type(seq) is int and not isinstance(seq, bool):
                watermark = max(watermark, seq)
                if seq > after_seq:
                    matched.append(dict(item))
        matched.sort(key=lambda item: item["producer_seq"])
        page = matched[:limit]
        return {
            "side_effect": False,
            "items": page,
            "watermark_seq": watermark,
            "gap": False,
            "truncated": len(matched) > limit,
            "next_after_seq": page[-1]["producer_seq"] if page else after_seq,
            "test_double": True,
            "github_proof": False,
            "macci_proof": False,
            "runtime_evidence": False,
        }


class FixtureSchedulePort:
    def __init__(self, snapshot):
        self.snapshot_body = snapshot
        self.calls = []
        self.intervene_calls = []

    def snapshot(self, principal, *, app_id, version):
        del principal, app_id
        self.calls.append("snapshot")
        return {
            "side_effect": False,
            "runtime_evidence": False,
            "test_double": True,
            "github_proof": False,
            "macci_proof": False,
            "interface": "test_double",
            "version": version,
            "tasks": list(self.snapshot_body.get("tasks") or []),
            "chains": list(self.snapshot_body.get("chains") or []),
            "rounds": list(self.snapshot_body.get("rounds") or []),
        }

    def intervene(self, principal, *, action, subject_id, evidence_ref, successor_id="", note=""):
        del principal, subject_id, evidence_ref, successor_id, note
        self.intervene_calls.append(action)
        return {
            "http": 202,
            "code": None,
            "state": "REQUESTED",
            "changed": False,
            "performed": False,
            "running": False,
            "side_effect": False,
            "test_double": True,
            "github_proof": False,
            "macci_proof": False,
            "real_merge": False,
            "real_deploy": False,
            "ok": True,
        }


class FixtureDeliveryPort:
    def __init__(self, projects):
        self.projects = projects
        self.calls = []
        self.read_calls = []

    def project(self, principal, *, app_id, task_id, scope):
        del principal, app_id, scope
        self.read_calls.append(task_id)
        return {
            "side_effect": False,
            "runtime_evidence": False,
            "test_double": True,
            "adopted": False,
            "github_proof": False,
            "macci_proof": False,
            "task_id": task_id,
            "projection": self.projects.get(task_id),
        }

    def coordinate(self, principal, *, action, task_id, chain_id, idempotency_key, expected_revision, scope):
        del principal, task_id, chain_id, idempotency_key, expected_revision, scope
        self.calls.append(f"coordinate:{action}")
        return {
            "http": 200,
            "code": None,
            "ok": True,
            "state": "UNCHANGED",
            "performed": False,
            "real_merge": False,
            "real_deploy": False,
            "changed": False,
            "running": False,
            "side_effect": False,
            "test_double": True,
            "github_proof": False,
            "macci_proof": False,
        }

    def request_publish(self, principal, *, task_id, chain_id, idempotency_key, expected_revision, scope):
        del principal, task_id, chain_id, idempotency_key, expected_revision, scope
        self.calls.append("request_publish")
        return {
            "http": 403,
            "code": "POLICY_DENIED",
            "allow": False,
            "performed": False,
            "changed": False,
            "side_effect": False,
            "test_double": True,
            "github_proof": False,
            "macci_proof": False,
            "real_merge": False,
            "real_deploy": False,
        }


class FixtureDiagnosticPort:
    def __init__(self, items):
        self.items = list(items)
        self.calls = []

    def poll(self, principal, *, app_id, subject=None, after_seq=0):
        del principal, subject, after_seq
        self.calls.append("poll")
        matched = []
        for item in self.items:
            if item.get("app_id") not in (None, app_id):
                continue
            matched.append(dict(item))
        return {
            "side_effect": False,
            "items": matched,
            "gap": False,
            "watermark_seq": len(matched),
            "test_double": True,
            "runtime_evidence": False,
            "github_proof": False,
            "macci_proof": False,
        }
