"""Ports for W01 events, W02 authentication, W04 scheduling, and W07 deploy.

This isolated tree does not import those packages. ``Blocked*`` ports report
INTERFACE_PENDING. ``Fixture*`` ports are test doubles: they are not GitHub
and they are not MacCI receipts.
"""

from __future__ import annotations


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


class BlockedEvents:
    """W01 is not in this tree."""

    is_test_double = False

    def append(self, event):
        return _blocked("event.append")

    def query(self, *, stream_id, stream_epoch, after_seq=0, limit=100):
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

    def authenticate(self, actor):
        self.calls.append(("authenticate", (actor or {}).get("id")))
        if not self.authenticated:
            return {"ok": False, "blocked": False, "code": "NOT_AUTHORIZED"}
        return {
            "ok": True,
            "blocked": False,
            "principal_id": (actor or {}).get("id"),
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


class FixtureEvents:
    """Records event intents. ``applied`` here is not a W01 receipt."""

    is_test_double = True

    def __init__(self):
        self.events = []
        self.fail = False

    def append(self, event):
        if "received_at" in event:
            return {"ok": False, "applied": False, "blocked": False, "code": "SCHEMA_UNSUPPORTED"}
        if self.fail:
            return {"ok": False, "applied": False, "blocked": False, "code": "SIDE_EFFECT_UNKNOWN", "status": "unknown"}
        self.events.append(dict(event))
        return {
            "ok": True,
            "applied": True,
            "blocked": False,
            "fact_state": "ACCEPTED",
            "delivery_state": "DURABLE_LOCAL",
            "test_double": True,
            "w01_proof": False,
        }

    def query(self, *, stream_id, stream_epoch, after_seq=0, limit=100):
        return {"ok": True, "blocked": False, "items": [], "side_effect": False, "test_double": True}


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
        if not found:
            return {
                "ok": True,
                "blocked": False,
                "classification": "unknown",
                "operation_id": operation_id,
                "test_double": True,
                "github_proof": False,
            }
        body = dict(found)
        body.setdefault("operation_id", operation_id)
        body.update({"ok": True, "blocked": False, "test_double": True, "github_proof": False})
        return body

    def observe_pr(self, number):
        return {"ok": True, "blocked": False, "number": number, "test_double": True, "github_proof": False}
