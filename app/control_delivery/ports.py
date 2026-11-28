"""Ports for W01 events, W02 authentication, W04 scheduling, and W07 deploy.

This isolated tree does not import those packages. ``Blocked*`` ports report
INTERFACE_PENDING. ``Fixture*`` ports are test doubles: they are not GitHub
and they are not MacCI receipts.
"""

from __future__ import annotations

import hashlib
import json


def event_identity_digest(event):
    material = {
        key: event.get(key)
        for key in (
            "event_id",
            "event_type",
            "repo_id",
            "stream_id",
            "stream_epoch",
            "producer_seq",
            "idempotency_key",
            "state",
            "operation",
        )
    }
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def event_payload_digest(event):
    """Hash the complete staged event, including scope and business intent."""
    raw = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _blocked(operation):
    return {
        "ok": False,
        "blocked": True,
        "pending": True,
        "applied": False,
        "allow": False,
        "code": "INTERFACE_PENDING",
        "status": "BLOCKED",
        "operation": operation,
        "github_proof": False,
        "macci_proof": False,
        "test_double": False,
    }


class BlockedAuth:
    """W02 is not in this tree."""

    is_test_double = False
    github_proof = False

    def authenticate(self, actor):
        return _blocked("authenticate")

    def authorize(self, actor, scope, operation, candidate_id):
        return _blocked("authorize")

    def preauthorization(self, scope, operation, candidate_id):
        return _blocked("preauthorization")

    def acceptance(self, principal_id, binding):
        return _blocked("acceptance")


class BlockedEvents:
    """W01 is not in this tree."""

    is_test_double = False

    def append(self, event):
        return _blocked("event.append")

    def query(self, *, stream_id, stream_epoch, after_seq=0, limit=100, event_id=None, **_extra):
        return _blocked("event.query")


class BlockedSchedule:
    """W04 is not in this tree."""

    is_test_double = False

    def reserve(self, spec):
        return _blocked("schedule.reserve")

    def freeze(self, candidate_id):
        return _blocked("schedule.freeze")

    def evidence(self, candidate_id):
        return _blocked("schedule.evidence")


class BlockedDeploy:
    """W07 is not in this tree. This port never switches a service."""

    is_test_double = False
    real_host = False

    def plan(self, spec):
        return _blocked("deploy.plan")

    def observe(self, candidate_id):
        return _blocked("deploy.observe")


class BlockedRemote:
    """No live GitHub client lives in this package."""

    is_test_double = False
    github_proof = False

    def lookup_issue(self, feedback_id):
        return _blocked("remote.lookup")

    def create_issue(self, payload):
        return _blocked("remote.create")

    def list_comments(self, issue_number, *, page, per_page):
        return _blocked("remote.comments")

    def reconcile(self, operation_id):
        return _blocked("remote.reconcile")

    def observe_pr(self, number):
        return _blocked("remote.pr")


class ServerClock:
    def __init__(self, start=1700000000):
        self.now = int(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += int(seconds)


class FixtureAuth:
    """In-memory authorization double. Not a signature oracle."""

    is_test_double = True
    github_proof = False
    macci_proof = False

    def __init__(self):
        self.calls = []
        self.authenticated = True
        self.denied = set()
        self.preauth = {}
        self.allow_publish = False
        self.acceptances = {}
        self.bound_principal = None

    def authenticate(self, actor):
        self.calls.append(("authenticate", (actor or {}).get("id")))
        if not self.authenticated:
            return {"ok": False, "blocked": False, "code": "NOT_AUTHORIZED"}
        principal = self.bound_principal
        if not isinstance(principal, str) or not principal:
            principal = (actor or {}).get("id")
        return {
            "ok": True,
            "blocked": False,
            "principal_id": principal,
            "test_double": True,
        }

    def authorize(self, actor, scope, operation, candidate_id):
        self.calls.append(("authorize", operation))
        if operation == "release.publish" and not self.allow_publish:
            return {"ok": False, "allow": False, "blocked": False, "code": "POLICY_DENIED"}
        if operation in self.denied:
            return {"ok": False, "allow": False, "blocked": False, "code": "POLICY_DENIED"}
        return {"ok": True, "allow": True, "blocked": False, "code": None, "test_double": True}

    def preauthorization(self, scope, operation, candidate_id):
        self.calls.append(("preauthorization", operation, candidate_id))
        grant = self.preauth.get((operation, candidate_id))
        if not grant:
            return {"ok": True, "allow": False, "blocked": False, "test_double": True}
        return {"ok": True, "allow": True, "blocked": False, "test_double": True, "grant": grant}

    def acceptance(self, principal_id, binding):
        self.calls.append(("acceptance", (binding or {}).get("candidate_id")))
        grant = self.acceptances.get((binding or {}).get("candidate_id"))
        if not grant:
            return {
                "ok": True,
                "allow": False,
                "blocked": False,
                "test_double": True,
                "code": "EVIDENCE_MISSING",
            }
        keys = ("repo", "task_id", "chain_id", "candidate_id", "head", "base", "merge", "tree", "decision")
        if grant.get("principal_id") != principal_id or any(grant.get(key) != (binding or {}).get(key) for key in keys):
            return {
                "ok": True,
                "allow": False,
                "blocked": False,
                "test_double": True,
                "reason": "tuple_mismatch",
            }
        body = {
            "ok": True,
            "allow": True,
            "blocked": False,
            "test_double": True,
            "decision": grant["decision"],
            "signer_kind": "user_acceptance",
            "runtime_acceptance": False,
            "github_proof": False,
            "macci_proof": False,
            "counts_as_runtime_user_acceptance": False,
        }
        for key in keys:
            body[key] = grant[key]
        return body


def _absence_proof(event):
    return {
        "accepted": False,
        "trusted": True,
        "authority": "W01",
        "author": "w01",
        "event_id": event.get("event_id"),
        "producer_seq": event.get("producer_seq"),
        "stream_id": event.get("stream_id"),
        "stream_epoch": event.get("stream_epoch"),
        "payload_digest": event_identity_digest(event),
        "event_digest": event_payload_digest(event),
        "test_double": True,
        "w01_proof": False,
    }


class FixtureEvents:
    """Records event intents. ``applied`` here is not a W01 receipt."""

    is_test_double = True

    def __init__(self):
        self.events = []
        self.by_id = {}
        self.fail = False

    def _receipt(self, event):
        return {
            "ok": True,
            "applied": True,
            "blocked": False,
            "fact_state": "ACCEPTED",
            "delivery_state": "DURABLE_LOCAL",
            "test_double": True,
            "w01_proof": False,
            "authority": "W01",
            "receipt": {
                "authority": "W01",
                "event_id": event["event_id"],
                "stream_id": event["stream_id"],
                "stream_epoch": event["stream_epoch"],
                "producer_seq": event["producer_seq"],
                "fact_state": "ACCEPTED",
                "event_digest": event_payload_digest(event),
                "test_double": True,
                "w01_proof": False,
            },
        }

    def append(self, event):
        if "received_at" in event:
            return {"ok": False, "applied": False, "blocked": False, "code": "SCHEMA_UNSUPPORTED"}
        if self.fail:
            return {
                "ok": False,
                "applied": False,
                "blocked": False,
                "code": "SIDE_EFFECT_UNKNOWN",
                "status": "unknown",
                "classification": "unknown",
            }
        event_id = event.get("event_id")
        if event_id in self.by_id:
            stored = self.by_id[event_id]
            if event_payload_digest(stored) != event_payload_digest(event):
                return {"ok": False, "applied": False, "blocked": False, "code": "IDEMPOTENCY_PAYLOAD_MISMATCH"}
            return self._receipt(stored)
        stored = dict(event)
        self.by_id[event_id] = stored
        self.events.append(stored)
        return self._receipt(stored)

    def query(
        self,
        *,
        stream_id,
        stream_epoch,
        after_seq=0,
        limit=100,
        event_id=None,
        producer_seq=None,
        repo_id=None,
        event_type=None,
        idempotency_key=None,
        state=None,
        operation=None,
        expected_event=None,
    ):
        if self.fail:
            return {
                "ok": False,
                "blocked": False,
                "classification": "unknown",
                "items": [],
                "test_double": True,
                "w01_proof": False,
            }
        if event_id is not None:
            found = self.by_id.get(event_id)
            if found is not None and (found.get("stream_id") != stream_id or found.get("stream_epoch") != stream_epoch):
                return {"ok": False, "blocked": False, "classification": "unknown", "items": []}
            if found is None:
                proof_event = {
                    "event_id": event_id,
                    "stream_id": stream_id,
                    "stream_epoch": stream_epoch,
                    "producer_seq": producer_seq,
                    "repo_id": repo_id,
                    "event_type": event_type,
                    "idempotency_key": idempotency_key,
                    "state": state,
                    "operation": operation,
                }
                return {
                    "ok": True,
                    "blocked": False,
                    "classification": "absent",
                    "event_id": event_id,
                    "items": [],
                    "side_effect": False,
                    "test_double": True,
                    "w01_proof": False,
                    "absence_proof": _absence_proof(expected_event if isinstance(expected_event, dict)
                                                      and event_identity_digest(expected_event) == event_identity_digest(proof_event)
                                                      else proof_event),
                }
            return {
                "ok": True,
                "blocked": False,
                "classification": "present",
                "event_id": event_id,
                "items": [dict(found)],
                "side_effect": False,
                "test_double": True,
                "w01_proof": False,
                "authority": "W01",
            }
        items = [
            dict(event)
            for event in self.events
            if event.get("stream_id") == stream_id
            and event.get("stream_epoch") == stream_epoch
            and int(event.get("producer_seq") or 0) > after_seq
        ]
        return {
            "ok": True,
            "blocked": False,
            "classification": "present" if items else "absent",
            "items": items[:limit],
            "side_effect": False,
            "test_double": True,
            "w01_proof": False,
        }


class FixtureSchedule:
    is_test_double = True
    github_proof = False

    def __init__(self):
        self.reservations = []
        self.freeze_state = {
            "draft": False,
            "equal_tree": False,
            "official_merge_observed": False,
            "blocked": False,
        }
        self.evidence_state = {}

    def reserve(self, spec):
        self.reservations.append(dict(spec))
        return {
            "ok": True,
            "blocked": False,
            "reserved": True,
            "performed": False,
            "real_merge": False,
            "test_double": True,
        }

    def freeze(self, candidate_id):
        state = dict(self.freeze_state)
        state["candidate_id"] = candidate_id
        state["test_double"] = True
        state.setdefault("blocked", False)
        return state

    def evidence(self, candidate_id):
        found = self.evidence_state.get(candidate_id)
        if found is None:
            return {"ok": True, "blocked": False, "known": False, "test_double": True}
        body = dict(found)
        body.update({"ok": True, "blocked": False, "known": True, "test_double": True, "macci_proof": False})
        return body


class FixtureDeploy:
    """Plan and observe only. ``real_host`` stays false."""

    is_test_double = True
    real_host = False

    def __init__(self):
        self.plans = []
        self.observations = {}

    def plan(self, spec):
        self.plans.append(dict(spec))
        return {
            "ok": True,
            "blocked": False,
            "state": "PLAN",
            "performed": False,
            "real_deploy": False,
            "real_host": False,
            "test_double": True,
        }

    def observe(self, candidate_id):
        found = self.observations.get(candidate_id)
        if found is None:
            return {"ok": True, "blocked": False, "status": "unknown", "test_double": True, "real_host": False}
        body = dict(found)
        body.setdefault("blocked", False)
        body.setdefault("real_host", False)
        body["test_double"] = True
        return body


class FixtureRemote:
    """Scripted issue and comment double. Not a GitHub client."""

    is_test_double = True
    github_proof = False

    def __init__(self):
        self.calls = []
        self.issues = {}
        self.comments = {}
        self.scripts = {}
        self.reconcile_result = {}
        self._next = 1
        self.fail_create = set()
        self.unknown_create = set()
        self.unknown_ops = set()
        self.applied = {}

    def lookup_issue(self, feedback_id):
        self.calls.append(("lookup", feedback_id))
        found = self.issues.get(feedback_id)
        if found is None:
            return {"ok": True, "blocked": False, "found": False, "test_double": True, "github_proof": False}
        body = dict(found)
        body.update({"ok": True, "blocked": False, "found": True, "test_double": True, "github_proof": False})
        return body

    def create_issue(self, payload):
        feedback_id = payload["feedback_id"]
        self.calls.append(("create", feedback_id))
        if feedback_id in self.unknown_create:
            self.unknown_ops.add(payload.get("operation_id"))
            return {
                "ok": False,
                "blocked": False,
                "status": "unknown",
                "operation_id": payload["operation_id"],
                "test_double": True,
                "github_proof": False,
            }
        if feedback_id in self.fail_create:
            return {
                "ok": False,
                "blocked": False,
                "status": "failed",
                "test_double": True,
                "github_proof": False,
            }
        if feedback_id in self.issues:
            body = dict(self.issues[feedback_id])
            body.update({"ok": True, "status": "exists", "test_double": True, "github_proof": False})
            return body
        number = self._next
        self._next += 1
        record = {
            "number": number,
            "author_id": payload["author_id"],
            "body": payload["body"],
            "feedback_id": feedback_id,
        }
        self.issues[feedback_id] = record
        operation_id = payload.get("operation_id")
        if operation_id:
            self.applied[operation_id] = {
                "classification": "succeeded",
                "operation_id": operation_id,
                "issue_number": number,
                "author_id": payload["author_id"],
                "feedback_id": feedback_id,
                "repo": (payload.get("scope") or {}).get("repo"),
                "scope": payload.get("scope"),
                "request_digest": payload.get("request_digest"),
            }
        body = dict(record)
        body.update({"ok": True, "blocked": False, "status": "created", "test_double": True, "github_proof": False})
        return body

    def list_comments(self, issue_number, *, page, per_page):
        self.calls.append(("comments", issue_number, page))
        rows = self.comments.get(issue_number, [])
        start = (page - 1) * per_page
        chunk = rows[start:start + per_page]
        return {
            "ok": True,
            "blocked": False,
            "items": [dict(item) for item in chunk],
            "has_more": start + per_page < len(rows),
            "page": page,
            "test_double": True,
            "github_proof": False,
        }

    def reconcile(self, operation_id):
        self.calls.append(("reconcile", operation_id))
        found = self.reconcile_result.get(operation_id)
        if found:
            body = dict(found)
            body.setdefault("operation_id", operation_id)
            body.update({"ok": True, "blocked": False, "test_double": True, "github_proof": False})
            return body
        if operation_id in self.unknown_ops:
            return {
                "ok": True,
                "blocked": False,
                "classification": "unknown",
                "operation_id": operation_id,
                "test_double": True,
                "github_proof": False,
            }
        applied = self.applied.get(operation_id)
        if applied:
            body = dict(applied)
            body.update({"ok": True, "blocked": False, "test_double": True, "github_proof": False})
            return body
        return {
            "ok": True,
            "blocked": False,
            "classification": "absent",
            "operation_id": operation_id,
            "test_double": True,
            "github_proof": False,
        }

    def observe_pr(self, number):
        return {"ok": True, "blocked": False, "number": number, "test_double": True, "github_proof": False}
