"""Closed ports for the isolated W07 driver.

These objects do not switch a host service. ``real_host`` stays false.
"""

from __future__ import annotations

import json

from app.control_deploy.contract_machine import digest_of
from app.control_deploy.ports import Effect, EventRejected, NotOccurred, fact_digest

OLD = "1" * 40
OLD_BYTES = b"previous-artifact-v1"
TOKEN = "isolated-token-not-a-lease"


class ClosedLease:
    def __init__(self):
        self.bindings = {("run-isolated", TOKEN, 1)}
        self.principal = "isolated"
        self.scope = "deploy"
        self.expires_at = 10_000
        self.now = 100

    def server_now(self):
        return self.now

    def check(self, *, token, principal_id, run_id, scope, generation, server_now, candidate_id=None, service_id=None, artifact_digest=None, operation_id=None):
        del candidate_id, service_id, artifact_digest, operation_id
        scope_ok = scope == self.scope or (isinstance(scope, str) and scope.startswith(self.scope + ":"))
        if server_now >= self.expires_at or principal_id != self.principal or not scope_ok:
            return False
        return (run_id, token, generation) in self.bindings


class ClosedAuth:
    def __init__(self):
        self.server_principal = "isolated"

    def authenticate(self, actor):
        del actor
        return True

    def principal(self, actor):
        del actor
        return self.server_principal

    def authorize(self, actor, scope, operation, candidate_id):
        del actor, scope, operation, candidate_id
        return True

    def high_risk_allowed(self, actor, operation):
        del actor, operation
        return False

    def recovery_authorized(self, actor, deployment_id):
        del actor, deployment_id
        return True

    def resume_authorized(self, actor, app_id, deployment_id):
        del actor, app_id, deployment_id
        return False


class ClosedEvents:
    def __init__(self):
        self.facts = []
        self.watermark = {}
        self._bodies = {}
        self._receipts = {}

    def _body(self, fact):
        body = {key: value for key, value in fact.items() if key != "received_at"}
        return json.dumps(body, sort_keys=True, default=str)

    def _receipt(self, fact):
        return {
            "event_id": fact["event_id"],
            "stream_id": fact["stream_id"],
            "stream_epoch": fact["stream_epoch"],
            "producer_seq": fact["producer_seq"],
            "received_at": "server",
            "fact_state": "ACCEPTED",
        }

    def append_deployment(self, fact):
        if "received_at" in fact:
            raise ValueError("caller supplied received_at")
        required = (
            "event_id", "schema_version", "event_type", "event_class", "repo_id", "producer_id",
            "stream_id", "stream_epoch", "producer_seq", "occurred_at", "idempotency_key",
        )
        missing = [name for name in required if fact.get(name) in (None, "")]
        if missing:
            raise EventRejected("missing:" + ",".join(missing))
        prior = self._bodies.get(fact["event_id"]) or self._bodies.get(fact["idempotency_key"])
        if prior is not None:
            if self._body(prior) != self._body(fact):
                raise EventRejected("payload_mismatch")
            return dict(self._receipts[prior["event_id"]])
        key = (fact["stream_id"], fact["stream_epoch"])
        expected = self.watermark.get(key, 0) + 1
        if fact["producer_seq"] != expected:
            raise EventRejected("sequence")
        stored = dict(fact)
        stored["received_at"] = "server"
        self.facts.append(stored)
        self._bodies[fact["event_id"]] = dict(fact)
        self._bodies[fact["idempotency_key"]] = dict(fact)
        receipt = self._receipt(fact)
        self._receipts[fact["event_id"]] = receipt
        self.watermark[key] = fact["producer_seq"]
        return dict(receipt)

    def reconcile_deployment(self, fact):
        if not isinstance(fact, dict):
            return {"outcome": "unknown"}
        names = (
            "event_id", "producer_id", "stream_id", "stream_epoch", "producer_seq",
            "idempotency_key", "app_id", "deployment_id",
        )
        digest = fact_digest(fact)
        prior = self._bodies.get(fact.get("event_id"))
        if prior is None and fact.get("idempotency_key"):
            prior = self._bodies.get(fact.get("idempotency_key"))
        if prior is not None:
            if fact_digest(prior) != digest:
                return {"outcome": "unknown"}
            proof = {name: prior.get(name) for name in names}
            proof["outcome"] = "accepted"
            proof["payload_digest"] = fact_digest(prior)
            proof["idempotent"] = True
            proof["receipt"] = dict(self._receipts[prior["event_id"]])
            return proof
        proof = {name: fact.get(name) for name in names}
        proof["outcome"] = "not_accepted"
        proof["payload_digest"] = digest
        proof["idempotent"] = True
        return proof


class ClosedService:
    real_host = False

    def __init__(self):
        self.calls = []
        self.queue = {}
        self.operations = {}
        self.running = {
            "ok": True,
            "running_sha": OLD,
            "service_id": "web",
            "artifact_digest": digest_of(OLD_BYTES),
        }

    def _remember(self, spec, effect):
        operation_id = spec.get("operation_id") if isinstance(spec, dict) else None
        if operation_id:
            self.operations.setdefault(operation_id, effect)
        return effect

    def operation_result(self, operation_id):
        if operation_id not in self.operations:
            return NotOccurred(operation_id)
        found = self.operations[operation_id]
        if not isinstance(found, Effect):
            return None
        return found

    def _next(self, name, default):
        self.calls.append(name)
        pending = self.queue.get(name) or []
        if pending:
            return pending.pop(0)
        return default()

    def stage(self, spec):
        effect = self._next("stage", lambda: Effect("ok"))
        return self._remember(spec, effect)

    def verify(self, spec):
        effect = self._next("verify", lambda: Effect("ok"))
        return self._remember(spec, effect)

    def switch(self, spec):
        effect = self._next("switch", lambda: Effect("ok"))
        if effect.status == "ok":
            self.running = {
                "ok": True,
                "running_sha": spec["head"],
                "service_id": spec["service_id"],
                "artifact_digest": spec["artifact_digest"],
            }
        return self._remember(spec, effect)

    def health(self, spec):
        def default():
            local = dict(self.running)
            local.setdefault("ok", True)
            return Effect("ok", local=local, public={"ok": False, "running_sha": None})

        return self._remember(spec, self._next("health", default))

    def commit(self, spec):
        return self._remember(spec, self._next("commit", lambda: Effect("ok")))

    def rollback(self, spec):
        effect = self._next("rollback", lambda: Effect("ok", resources_removed=False))
        if effect.status == "ok" and spec.get("install_kind") != "first_install":
            self.running = {
                "ok": True,
                "running_sha": spec.get("previous_healthy_sha"),
                "service_id": spec.get("service_id"),
                "artifact_digest": spec.get("previous_artifact_digest"),
            }
        return self._remember(spec, effect)

    def observe(self, spec):
        def default():
            local = dict(self.running)
            local.setdefault("ok", True)
            return Effect("ok", local=local, public={"ok": False})

        return self._next("observe", default)
