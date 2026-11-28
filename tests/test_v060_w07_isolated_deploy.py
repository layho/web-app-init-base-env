"""Isolated W07 deploy transaction and recovery.

These tests use a temporary directory and a synthetic service. They are not
MacCI, not a host deploy, and not production acceptance.
"""

import ast
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from app.control_deploy import (
    MANAGES_REAL_DEPLOY_ENTRY,
    PRODUCTION_ENABLEMENT,
    REAL_SERVICE_SWITCH,
    DeployEngine,
    Interrupted,
)
from app.control_deploy.contract_machine import (
    ContractError,
    allows,
    candidate_id,
    digest_of,
    load_contract,
)
from app.control_deploy.engine import Request
from app.control_deploy.journal import JournalCorrupt
from app.control_deploy.ports import Effect, EventRejected, NotOccurred, fact_digest


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "tests" / "fixtures" / "v060" / "w00" / "contract.json"
CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
EDGES = CONTRACT["entities"]["deployment"]["transitions"]
SPEC = CONTRACT["entities"]["candidate"]["id"]
PACKAGE = ROOT / "app" / "control_deploy"

OLD = "1" * 40
HEAD = "2" * 40
BASE = "3" * 40
MERGE = "4" * 40
TREE = "5" * 40
OLD_BYTES = b"previous-artifact-v1"
NEW_BYTES = b"candidate-artifact-v2"
TOKEN = "opaque-token-not-the-generation"


def cid(candidate):
    return candidate_id(
        SPEC,
        candidate["repo_id"],
        candidate["task_id"],
        candidate["head"],
        candidate["base"],
        candidate["merge"],
        candidate["tree"],
    )


def candidate(head=HEAD, task="tsk_w07", tree=TREE):
    return {
        "repo_id": "layho/web-app-init-base-env",
        "task_id": task,
        "head": head,
        "base": BASE,
        "merge": MERGE,
        "tree": tree,
    }


def make_request(**overrides):
    cand = overrides.pop("candidate", None) or candidate()
    if "artifact_bytes" in overrides:
        body = overrides.pop("artifact_bytes")
    else:
        body = NEW_BYTES
    digest = overrides.pop("artifact_digest", None)
    if digest is None and body is not None:
        digest = digest_of(body)
    if "previous_artifact_bytes" in overrides:
        previous_body = overrides.pop("previous_artifact_bytes")
    else:
        previous_body = OLD_BYTES
    previous_digest = overrides.pop("previous_artifact_digest", None)
    if previous_digest is None and previous_body is not None:
        previous_digest = digest_of(previous_body)
    request = Request(
        actor={"kind": "test", "id": "fixture", "proof_ref": "test-proof://w07/fixture"},
        principal_id="fixture",
        scope="deploy",
        expected_revision=overrides.pop("expected_revision", 0),
        idempotency_key=overrides.pop("idempotency_key", "idem-w07-plan"),
        run_id=overrides.pop("run_id", "run-1"),
        lease_token=overrides.pop("lease_token", TOKEN),
        lease_generation=overrides.pop("lease_generation", 1),
        server_now=overrides.pop("server_now", 100),
        candidate=cand,
        candidate_id=overrides.pop("candidate_id", None) or cid(cand),
        artifact_digest=digest,
        artifact_bytes=body,
        service_id=overrides.pop("service_id", "web"),
        previous_healthy_sha=overrides.pop("previous_healthy_sha", OLD),
        previous_artifact_digest=previous_digest,
        previous_artifact_bytes=previous_body,
        running_sha=overrides.pop("running_sha", OLD),
        deployment_id=overrides.pop("deployment_id", "dep_w07"),
    )
    if overrides:
        request = replace(request, **overrides)
    return request


class MemoryLease:
    def __init__(self):
        self.bindings = {("run-1", TOKEN, 1)}
        self.principal = "fixture"
        self.scope = "deploy"
        self.expires_at = 10_000
        self.now = 100
        self.force_allow = False
        self.checks = []

    def allow(self, run_id, token=TOKEN, generation=1):
        self.bindings.add((run_id, token, generation))

    def server_now(self):
        return self.now

    def check(
        self,
        *,
        token,
        principal_id,
        run_id,
        scope,
        generation,
        server_now,
        candidate_id=None,
        service_id=None,
        artifact_digest=None,
        operation_id=None,
    ):
        self.checks.append(
            {
                "principal_id": principal_id,
                "scope": scope,
                "server_now": server_now,
                "run_id": run_id,
                "candidate_id": candidate_id,
                "service_id": service_id,
                "artifact_digest": artifact_digest,
                "operation_id": operation_id,
            }
        )
        if self.force_allow:
            return True
        scope_ok = scope == self.scope or (isinstance(scope, str) and scope.startswith(self.scope + ":"))
        if server_now >= self.expires_at or principal_id != self.principal or not scope_ok:
            return False
        return (run_id, token, generation) in self.bindings


class MemoryAuth:
    def __init__(self):
        self.calls = []
        self.authenticated = True
        self.allow = True
        self.high_risk = False
        self.recovery = True
        self.server_principal = "fixture"
        self.resume_scopes = "all"

    def authenticate(self, actor):
        self.calls.append("authenticate")
        return self.authenticated

    def principal(self, actor):
        if not self.authenticated:
            return None
        return self.server_principal

    def authorize(self, actor, scope, operation, candidate_id):
        self.calls.append("authorize")
        return self.allow

    def high_risk_allowed(self, actor, operation):
        self.calls.append("high_risk")
        return self.high_risk

    def recovery_authorized(self, actor, deployment_id):
        self.calls.append(("recovery", deployment_id))
        return self.recovery

    def resume_authorized(self, actor, app_id, deployment_id):
        self.calls.append(("resume", app_id, deployment_id))
        if self.resume_scopes == "all":
            return True
        if not self.resume_scopes:
            return False
        return (app_id, deployment_id) in self.resume_scopes


class MemoryEvents:
    """In-test W01 port. Accepted facts live only here; the journal is the outbox."""

    def __init__(self):
        self.facts = []
        self.fail = False
        self.reject_reason = None
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
            "event_id",
            "schema_version",
            "event_type",
            "event_class",
            "repo_id",
            "producer_id",
            "stream_id",
            "stream_epoch",
            "producer_seq",
            "occurred_at",
            "idempotency_key",
        )
        missing = [name for name in required if fact.get(name) in (None, "")]
        if missing:
            raise EventRejected("missing:" + ",".join(missing))
        if self.reject_reason:
            raise EventRejected(self.reject_reason)
        if self.fail:
            raise RuntimeError("delivery down")
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
            "event_id",
            "producer_id",
            "stream_id",
            "stream_epoch",
            "producer_seq",
            "idempotency_key",
            "app_id",
            "deployment_id",
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
        if self.fail:
            return {"outcome": "unknown"}
        proof = {name: fact.get(name) for name in names}
        proof["outcome"] = "not_accepted"
        proof["payload_digest"] = digest
        proof["idempotent"] = True
        return proof


class ScriptedService:
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
        self._remember(spec, effect)
        return effect

    def verify(self, spec):
        effect = self._next("verify", lambda: Effect("ok"))
        self._remember(spec, effect)
        return effect

    def switch(self, spec):
        effect = self._next("switch", lambda: Effect("ok"))
        self._remember(spec, effect)
        if effect.status == "ok":
            self.running = {
                "ok": True,
                "running_sha": spec["head"],
                "service_id": spec["service_id"],
                "artifact_digest": spec["artifact_digest"],
            }
        return effect

    def health(self, spec):
        def default():
            local = dict(self.running)
            local.setdefault("ok", True)
            return Effect("ok", local=local, public={"ok": False, "running_sha": None})

        effect = self._next("health", default)
        self._remember(spec, effect)
        return effect

    def commit(self, spec):
        effect = self._next("commit", lambda: Effect("ok"))
        self._remember(spec, effect)
        return effect

    def rollback(self, spec):
        effect = self._next("rollback", lambda: Effect("ok", resources_removed=False))
        self._remember(spec, effect)
        if effect.status == "ok" and spec.get("install_kind") != "first_install":
            self.running = {
                "ok": True,
                "running_sha": spec.get("previous_healthy_sha"),
                "service_id": spec.get("service_id"),
                "artifact_digest": spec.get("previous_artifact_digest"),
            }
        return effect

    def observe(self, spec):
        def default():
            local = dict(self.running)
            local.setdefault("ok", True)
            return Effect("ok", local=local, public={"ok": False})

        return self._next("observe", default)


class Session:
    def __init__(self, root, service, lease, auth, events, request=None):
        self.root = root
        self.service = service
        self.lease = lease
        self.auth = auth
        self.events = events
        self.engine = DeployEngine(
            root,
            service=service,
            lease=lease,
            auth=auth,
            events=events,
            contract_path=CONTRACT_PATH,
        )
        self.request = request or make_request()
        self.revision = 0
        self.fence = None
        self.deployment_id = self.request.deployment_id

    def reopen(self):
        self.engine.close()
        self.engine = DeployEngine(
            self.root,
            service=self.service,
            lease=self.lease,
            auth=self.auth,
            events=self.events,
            contract_path=CONTRACT_PATH,
        )
        return self.engine

    def call(self, method, key, **overrides):
        fields = {
            "idempotency_key": key,
            "expected_revision": self.revision,
            "fence_generation": self.fence,
            "deployment_id": self.deployment_id,
        }
        fields.update(overrides)
        request = replace(self.request, **fields)
        result = getattr(self.engine, method)(request)
        if result.deployment_id:
            self.deployment_id = result.deployment_id
        if result.revision is not None:
            self.revision = result.revision
            self.fence = result.fence_generation
        return result

    def close(self):
        self.engine.close()


def open_session(test, **request_overrides):
    directory = tempfile.TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    service = ScriptedService()
    lease = MemoryLease()
    auth = MemoryAuth()
    events = MemoryEvents()
    session = Session(
        Path(directory.name),
        service,
        lease,
        auth,
        events,
        make_request(**request_overrides),
    )
    test.addCleanup(session.close)
    return session


def assert_legal(test, engine, deployment_id):
    seen = engine.transitions(deployment_id)
    test.assertTrue(seen)
    for edge in seen:
        test.assertTrue(allows(EDGES, edge["from"], edge["to"], edge["guard"]), edge)


class IsolatedDeployTransactionTest(unittest.TestCase):
    def test_candidate_encoding_matches_w00_vector(self):
        vector = SPEC["vector"]
        self.assertEqual(
            candidate_id(
                SPEC,
                vector["repo_id"],
                vector["task_id"],
                vector["head"],
                vector["base"],
                vector["merge"],
                vector["tree"],
            ),
            vector["candidate_id"],
        )
        contract, edges, _spec = load_contract(CONTRACT_PATH)
        self.assertFalse(contract["identity"]["production_verification_enabled"])
        self.assertFalse(any(edge["from"] == "SAFE_FROZEN" for edge in edges))

    def test_production_stays_disabled_and_real_entry_is_not_imported(self):
        self.assertFalse(PRODUCTION_ENABLEMENT)
        self.assertFalse(REAL_SERVICE_SWITCH)
        self.assertFalse(MANAGES_REAL_DEPLOY_ENTRY)
        banned = {
            "app.main",
            "app.operations",
            "app.db",
            "app.integration_queue",
            "deploy.macci.runner",
            "deploy.macci.preview_journal",
            "deploy.macci.merge_preview",
            "app.control_identity",
            "app.control_events",
            "app.control_authz",
            "app.control_scheduler",
            "subprocess",
            "sqlalchemy",
        }
        for path in PACKAGE.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                self.assertFalse(banned.intersection(names), path.name)
        flipped = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        flipped["identity"]["production_verification_enabled"] = True
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "contract.json"
            target.write_text(json.dumps(flipped), encoding="utf-8")
            with self.assertRaises(ContractError):
                load_contract(target)

    def test_r01_plan_records_transaction_and_happy_path_reaches_commit(self):
        session = open_session(self)
        plan = session.call("plan", "idem-w07-plan")
        self.assertEqual(plan.http, 200)
        self.assertEqual(plan.state, "PLAN")
        self.assertFalse(plan.details["production_enablement"])
        for method, key, state in (
            ("apply", "idem-w07-stage", "STAGE"),
            ("apply", "idem-w07-verify", "VERIFY"),
            ("apply", "idem-w07-switch", "SWITCH"),
            ("apply", "idem-w07-health", "HEALTH"),
            ("apply", "idem-w07-pending", "COMMIT_PENDING"),
            ("apply", "idem-w07-commit", "COMMIT"),
        ):
            result = session.call(method, key)
            self.assertEqual(result.state, state, key)
            self.assertFalse(result.details["production_enablement"])
        self.assertEqual(
            session.engine.transitions(session.deployment_id),
            [
                {"from": "PLAN", "to": "STAGE", "guard": None},
                {"from": "STAGE", "to": "VERIFY", "guard": None},
                {"from": "VERIFY", "to": "SWITCH", "guard": None},
                {"from": "SWITCH", "to": "HEALTH", "guard": None},
                {"from": "HEALTH", "to": "COMMIT_PENDING", "guard": "health_ok"},
                {"from": "COMMIT_PENDING", "to": "COMMIT", "guard": "commit_ack_durable"},
            ],
        )
        assert_legal(self, session.engine, session.deployment_id)
        stored = session.engine.deployment(session.deployment_id)
        self.assertFalse(stored["schema_executed"])
        self.assertEqual(stored["candidate_id"], session.request.candidate_id)
        self.assertEqual(stored["service_id"], "web")
        self.assertEqual(stored["artifact_digest"], digest_of(NEW_BYTES))
        self.assertTrue(any(fact["event_type"] == "deployment.commit" for fact in session.events.facts))
        self.assertTrue(all("received_at" not in fact or fact["received_at"] == "server" for fact in session.events.facts))

    def test_r01_second_worker_cannot_pass_fence(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        lease = MemoryLease()
        lease.allow("run-a")
        lease.allow("run-b")
        auth = MemoryAuth()
        root = Path(directory.name)
        results = []

        def work(run_id):
            engine = DeployEngine(root, service=ScriptedService(), lease=lease, auth=auth, contract_path=CONTRACT_PATH)
            try:
                results.append(
                    engine.plan(
                        make_request(
                            run_id=run_id,
                            idempotency_key=f"idem-w07-{run_id}",
                            deployment_id=f"dep-{run_id}",
                        )
                    )
                )
            finally:
                engine.close()

        threads = [threading.Thread(target=work, args=(run_id,)) for run_id in ("run-a", "run-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 2)
        states = sorted((result.http, result.state, result.details.get("record")) for result in results)
        self.assertEqual(states[0][0], 200)
        self.assertEqual(states[0][1], "PLAN")
        self.assertEqual(states[1][0], 409)
        self.assertEqual(states[1][2], "update_locked")
        self.assertNotEqual(states[1][1], "COMMIT")

    def test_r01_stale_generation_cannot_commit_and_directory_is_not_ownership(self):
        session = open_session(self)
        plan = session.call("plan", "idem-w07-plan")
        self.assertEqual(plan.state, "PLAN")
        session.engine.close()
        session.lease.allow("run-2")
        session.reopen()
        adopted = session.call("start", "idem-w07-adopt", takeover=True, run_id="run-2")
        self.assertEqual(adopted.state, "PLAN")
        self.assertGreater(adopted.fence_generation, 1)
        stale = session.engine.apply(
            replace(
                session.request,
                idempotency_key="idem-w07-stale",
                expected_revision=adopted.revision,
                fence_generation=1,
                run_id="run-1",
                deployment_id=session.deployment_id,
            )
        )
        self.assertEqual(stale.code, "LEASE_FENCE_REJECTED")
        self.assertEqual(session.engine.deployment(session.deployment_id)["state"], "PLAN")
        self.assertEqual(session.service.calls.count("stage"), 0)

        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        root = Path(other.name)
        key = root / "fence"
        key.mkdir(parents=True)
        (key / "base__host-test__web.json").mkdir()
        engine = DeployEngine(
            root,
            service=ScriptedService(),
            lease=session.lease,
            auth=session.auth,
            contract_path=CONTRACT_PATH,
        )
        self.addCleanup(engine.close)
        blocked = engine.plan(make_request(idempotency_key="idem-w07-legacy", deployment_id="dep_legacy"))
        self.assertEqual(blocked.details["record"], "lock_unproven")
        self.assertTrue((key / "base__host-test__web.json").is_dir())
        self.assertIsNone(engine.deployment("dep_legacy"))

    def test_r02_candidate_artifact_service_binding_exact(self):
        session = open_session(self)
        session.call("plan", "idem-w07-plan")
        other = candidate(task="tsk_other")
        stale = session.call(
            "apply",
            "idem-w07-wrong-candidate",
            candidate=other,
            candidate_id=cid(other),
        )
        self.assertEqual(stale.code, "CANDIDATE_STALE")
        self.assertEqual(stale.details["record"], "candidate_binding")
        self.assertEqual(session.service.calls.count("stage"), 0)
        wrong_service = session.call("apply", "idem-w07-wrong-service", service_id="other")
        self.assertEqual(wrong_service.details["record"], "service_binding")
        wrong_digest = session.call(
            "apply",
            "idem-w07-wrong-artifact",
            artifact_digest=digest_of(b"different-artifact"),
            artifact_bytes=b"different-artifact",
        )
        self.assertEqual(wrong_digest.details["record"], "artifact_binding")
        self.assertEqual(session.engine.deployment(session.deployment_id)["state"], "PLAN")
        moved = session.call("apply", "idem-w07-stage")
        self.assertEqual(moved.state, "STAGE")
        self.assertEqual(
            session.engine.artifacts.read(digest_of(NEW_BYTES)),
            NEW_BYTES,
        )
        binding = json.loads(
            (session.root / "artifacts" / digest_of(NEW_BYTES).split(":", 1)[1]).with_suffix(".bind.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(binding["candidate_id"], session.request.candidate_id)
        self.assertEqual(binding["service_id"], "web")
        with self.assertRaises(Exception):
            session.engine.artifacts.bind(digest_of(NEW_BYTES), cid(other), "web")

    def test_r02_artifact_bytes_are_immutable(self):
        session = open_session(self)
        session.call("plan", "idem-w07-plan")
        staged = session.call("apply", "idem-w07-stage")
        self.assertEqual(staged.state, "STAGE")
        path = session.root / "artifacts" / digest_of(NEW_BYTES).split(":", 1)[1]
        path.write_bytes(b"tampered")
        failed = session.call("apply", "idem-w07-verify")
        self.assertEqual(failed.state, "RECONCILE")
        self.assertEqual(failed.details["record"], "artifact_mismatch")
        self.assertNotIn("verify", session.service.calls)
        self.assertTrue(failed.details["held"])

    def test_r02_no_change_checks_health_before_commit(self):
        cand = candidate(head=OLD)
        session = open_session(
            self,
            candidate=cand,
            artifact_bytes=OLD_BYTES,
            no_change=True,
            running_sha=OLD,
            idempotency_key="idem-w07-same",
        )
        result = session.call("plan", "idem-w07-same")
        self.assertEqual(result.state, "COMMIT")
        self.assertEqual(result.details["update_no_change"], 1)
        notes = [note["name"] for note in session.engine.journal.replay()["notes"]]
        self.assertLess(notes.index("health_check_started"), notes.index("transition"))
        self.assertEqual(
            session.engine.transitions(session.deployment_id),
            [{"from": "PLAN", "to": "COMMIT", "guard": "no_change_and_healthy_and_previous_sha"}],
        )
        assert_legal(self, session.engine, session.deployment_id)

    def test_r02_unhealthy_same_sha_does_not_record_no_change(self):
        cand = candidate(head=OLD)
        session = open_session(
            self,
            candidate=cand,
            artifact_bytes=OLD_BYTES,
            no_change=True,
            running_sha=OLD,
        )
        session.service.queue["health"] = [
            Effect(
                "ok",
                local={
                    "ok": False,
                    "running_sha": OLD,
                    "service_id": "web",
                    "artifact_digest": digest_of(OLD_BYTES),
                },
                public={"ok": True, "running_sha": OLD},
            )
        ]
        result = session.call("plan", "idem-w07-unhealthy")
        self.assertEqual(result.state, "PLAN")
        self.assertEqual(result.details["update_no_change"], 0)
        self.assertFalse(result.details["no_change_recorded"])
        self.assertEqual(session.service.calls.count("health"), 1)
        self.assertEqual(session.engine.transitions(session.deployment_id), [])
        names = [note["name"] for note in session.engine.journal.replay()["notes"]]
        self.assertIn("health_check_started", names)
        self.assertNotIn("update_no_change", names)

    def test_r02_first_install_cannot_take_no_change_and_ids_do_not_cross(self):
        session = open_session(
            self,
            install_kind="first_install",
            previous_healthy_sha=None,
            previous_artifact_bytes=None,
            previous_artifact_digest=None,
            no_change=True,
            running_sha=None,
        )
        result = session.call("plan", "idem-w07-first-nochange")
        self.assertEqual(result.details["record"], "first_install_no_change")
        self.assertEqual(result.details["update_no_change"], 0)
        self.assertEqual(session.engine.journal.replay()["deployments"], {})
        left = candidate(task="tsk_left")
        right = candidate(task="tsk_right")
        self.assertNotEqual(cid(left), cid(right))

    def test_r03_journal_lock_and_inventory_survive_restart(self):
        session = open_session(self)
        session.call("plan", "idem-w07-plan")
        session.call("apply", "idem-w07-stage")
        session.reopen()
        stored = session.engine.deployment(session.deployment_id)
        self.assertEqual(stored["state"], "STAGE")
        self.assertEqual(stored["inventory"][0]["running_sha"], OLD)
        self.assertEqual(stored["inventory"][0]["artifact_digest"], digest_of(OLD_BYTES))
        lock = session.engine.journal.replay()["locks"]
        self.assertTrue(lock)
        self.assertEqual(lock[next(iter(lock))]["run_id"], "run-1")
        continued = session.call("apply", "idem-w07-verify")
        self.assertEqual(continued.state, "VERIFY")

    def test_r03_idempotent_replay_auth_order_and_payload_mismatch(self):
        session = open_session(self)
        session.call("plan", "idem-w07-plan")
        session.call("apply", "idem-w07-stage")
        stage_calls = session.service.calls.count("stage")
        replay = session.call("plan", "idem-w07-plan")
        self.assertEqual(replay.code, "IDEMPOTENCY_REPLAY")
        self.assertEqual(replay.original_result["state"], "PLAN")
        self.assertEqual(replay.current_state, "STAGE")
        self.assertIsNone(replay.lease_token)
        self.assertEqual(session.service.calls.count("stage"), stage_calls)
        mismatch = session.call(
            "plan",
            "idem-w07-plan",
            artifact_digest=digest_of(b"other-body"),
            artifact_bytes=b"other-body",
        )
        self.assertEqual(mismatch.code, "IDEMPOTENCY_PAYLOAD_MISMATCH")
        self.assertEqual(session.service.calls.count("stage"), stage_calls)

        denied = open_session(self)
        denied.auth.authenticated = False
        result = denied.call("plan", "idem-w07-auth")
        self.assertEqual(result.code, "NOT_AUTHORIZED")
        self.assertNotIn("authorize", denied.auth.calls)
        self.assertEqual(denied.engine.journal.replay()["idempotency"], {})

        forbidden = open_session(self)
        forbidden.auth.allow = False
        result = forbidden.call("plan", "idem-w07-policy")
        self.assertEqual(result.code, "POLICY_DENIED")
        self.assertEqual(forbidden.auth.calls[:2], ["authenticate", "authorize"])
        self.assertEqual(forbidden.engine.journal.replay()["idempotency"], {})

    def test_r03_delivery_failure_does_not_undo_commit(self):
        session = open_session(self)
        session.events.fail = True
        self.assertEqual(session.call("plan", "idem-w07-plan").state, "PLAN")
        for key in ("stage", "verify", "switch", "health", "pending", "commit"):
            session.call("apply", f"idem-w07-{key}")
        self.assertEqual(session.engine.deployment(session.deployment_id)["state"], "COMMIT")
        deliveries = [note for note in session.engine.journal.replay()["notes"] if note["name"] == "delivery"]
        self.assertTrue(deliveries)
        self.assertTrue(all(note["status"] == "DELIVERY_UNKNOWN" for note in deliveries))

    def test_r04_crash_before_effect_does_not_guess_success(self):
        session = open_session(self)
        session.call("plan", "idem-w07-plan")
        session.call("apply", "idem-w07-stage")
        session.call("apply", "idem-w07-verify")
        session.engine.halt_at = "after_intent:switch"
        with self.assertRaises(Interrupted):
            session.call("apply", "idem-w07-switch")
        self.assertEqual(session.service.calls.count("switch"), 0)
        self.assertEqual(session.engine.deployment(session.deployment_id)["state"], "VERIFY")
        self.assertEqual(session.engine.deployment(session.deployment_id)["pending"]["phase"], "intent")
        session.reopen()
        resumed = session.call("apply", "idem-w07-switch")
        self.assertEqual(resumed.state, "SWITCH")
        self.assertEqual(session.service.calls.count("switch"), 1)

    def test_r04_crash_after_unknown_effect_requires_reconcile(self):
        session = open_session(self)
        session.call("plan", "idem-w07-plan")
        session.call("apply", "idem-w07-stage")
        session.call("apply", "idem-w07-verify")
        session.service.queue["switch"] = [Effect("unknown", detail="no receipt")]
        session.engine.halt_at = "after_result:switch"
        with self.assertRaises(Interrupted):
            session.call("apply", "idem-w07-switch")
        self.assertEqual(session.service.calls.count("switch"), 1)
        session.reopen()
        resumed = session.call("apply", "idem-w07-switch")
        self.assertEqual(resumed.code, "SIDE_EFFECT_UNKNOWN")
        self.assertEqual(resumed.state, "RECONCILE")
        self.assertEqual(session.service.calls.count("switch"), 1)
        again = session.call("apply", "idem-w07-switch-again")
        self.assertEqual(again.code, "SIDE_EFFECT_UNKNOWN")
        self.assertEqual(session.service.calls.count("switch"), 1)
        frozen = session.call("reconcile", "idem-w07-unknown", observation={"status": "unknown"})
        self.assertEqual(frozen.state, "SAFE_FROZEN")
        self.assertEqual(session.engine.transitions(session.deployment_id)[-1]["guard"], "outcome_unknown")
        later = session.call("apply", "idem-w07-after-freeze")
        self.assertEqual(later.state, "SAFE_FROZEN")
        assert_legal(self, session.engine, session.deployment_id)

    def test_r04_lost_receipt_does_not_commit_without_health(self):
        session = open_session(self)
        for key, _state in (
            ("plan", "PLAN"),
            ("stage", "STAGE"),
            ("verify", "VERIFY"),
            ("switch", "SWITCH"),
            ("health", "HEALTH"),
            ("pending", "COMMIT_PENDING"),
        ):
            method = "plan" if key == "plan" else "apply"
            result = session.call(method, f"idem-w07-{key}")
            self.assertEqual(result.state, _state)
        session.service.queue["commit"] = [Effect("unknown", detail="lost")]
        lost = session.call("apply", "idem-w07-commit")
        self.assertEqual(lost.state, "RECONCILE")
        self.assertEqual(lost.details["record"], "commit_ack_lost")
        self.assertNotEqual(lost.state, "COMMIT")
        session.service.queue["observe"] = [
            Effect(
                "ok",
                local={
                    "ok": True,
                    "running_sha": OLD,
                    "service_id": "web",
                    "artifact_digest": digest_of(OLD_BYTES),
                },
                public={"ok": True, "running_sha": HEAD},
            )
        ]
        denied = session.call(
            "reconcile",
            "idem-w07-backfill-denied",
            observation={"status": "healthy", "public": {"ok": True, "running_sha": HEAD}},
        )
        self.assertEqual(denied.code, "EVIDENCE_MISSING")
        self.assertEqual(denied.state, "RECONCILE")
        session.service.queue["observe"] = [
            Effect(
                "ok",
                local={
                    "ok": True,
                    "running_sha": HEAD,
                    "service_id": "web",
                    "artifact_digest": digest_of(NEW_BYTES),
                },
                public={"ok": False},
            )
        ]
        confirmed = session.call(
            "reconcile",
            "idem-w07-backfill",
            observation={
                "status": "healthy",
                "local": {
                    "ok": True,
                    "running_sha": HEAD,
                    "service_id": "web",
                    "artifact_digest": digest_of(NEW_BYTES),
                },
                "public": {"ok": False},
            },
        )
        self.assertEqual(confirmed.state, "COMMIT")
        self.assertEqual(
            session.engine.transitions(session.deployment_id)[-1]["guard"],
            "reconcile_confirms_healthy_and_backfill_commit",
        )
        assert_legal(self, session.engine, session.deployment_id)

    def test_r04_residual_matching_inventory_does_not_block_plan(self):
        allowed = open_session(self)
        residual = allowed.root / "residuals" / "dist.previous"
        residual.parent.mkdir()
        residual.mkdir()
        (residual / "BODY").write_bytes(OLD_BYTES)
        venv = allowed.root / "residuals" / "venv.previous"
        venv.mkdir()
        (venv / "BODY").write_bytes(OLD_BYTES)
        result = allowed.call("plan", "idem-w07-plan")
        self.assertEqual(result.state, "PLAN")
        self.assertTrue((residual / "BODY").exists())
        self.assertIn("dist.previous", result.details["residuals_kept"])

        blocked = open_session(self)
        stray = blocked.root / "residuals" / "dist.previous"
        stray.parent.mkdir()
        stray.write_bytes(b"not-the-previous-artifact")
        refused = blocked.call("plan", "idem-w07-stray")
        self.assertEqual(refused.details["record"], "residual_unbound")
        self.assertFalse(refused.details["deleted"])
        self.assertTrue(stray.exists())
        self.assertEqual(blocked.engine.journal.replay()["deployments"], {})
        self.assertEqual(blocked.service.calls, [])

    def test_r04_start_requires_current_lease(self):
        session = open_session(self)
        session.call("plan", "idem-w07-plan")
        started = session.call("start", "idem-w07-start")
        self.assertTrue(started.details["started"])
        self.assertEqual(started.state, "PLAN")
        session.lease.force_allow = True
        forged = session.call("start", "idem-w07-generation-token", lease_token="1", lease_generation=1)
        self.assertEqual(forged.code, "LEASE_FENCE_REJECTED")
        self.assertEqual(forged.details["record"], "generation_is_not_a_token")
        session.lease.force_allow = False
        session.lease.now = 10_000
        expired = session.call("start", "idem-w07-expired", server_now=1)
        self.assertEqual(expired.code, "LEASE_FENCE_REJECTED")
        self.assertEqual(session.engine.deployment(session.deployment_id)["state"], "PLAN")
        self.assertEqual(session.engine.deployment(session.deployment_id)["revision"], started.revision)

    def test_r05_health_requires_local_identity_not_public_observation(self):
        session = open_session(self)
        for key in ("plan", "stage", "verify", "switch"):
            method = "plan" if key == "plan" else "apply"
            session.call(method, f"idem-w07-{key}")
        session.service.queue["health"] = [
            Effect(
                "ok",
                local={
                    "ok": True,
                    "running_sha": OLD,
                    "service_id": "other",
                    "artifact_digest": digest_of(OLD_BYTES),
                },
                public={"ok": True, "running_sha": HEAD},
            )
        ]
        result = session.call("apply", "idem-w07-health")
        self.assertEqual(result.details["record"], "health_misdiagnosis")
        self.assertFalse(result.details["public_used_as_health"])
        self.assertEqual(result.state, "RECONCILE")
        guards = [edge["guard"] for edge in session.engine.transitions(session.deployment_id)]
        self.assertNotIn("health_ok", guards)
        self.assertNotIn("commit_ack_durable", guards)
        self.assertNotEqual(session.engine.deployment(session.deployment_id)["state"], "COMMIT")

    def test_r05_transient_failure_does_not_hold_and_deterministic_failure_does(self):
        transient = open_session(self)
        transient.service.queue["verify"] = [Effect("failed", "transient", "timeout")]
        transient.call("plan", "idem-w07-plan")
        transient.call("apply", "idem-w07-stage")
        failed = transient.call("apply", "idem-w07-verify")
        self.assertEqual(failed.state, "RECONCILE")
        self.assertFalse(failed.details["held"])
        self.assertEqual(failed.details["failure_class"], "transient")
        self.assertEqual(transient.call("rollback", "idem-w07-rollback").state, "ROLLBACK")
        restored = transient.call("verify_old", "idem-w07-old")
        self.assertEqual(restored.state, "ROLLED_BACK")
        retry = transient.call(
            "plan",
            "idem-w07-retry",
            deployment_id="dep_retry",
        )
        self.assertEqual(retry.state, "PLAN")
        self.assertNotEqual(retry.details.get("record"), "update_held")

        deterministic = open_session(self)
        deterministic.service.queue["verify"] = [Effect("failed", "deterministic", "build failed")]
        deterministic.call("plan", "idem-w07-plan")
        deterministic.call("apply", "idem-w07-stage")
        held = deterministic.call("apply", "idem-w07-verify")
        self.assertTrue(held.details["held"])
        self.assertEqual(deterministic.call("rollback", "idem-w07-rollback").state, "ROLLBACK")
        self.assertEqual(deterministic.call("verify_old", "idem-w07-old").state, "ROLLED_BACK")
        blocked = deterministic.call("plan", "idem-w07-held", deployment_id="dep_held")
        self.assertEqual(blocked.details["record"], "update_held")
        self.assertTrue(blocked.details["held"])
        self.assertIsNone(deterministic.engine.deployment("dep_held"))

    def test_r06_switch_failure_rolls_back_and_verifies_old(self):
        session = open_session(self)
        session.service.queue["switch"] = [Effect("failed", "deterministic", "switch rejected")]
        session.call("plan", "idem-w07-plan")
        session.call("apply", "idem-w07-stage")
        session.call("apply", "idem-w07-verify")
        failed = session.call("apply", "idem-w07-switch")
        self.assertEqual(failed.state, "RECONCILE")
        self.assertEqual(failed.details["record"], "switch_failed")
        self.assertEqual(session.service.calls.count("switch"), 1)
        untouched = session.call("apply", "idem-w07-switch-again")
        self.assertEqual(session.service.calls.count("switch"), 1)
        self.assertEqual(untouched.state, "RECONCILE")
        rolled = session.call("rollback", "idem-w07-rollback")
        self.assertEqual(rolled.state, "ROLLBACK")
        verified = session.call("verify_old", "idem-w07-old")
        self.assertEqual(verified.state, "ROLLED_BACK")
        self.assertEqual(session.service.running["running_sha"], OLD)
        self.assertEqual(
            [edge["to"] for edge in session.engine.transitions(session.deployment_id)],
            ["STAGE", "VERIFY", "RECONCILE", "ROLLBACK", "VERIFY_OLD", "ROLLED_BACK"],
        )
        self.assertEqual(session.engine.transitions(session.deployment_id)[-1]["guard"], "old_health_proved")
        assert_legal(self, session.engine, session.deployment_id)

    def test_r06_corrupted_old_artifact_and_rollback_failure_safe_frozen(self):
        corrupted = open_session(self)
        corrupted.service.queue["switch"] = [Effect("failed", "transient", "switch")]
        corrupted.call("plan", "idem-w07-plan")
        corrupted.call("apply", "idem-w07-stage")
        corrupted.call("apply", "idem-w07-verify")
        corrupted.call("apply", "idem-w07-switch")
        restored = corrupted.call("rollback", "idem-w07-rollback")
        self.assertEqual(restored.state, "ROLLBACK")
        old_path = corrupted.root / "artifacts" / digest_of(OLD_BYTES).split(":", 1)[1]
        old_path.write_bytes(b"corrupted-old")
        frozen = corrupted.call("verify_old", "idem-w07-old")
        self.assertEqual(frozen.state, "SAFE_FROZEN")
        self.assertEqual(frozen.details["record"], "old_health_unproven")
        self.assertTrue(frozen.details["journal_preserved"])
        self.assertTrue(frozen.details["backup_preserved"])
        self.assertTrue(old_path.exists())
        self.assertEqual(corrupted.engine.transitions(corrupted.deployment_id)[-1]["guard"], "old_health_unproven")
        self.assertIn(corrupted.deployment_id, corrupted.engine.journal.log_path.read_text(encoding="utf-8"))

        failed = open_session(self)
        failed.service.queue["switch"] = [Effect("failed", "transient", "switch")]
        failed.service.queue["rollback"] = [Effect("failed", "deterministic", "restore failed")]
        failed.call("plan", "idem-w07-plan")
        failed.call("apply", "idem-w07-stage")
        failed.call("apply", "idem-w07-verify")
        failed.call("apply", "idem-w07-switch")
        outcome = failed.call("rollback", "idem-w07-rollback")
        self.assertEqual(outcome.state, "SAFE_FROZEN")
        self.assertEqual(outcome.details["record"], "rollback_failed")
        self.assertTrue(outcome.details["journal_preserved"])
        self.assertTrue((failed.root / "artifacts" / digest_of(OLD_BYTES).split(":", 1)[1]).exists())
        stuck = failed.call("apply", "idem-w07-stuck")
        self.assertEqual(stuck.state, "SAFE_FROZEN")
        frozen_id = failed.deployment_id
        failed.auth.recovery = False
        denied = failed.call(
            "plan",
            "idem-w07-recover-denied",
            deployment_id="dep_next",
            recovers_deployment_id=frozen_id,
        )
        self.assertEqual(denied.code, "POLICY_DENIED")
        self.assertEqual(failed.engine.deployment(frozen_id)["state"], "SAFE_FROZEN")
        failed.auth.recovery = True
        recovered = failed.call(
            "plan",
            "idem-w07-recover",
            deployment_id="dep_next",
            recovers_deployment_id=frozen_id,
        )
        self.assertEqual(recovered.state, "PLAN")
        self.assertEqual(recovered.deployment_id, "dep_next")
        self.assertEqual(failed.engine.deployment(frozen_id)["state"], "SAFE_FROZEN")
        self.assertEqual(
            failed.engine.deployment("dep_next")["recovers_deployment_id"],
            frozen_id,
        )
        copied = failed.engine.deployment(frozen_id)
        copied["state"] = "SAFE_FROZEN"
        with self.assertRaises(ContractError):
            failed.engine._transition(copied, "SAFE_FROZEN", "PLAN", None)
        self.assertEqual(failed.engine.deployment(frozen_id)["state"], "SAFE_FROZEN")

    def test_r06_first_install_without_removal_proof_safe_frozen(self):
        session = open_session(
            self,
            install_kind="first_install",
            previous_healthy_sha=None,
            previous_artifact_bytes=None,
            previous_artifact_digest=None,
            running_sha=None,
        )
        session.service.queue["verify"] = [Effect("failed", "deterministic", "build")]
        session.call("plan", "idem-w07-plan")
        session.call("apply", "idem-w07-stage")
        session.call("apply", "idem-w07-verify")
        missing = session.call("rollback", "idem-w07-rollback", resources_removed=False)
        self.assertEqual(missing.state, "SAFE_FROZEN")
        self.assertEqual(missing.details["record"], "removal_unproven")
        self.assertIsNone(session.engine.deployment(session.deployment_id)["previous_healthy_sha"])
        guards = [edge["guard"] for edge in session.engine.transitions(session.deployment_id)]
        self.assertNotIn("old_health_proved", guards)
        self.assertNotIn("first_install_resources_removed", guards)
        self.assertFalse(allows(EDGES, "ROLLBACK", "ROLLED_BACK", "old_health_proved"))

        proved = open_session(
            self,
            install_kind="first_install",
            previous_healthy_sha=None,
            previous_artifact_bytes=None,
            previous_artifact_digest=None,
            running_sha=None,
        )
        proved.service.queue["verify"] = [Effect("failed", "transient", "blip")]
        proved.service.queue["rollback"] = [Effect("ok", resources_removed=True)]
        proved.call("plan", "idem-w07-plan")
        proved.call("apply", "idem-w07-stage")
        proved.call("apply", "idem-w07-verify")
        done = proved.call("rollback", "idem-w07-rollback", resources_removed=True)
        self.assertEqual(done.state, "ROLLED_BACK")
        self.assertEqual(
            proved.engine.transitions(proved.deployment_id)[-1]["guard"],
            "first_install_resources_removed",
        )
        self.assertIsNone(proved.engine.deployment(proved.deployment_id)["previous_healthy_sha"])

    def test_r07_boot_recovery_reloads_midflight_without_reset(self):
        session = open_session(self)
        session.call("plan", "idem-w07-plan")
        session.call("apply", "idem-w07-stage")
        before = session.engine.deployment(session.deployment_id)
        session.reopen()
        report = session.call("recover_boot", "idem-w07-boot", hostops_pass_count=7)
        after = session.engine.deployment(session.deployment_id)
        self.assertEqual(after["state"], before["state"])
        self.assertEqual(after["revision"], before["revision"])
        self.assertFalse(report.details["boot_recovery_proved"])
        self.assertTrue(report.details["state_unchanged"])
        self.assertEqual(report.details["hostops_pass_count_ignored"], 7)
        self.assertFalse(report.details["migration_executed"])
        self.assertNotIn("switch", session.service.calls)

    def test_r07_migration_digest_blocks_without_executing_sql(self):
        body = "create table example(id int);\n"
        filename_only = open_session(self)
        result = filename_only.call(
            "plan",
            "idem-w07-filename",
            migrations={
                "files": [{"filename": "001_init.sql", "body": body}],
                "ledger": [{"filename": "001_init.sql"}],
            },
        )
        self.assertEqual(result.details["record"], "schema_blocked")
        self.assertFalse(result.details["executed"])
        self.assertFalse(result.details["held"])
        self.assertEqual(result.details["problems"][0]["reason"], "digest_mismatch")
        self.assertEqual(filename_only.engine.journal.replay()["deployments"], {})
        self.assertEqual(filename_only.engine.journal.replay()["holds"], {})

        pending = open_session(self)
        blocked = pending.call(
            "plan",
            "idem-w07-pending",
            migrations={"files": [{"filename": "001_init.sql", "body": body}], "ledger": []},
        )
        self.assertEqual(blocked.details["problems"][0]["reason"], "pending")
        self.assertFalse(blocked.details["held"])
        allowed = pending.call("plan", "idem-w07-after-pending")
        self.assertEqual(allowed.state, "PLAN")

        forced = open_session(self)
        forced.auth.high_risk = True
        forced.auth.allow = True
        rejected = forced.call("plan", "idem-w07-apply-sql", apply_migrations=True)
        self.assertEqual(rejected.code, "PRODUCTION_DB_FORBIDDEN")
        self.assertFalse(rejected.details["executed"])
        self.assertEqual(forced.engine.journal.replay()["idempotency"], {})
        self.assertEqual(forced.engine.journal.replay()["deployments"], {})

    def test_r09_hostops_count_is_not_cold_start_and_full_chain_is_required(self):
        session = open_session(self)
        for key in ("plan", "stage", "verify", "switch", "health", "pending", "commit"):
            method = "plan" if key == "plan" else "apply"
            self.assertEqual(session.call(method, f"idem-w07-{key}").http, 200)
        self.assertEqual(session.engine.deployment(session.deployment_id)["state"], "COMMIT")
        switches = session.service.calls.count("switch")
        rollbacks = session.service.calls.count("rollback")
        session.service.queue["observe"] = [
            Effect(
                "ok",
                local={
                    "ok": True,
                    "running_sha": OLD,
                    "service_id": "web",
                    "artifact_digest": digest_of(OLD_BYTES),
                },
            )
        ]
        weak = session.call("recover_boot", "idem-w07-weak", hostops_pass_count=7)
        self.assertFalse(weak.details["boot_recovery_proved"])
        self.assertEqual(weak.details["hostops_pass_count_ignored"], 7)
        self.assertFalse(weak.details["links"]["service_identity_checked"])
        self.assertEqual(session.engine.deployment(session.deployment_id)["state"], "COMMIT")
        proved = session.call("recover_boot", "idem-w07-chain", hostops_pass_count=0)
        self.assertTrue(proved.details["boot_recovery_proved"])
        self.assertTrue(all(proved.details["links"].values()))
        self.assertFalse(proved.details["production_enablement"])
        self.assertEqual(session.service.calls.count("switch"), switches)
        self.assertEqual(session.service.calls.count("rollback"), rollbacks)

    def test_r09_safe_frozen_has_no_exit(self):
        session = open_session(self)
        session.service.queue["switch"] = [Effect("unknown")]
        session.call("plan", "idem-w07-plan")
        session.call("apply", "idem-w07-stage")
        session.call("apply", "idem-w07-verify")
        unknown = session.call("apply", "idem-w07-switch")
        self.assertEqual(unknown.state, "RECONCILE")
        frozen = session.call("reconcile", "idem-w07-freeze", observation={"status": "unknown"})
        self.assertEqual(frozen.state, "SAFE_FROZEN")
        self.assertEqual(session.call("apply", "idem-w07-leave").state, "SAFE_FROZEN")
        self.assertEqual(session.call("rollback", "idem-w07-leave-rb").state, "SAFE_FROZEN")
        self.assertFalse(any(edge["from"] == "SAFE_FROZEN" for edge in EDGES))

    def _drive(self, session, *steps):
        for key in steps:
            method = "plan" if key == "plan" else "apply"
            result = session.call(method, f"idem-w07-{key}")
            self.assertNotEqual(result.state, "SAFE_FROZEN", key)
        return result

    def test_p0_unfinished_external_intent_is_not_repeated(self):
        switched = open_session(self)
        self._drive(switched, "plan", "stage", "verify")
        switched.engine.halt_at = "before_result:switch"
        with self.assertRaises(Interrupted):
            switched.call("apply", "idem-w07-switch")
        self.assertEqual(switched.service.calls.count("switch"), 1)
        self.assertEqual(switched.service.running["running_sha"], HEAD)
        pending = switched.engine.deployment(switched.deployment_id)["pending"]
        self.assertEqual(pending["phase"], "intent")
        operation_id = pending["operation_id"]
        self.assertTrue(operation_id)
        switched.reopen()
        resumed = switched.call("apply", "idem-w07-switch-resume")
        self.assertEqual(switched.service.calls.count("switch"), 1)
        self.assertEqual(resumed.state, "SWITCH")
        self.assertIn(operation_id, switched.service.operations)

        rolled = open_session(self)
        rolled.service.queue["switch"] = [Effect("failed", "deterministic", "switch rejected")]
        self._drive(rolled, "plan", "stage", "verify", "switch")
        self.assertEqual(rolled.engine.deployment(rolled.deployment_id)["state"], "RECONCILE")
        rolled.engine.halt_at = "before_result:rollback"
        with self.assertRaises(Interrupted):
            rolled.call("rollback", "idem-w07-rollback")
        self.assertEqual(rolled.service.calls.count("rollback"), 1)
        self.assertEqual(rolled.service.running["running_sha"], OLD)
        crashed = rolled.engine.deployment(rolled.deployment_id)
        self.assertEqual(crashed["pending"]["phase"], "intent")
        rolled.revision = crashed["revision"]
        rolled.fence = crashed["fence_generation"]
        rolled.reopen()
        restored = rolled.call("rollback", "idem-w07-rollback-resume")
        self.assertEqual(rolled.service.calls.count("rollback"), 1)
        self.assertEqual(restored.details["record"], "restore_recorded")
        self.assertTrue(rolled.engine.deployment(rolled.deployment_id)["restore_ok"])

        committed = open_session(self)
        self._drive(committed, "plan", "stage", "verify", "switch", "health", "pending")
        self.assertEqual(committed.engine.deployment(committed.deployment_id)["state"], "COMMIT_PENDING")
        committed.engine.halt_at = "before_result:commit"
        with self.assertRaises(Interrupted):
            committed.call("apply", "idem-w07-commit")
        self.assertEqual(committed.service.calls.count("commit"), 1)
        self.assertEqual(committed.engine.deployment(committed.deployment_id)["pending"]["phase"], "intent")
        committed.reopen()
        done = committed.call("apply", "idem-w07-commit-resume")
        self.assertEqual(committed.service.calls.count("commit"), 1)
        self.assertEqual(done.state, "COMMIT")

    def test_p0_terminal_finalize_and_w01_stream_recover(self):
        session = open_session(self)
        self._drive(session, "plan", "stage", "verify", "switch", "health", "pending")
        self.assertEqual([fact["producer_seq"] for fact in session.events.facts], [1])
        self.assertEqual(session.events.facts[0]["event_type"], "deployment.health")
        session.engine.halt_at = "after_terminal_intent:COMMIT"
        with self.assertRaises(Interrupted):
            session.call("apply", "idem-w07-commit")
        stored = session.engine.deployment(session.deployment_id)
        self.assertEqual(stored["state"], "COMMIT")
        self.assertFalse(stored["finalize"]["inventory_done"])
        self.assertFalse(stored["finalize"]["release_done"])
        fact = stored["finalize"]["delivery_fact"]
        self.assertEqual(fact["event_type"], "deployment.commit")
        self.assertEqual(fact["producer_seq"], 2)
        self.assertNotEqual(fact["producer_seq"], stored["revision"])
        self.assertNotIn("received_at", fact)
        self.assertEqual(fact["stream_id"], "host_deployer:deployment")
        self.assertEqual(fact["stream_epoch"], 1)
        fence = session.root / "fence" / "base__host-test__web.json"
        self.assertTrue(fence.is_file())
        saved = [
            record
            for record in session.engine.journal.replay()["deliveries"].values()
            if record["event_type"] == "deployment.commit"
        ]
        self.assertEqual(saved[0]["status"], "DURABLE_LOCAL")
        self.assertEqual(saved[0]["fact"]["event_id"], fact["event_id"])
        self.assertEqual([item["event_type"] for item in session.events.facts], ["deployment.health"])
        session.reopen()
        recovered = session.call("recover_boot", "idem-w07-finalize", hostops_pass_count=4)
        done = session.engine.deployment(session.deployment_id)
        self.assertTrue(done["finalize"]["inventory_done"])
        self.assertTrue(done["finalize"]["delivery_done"])
        self.assertTrue(done["finalize"]["release_done"])
        self.assertFalse(fence.exists())
        self.assertTrue(recovered.details["boot_recovery_proved"])
        commits = [item for item in session.events.facts if item["event_type"] == "deployment.commit"]
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0]["producer_seq"], 2)
        self.assertEqual(commits[0]["received_at"], "server")
        before = len(session.events.facts)
        replay = session.events.append_deployment({key: value for key, value in commits[0].items() if key != "received_at"})
        self.assertEqual(replay["event_id"], commits[0]["event_id"])
        self.assertEqual(replay["producer_seq"], 2)
        self.assertEqual(replay["fact_state"], "ACCEPTED")
        self.assertEqual(len(session.events.facts), before)
        session.call("recover_boot", "idem-w07-finalize-again")
        self.assertEqual(len([item for item in session.events.facts if item["event_type"] == "deployment.commit"]), 1)
        gap = dict(commits[0])
        gap.pop("received_at")
        gap["event_id"] = "evt_gap"
        gap["idempotency_key"] = "idem-gap"
        gap["producer_seq"] = 9
        with self.assertRaises(EventRejected) as rejected:
            session.events.append_deployment(gap)
        self.assertEqual(str(rejected.exception), "sequence")

        shared = open_session(self)
        for key in ("plan", "stage", "verify", "switch", "health", "pending", "commit"):
            method = "plan" if key == "plan" else "apply"
            self.assertEqual(shared.call(method, f"idem-w07-{key}").http, 200)
        second = shared.call("plan", "idem-w07-second", deployment_id="dep_second")
        self.assertEqual(second.state, "PLAN")
        for key in ("stage", "verify", "switch", "health"):
            self.assertEqual(shared.call("apply", f"idem-w07-second-{key}").http, 200)
        sequences = [(fact["event_type"], fact["producer_seq"], fact["stream_epoch"]) for fact in shared.events.facts]
        self.assertEqual(
            sequences,
            [
                ("deployment.health", 1, 1),
                ("deployment.commit", 2, 1),
                ("deployment.health", 3, 1),
            ],
        )
        self.assertTrue(all(fact["stream_id"] == "host_deployer:deployment" for fact in shared.events.facts))

        unknown = open_session(self)
        unknown.events.fail = True
        for key in ("plan", "stage", "verify", "switch", "health", "pending", "commit"):
            method = "plan" if key == "plan" else "apply"
            unknown.call(method, f"idem-w07-{key}")
        self.assertEqual(unknown.engine.deployment(unknown.deployment_id)["state"], "COMMIT")
        notes = [note for note in unknown.engine.journal.replay()["notes"] if note["name"] == "delivery"]
        self.assertTrue(notes)
        self.assertTrue(all(note["status"] == "DELIVERY_UNKNOWN" for note in notes))
        self.assertTrue(all(note["fact"]["event_id"] and "received_at" not in note["fact"] for note in notes))
        unknown.events.fail = False
        unknown.reopen()
        unknown.call("recover_boot", "idem-w07-deliver")
        delivered = [(fact["event_type"], fact["producer_seq"]) for fact in unknown.events.facts]
        self.assertEqual(delivered, [("deployment.health", 1), ("deployment.commit", 2)])
        unknown.call("recover_boot", "idem-w07-deliver-again")
        self.assertEqual(len(unknown.events.facts), 2)

        rejected_port = open_session(self)
        rejected_port.events.reject_reason = "schema_rejected"
        for key in ("plan", "stage", "verify", "switch", "health", "pending", "commit"):
            method = "plan" if key == "plan" else "apply"
            rejected_port.call(method, f"idem-w07-{key}")
        self.assertEqual(rejected_port.engine.deployment(rejected_port.deployment_id)["state"], "COMMIT")
        self.assertEqual(rejected_port.events.facts, [])
        quarantined = [
            record
            for record in rejected_port.engine.journal.replay()["deliveries"].values()
            if record["status"] == "QUARANTINED"
        ]
        self.assertTrue(quarantined)
        self.assertTrue(all(record["fact"]["event_id"] and record["rejection"] == "schema_rejected" for record in quarantined))
        rejected_port.events.reject_reason = None
        rejected_port.reopen()
        rejected_port.call("recover_boot", "idem-w07-quarantine")
        self.assertEqual(rejected_port.events.facts, [])

    def test_p0_server_authority_not_caller_observation_clock_or_principal(self):
        session = open_session(self)
        self._drive(session, "plan", "stage", "verify", "switch", "health", "pending")
        session.service.queue["commit"] = [Effect("unknown", detail="lost")]
        lost = session.call("apply", "idem-w07-commit")
        self.assertEqual(lost.state, "RECONCILE")
        session.service.queue["observe"] = [
            Effect(
                "ok",
                local={
                    "ok": True,
                    "running_sha": OLD,
                    "service_id": "web",
                    "artifact_digest": digest_of(OLD_BYTES),
                },
                public={"ok": True, "running_sha": HEAD},
            )
        ]
        denied = session.call(
            "reconcile",
            "idem-w07-lie",
            server_now=1,
            observation={
                "status": "healthy",
                "local": {
                    "ok": True,
                    "running_sha": HEAD,
                    "service_id": "web",
                    "artifact_digest": digest_of(NEW_BYTES),
                },
            },
        )
        self.assertEqual(denied.code, "EVIDENCE_MISSING")
        self.assertEqual(denied.state, "RECONCILE")
        self.assertNotEqual(denied.state, "COMMIT")
        checked = [item for item in session.lease.checks if item["candidate_id"]]
        self.assertTrue(checked)
        self.assertEqual(checked[-1]["principal_id"], "fixture")
        self.assertEqual(checked[-1]["server_now"], 100)
        self.assertIn(session.request.candidate_id, checked[-1]["scope"])
        self.assertEqual(checked[-1]["service_id"], "web")
        self.assertEqual(checked[-1]["artifact_digest"], digest_of(NEW_BYTES))
        session.service.queue["observe"] = [
            Effect(
                "ok",
                local={
                    "ok": True,
                    "running_sha": HEAD,
                    "service_id": "web",
                    "artifact_digest": digest_of(NEW_BYTES),
                },
                public={"ok": False},
            )
        ]
        proved = session.call("reconcile", "idem-w07-server-health", server_now=1)
        self.assertEqual(proved.state, "COMMIT")

        expired = open_session(self)
        expired.lease.now = 10_000
        blocked = expired.call("plan", "idem-w07-caller-clock", server_now=1)
        self.assertEqual(blocked.code, "LEASE_FENCE_REJECTED")
        self.assertEqual(expired.engine.journal.replay()["deployments"], {})
        self.assertTrue(all(item["server_now"] == 10_000 for item in expired.lease.checks))

        spoofed = open_session(self)
        spoofed.auth.server_principal = "intruder"
        spoofed.lease.principal = "fixture"
        rejected = spoofed.call("plan", "idem-w07-caller-principal", principal_id="fixture")
        self.assertEqual(rejected.code, "LEASE_FENCE_REJECTED")
        self.assertEqual(spoofed.lease.checks[-1]["principal_id"], "intruder")
        self.assertEqual(spoofed.engine.journal.replay()["deployments"], {})

    def test_p0_reconcile_boot_and_takeover_bind_exact_ids(self):
        session = open_session(self)
        self._drive(session, "plan", "stage", "verify")
        session.service.queue["switch"] = [Effect("unknown")]
        unknown = session.call("apply", "idem-w07-switch")
        self.assertEqual(unknown.state, "RECONCILE")
        operation_id = session.engine.deployment(session.deployment_id)["pending"]["operation_id"]
        mismatched = session.call("reconcile", "idem-w07-wrong-op", operation_id="op_other", server_now=1)
        self.assertEqual(mismatched.details["record"], "operation_binding")
        self.assertEqual(session.engine.deployment(session.deployment_id)["state"], "RECONCILE")
        other = candidate(task="tsk_other")
        stale = session.call(
            "reconcile",
            "idem-w07-wrong-candidate",
            candidate=other,
            candidate_id=cid(other),
        )
        self.assertEqual(stale.details["record"], "candidate_binding")
        frozen = session.call("reconcile", "idem-w07-exact", operation_id=operation_id, server_now=1)
        self.assertEqual(frozen.state, "SAFE_FROZEN")
        binding = [item for item in session.lease.checks if item["operation_id"] == operation_id]
        self.assertTrue(binding)
        self.assertEqual(binding[-1]["principal_id"], "fixture")
        self.assertIn(operation_id, binding[-1]["scope"])
        self.assertEqual(binding[-1]["artifact_digest"], digest_of(NEW_BYTES))

        booting = open_session(self)
        for key in ("plan", "stage", "verify", "switch", "health", "pending", "commit"):
            method = "plan" if key == "plan" else "apply"
            self.assertEqual(booting.call(method, f"idem-w07-{key}").http, 200)
        booting.service.queue["observe"] = [
            Effect(
                "ok",
                local={
                    "ok": True,
                    "running_sha": HEAD,
                    "service_id": "web",
                    "artifact_digest": digest_of(NEW_BYTES),
                },
            )
        ]
        lied = booting.call(
            "recover_boot",
            "idem-w07-boot-lie",
            artifact_digest=digest_of(b"not-the-artifact"),
            artifact_bytes=b"not-the-artifact",
            observation={"status": "healthy", "local": {"ok": True, "running_sha": HEAD}},
            hostops_pass_count=9,
        )
        self.assertEqual(lied.details["record"], "artifact_binding")
        self.assertFalse(lied.details.get("boot_recovery_proved", False))
        self.assertEqual(booting.engine.deployment(booting.deployment_id)["state"], "COMMIT")

        held = open_session(self)
        held.call("plan", "idem-w07-plan")
        generation = held.engine.deployment(held.deployment_id)["fence_generation"]
        held.lease.allow("run-2")
        refused = held.call("start", "idem-w07-bad-takeover", takeover=True, run_id="run-2", service_id="other")
        self.assertEqual(refused.details["record"], "service_binding")
        self.assertEqual(held.engine.deployment(held.deployment_id)["fence_generation"], generation)
        self.assertEqual(held.engine.deployment(held.deployment_id)["owner_run_id"], "run-1")

    def test_p0_cross_process_fence_and_torn_journal_fail_closed(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        ready = root / "ready"
        script = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "from app.control_deploy import DeployEngine\n"
            "from tests.test_v060_w07_isolated_deploy import MemoryAuth, MemoryLease, ScriptedService, make_request\n"
            "root, ready_path = sys.argv[1], sys.argv[2]\n"
            "engine = DeployEngine(root, service=ScriptedService(), lease=MemoryLease(), auth=MemoryAuth())\n"
            "result = engine.plan(make_request())\n"
            "Path(ready_path).write_text((result.state or '') + '\\n' + str(result.code or ''), encoding='utf-8')\n"
            "time.sleep(30)\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(root), str(ready)],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        parent = None
        try:
            deadline = time.time() + 15
            while time.time() < deadline and not ready.exists():
                if proc.poll() is not None:
                    self.fail(proc.stderr.read().decode("utf-8", errors="replace"))
                time.sleep(0.05)
            self.assertTrue(ready.exists())
            self.assertTrue(ready.read_text(encoding="utf-8").startswith("PLAN"))
            lease = MemoryLease()
            lease.allow("run-2")
            parent = DeployEngine(
                root,
                service=ScriptedService(),
                lease=lease,
                auth=MemoryAuth(),
                contract_path=CONTRACT_PATH,
            )
            self.addCleanup(parent.close)
            stored = parent.deployment("dep_w07")
            generation = stored["fence_generation"]
            blocked = parent.start(
                replace(
                    make_request(),
                    takeover=True,
                    run_id="run-2",
                    deployment_id="dep_w07",
                    expected_revision=stored["revision"],
                    fence_generation=generation,
                    idempotency_key="idem-alive",
                )
            )
            self.assertEqual(blocked.code, "RESERVATION_CONFLICT")
            self.assertEqual(blocked.details["record"], "owner_alive")
            self.assertEqual(parent.deployment("dep_w07")["fence_generation"], generation)
            self.assertEqual(parent.fence.read("base__host-test__web")["holder_pid"], proc.pid)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        self.assertIsNotNone(parent)
        current = parent.deployment("dep_w07")
        adopted = parent.start(
            replace(
                make_request(),
                takeover=True,
                run_id="run-2",
                deployment_id="dep_w07",
                expected_revision=current["revision"],
                fence_generation=current["fence_generation"],
                idempotency_key="idem-dead",
            )
        )
        self.assertEqual(adopted.http, 200, adopted.details)
        self.assertGreater(adopted.fence_generation, current["fence_generation"])
        deaths = [note for note in parent.journal.replay()["notes"] if note.get("name") == "fence_death_reconciled"]
        self.assertTrue(deaths)
        self.assertEqual(deaths[-1]["previous_pid"], proc.pid)

        session = open_session(self)
        session.call("plan", "idem-w07-plan")
        session.call("apply", "idem-w07-stage")
        with session.engine.journal.log_path.open("a", encoding="utf-8") as handle:
            handle.write('{"type":"deployment","deployment":{"deployment_id":"dep_w07","state":"COMMIT"')
        with self.assertRaises(JournalCorrupt) as raised:
            session.engine.deployment(session.deployment_id)
        self.assertTrue(raised.exception.torn)
        self.assertTrue(raised.exception.recoverable)
        self.assertIsNotNone(raised.exception.line)
        diagnosis = session.engine.journal.diagnose()
        self.assertFalse(diagnosis["ok"])
        self.assertTrue(diagnosis["recoverable"])
        self.assertGreaterEqual(diagnosis["offset"], 0)
        recovered = session.engine.journal.recover_torn_tail()
        self.assertTrue(recovered["recovered"])
        self.assertTrue(recovered["quarantined"])
        self.assertEqual(session.engine.deployment(session.deployment_id)["state"], "STAGE")
        quarantine = Path(recovered["quarantine_path"]).read_text(encoding="utf-8")
        self.assertIn("COMMIT", quarantine)
        self.assertIn(str(recovered["line"]), quarantine)
        with session.engine.journal.log_path.open("a", encoding="utf-8") as handle:
            handle.write('{"type":"nope"}\n{"type":"note","name":"after"}\n')
        with self.assertRaises(JournalCorrupt) as middle:
            session.engine.deployment(session.deployment_id)
        self.assertFalse(middle.exception.recoverable)
        refused = session.engine.journal.recover_torn_tail()
        self.assertFalse(refused["recovered"])
        self.assertEqual(refused["reason"], middle.exception.reason)
        self.assertGreaterEqual(refused["line"], 1)

    def test_recovery_edge_missing_operation_record_is_not_retried(self):
        def crash_after_effect(session, action, method, key):
            session.engine.halt_at = f"before_result:{action}"
            with self.assertRaises(Interrupted):
                session.call(method, key)
            session.engine.halt_at = None
            pending = session.engine.deployment(session.deployment_id)["pending"]
            self.assertEqual(pending["phase"], "intent")
            self.assertEqual(session.service.calls.count(action), 1)
            crashed = session.engine.deployment(session.deployment_id)
            session.revision = crashed["revision"]
            session.fence = crashed["fence_generation"]
            return pending["operation_id"]

        switched = open_session(self)
        self._drive(switched, "plan", "stage", "verify")
        operation_id = crash_after_effect(switched, "switch", "apply", "idem-w07-switch")
        self.assertEqual(switched.service.running["running_sha"], HEAD)
        switched.service.operations.pop(operation_id)

        def lookup_missing(_operation_id):
            return None

        switched.service.operation_result = lookup_missing
        switched.reopen()
        missing = switched.call("apply", "idem-w07-switch-missing")
        self.assertEqual(switched.service.calls.count("switch"), 1)
        self.assertEqual(missing.code, "SIDE_EFFECT_UNKNOWN")
        self.assertEqual(missing.state, "RECONCILE")
        self.assertEqual(switched.service.running["running_sha"], HEAD)

        rolled = open_session(self)
        rolled.service.queue["switch"] = [Effect("failed", "deterministic", "switch rejected")]
        self._drive(rolled, "plan", "stage", "verify", "switch")
        operation_id = crash_after_effect(rolled, "rollback", "rollback", "idem-w07-rollback")
        self.assertEqual(rolled.service.running["running_sha"], OLD)
        rolled.service.operations[operation_id] = "corrupt-record"

        def lookup_corrupt(op_id):
            found = rolled.service.operations.get(op_id)
            if not isinstance(found, Effect):
                return None
            return found

        rolled.service.operation_result = lookup_corrupt
        rolled.reopen()
        corrupt = rolled.call("rollback", "idem-w07-rollback-corrupt")
        self.assertEqual(rolled.service.calls.count("rollback"), 1)
        self.assertEqual(corrupt.code, "SIDE_EFFECT_UNKNOWN")
        self.assertEqual(rolled.service.running["running_sha"], OLD)

        committed = open_session(self)
        self._drive(committed, "plan", "stage", "verify", "switch", "health", "pending")
        operation_id = crash_after_effect(committed, "commit", "apply", "idem-w07-commit")
        committed.service.operations.pop(operation_id)
        committed.service.operation_result = lookup_missing
        committed.reopen()
        lost = committed.call("apply", "idem-w07-commit-missing")
        self.assertEqual(committed.service.calls.count("commit"), 1)
        self.assertEqual(lost.code, "SIDE_EFFECT_UNKNOWN")
        self.assertNotEqual(lost.state, "COMMIT")

        proved = open_session(self)
        self._drive(proved, "plan", "stage", "verify")
        proved.engine.halt_at = "after_intent:switch"
        with self.assertRaises(Interrupted):
            proved.call("apply", "idem-w07-switch-intent")
        proved.engine.halt_at = None
        self.assertEqual(proved.service.calls.count("switch"), 0)
        intent_id = proved.engine.deployment(proved.deployment_id)["pending"]["operation_id"]
        proof = proved.service.operation_result(intent_id)
        self.assertIsInstance(proof, NotOccurred)
        self.assertEqual(proof.operation_id, intent_id)
        proved.reopen()
        retried = proved.call("apply", "idem-w07-switch-proved-absent")
        self.assertEqual(proved.service.calls.count("switch"), 1)
        self.assertEqual(retried.state, "SWITCH")

        wrong = open_session(self)
        self._drive(wrong, "plan", "stage", "verify")
        wrong_id = crash_after_effect(wrong, "switch", "apply", "idem-w07-switch-wrong")
        self.assertEqual(wrong.service.running["running_sha"], HEAD)

        def lookup_wrong(_operation_id):
            return NotOccurred("op_other")

        wrong.service.operation_result = lookup_wrong
        wrong.service.operations.pop(wrong_id, None)
        wrong.reopen()
        refused = wrong.call("apply", "idem-w07-switch-wrong-proof")
        self.assertEqual(wrong.service.calls.count("switch"), 1)
        self.assertEqual(refused.code, "SIDE_EFFECT_UNKNOWN")
        self.assertEqual(wrong.service.running["running_sha"], HEAD)

    def test_recovery_edge_finalize_requires_proved_fence_release(self):
        session = open_session(self)
        self._drive(session, "plan", "stage", "verify", "switch", "health", "pending")
        session.engine.halt_at = "after_terminal_intent:COMMIT"
        with self.assertRaises(Interrupted):
            session.call("apply", "idem-w07-commit")
        session.engine.halt_at = None
        stored = session.engine.deployment(session.deployment_id)
        session.revision = stored["revision"]
        session.fence = stored["fence_generation"]
        self.assertEqual(stored["state"], "COMMIT")
        self.assertFalse(stored["finalize"]["release_done"])
        fence = session.root / "fence" / "base__host-test__web.json"
        body = json.loads(fence.read_text(encoding="utf-8"))
        body["generation"] = stored["fence_generation"] + 1
        fence.write_text(json.dumps(body), encoding="utf-8")
        mismatched = session.call("recover_boot", "idem-w07-release-mismatch")
        done = session.engine.deployment(session.deployment_id)
        self.assertFalse(done["finalize"]["release_done"])
        self.assertTrue(fence.is_file())
        self.assertEqual(json.loads(fence.read_text(encoding="utf-8"))["generation"], stored["fence_generation"] + 1)
        self.assertNotIn('"type":"lock_release"', session.engine.journal.log_path.read_text(encoding="utf-8"))
        self.assertEqual(mismatched.state, "COMMIT")

        absent = open_session(self)
        self._drive(absent, "plan", "stage", "verify", "switch", "health", "pending")
        absent.engine.halt_at = "after_terminal_intent:COMMIT"
        with self.assertRaises(Interrupted):
            absent.call("apply", "idem-w07-commit")
        absent.engine.halt_at = None
        stored = absent.engine.deployment(absent.deployment_id)
        absent.revision = stored["revision"]
        absent.fence = stored["fence_generation"]
        missing_fence = absent.root / "fence" / "base__host-test__web.json"
        missing_fence.unlink()
        absent.call("recover_boot", "idem-w07-release-absent")
        done = absent.engine.deployment(absent.deployment_id)
        self.assertFalse(done["finalize"]["release_done"])
        self.assertFalse(missing_fence.exists())
        self.assertNotIn('"type":"lock_release"', absent.engine.journal.log_path.read_text(encoding="utf-8"))

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        held = Session(
            root,
            ScriptedService(),
            MemoryLease(),
            MemoryAuth(),
            MemoryEvents(),
            make_request(),
        )
        self.addCleanup(held.close)
        for key in ("plan", "stage", "verify", "switch", "health", "pending"):
            method = "plan" if key == "plan" else "apply"
            self.assertEqual(held.call(method, f"idem-w07-{key}").http, 200)
        held.engine.halt_at = "after_terminal_intent:COMMIT"
        with self.assertRaises(Interrupted):
            held.call("apply", "idem-w07-commit")
        stored = held.engine.deployment(held.deployment_id)
        self.assertFalse(stored["finalize"]["release_done"])
        fence = root / "fence" / "base__host-test__web.json"
        self.assertTrue(fence.is_file())
        held.close()
        ready = root / "fence-held"
        script = (
            "import json, os, sys, time, fcntl\n"
            "path, ready_path = sys.argv[1], sys.argv[2]\n"
            "fd = os.open(path, os.O_RDWR)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "os.lseek(fd, 0, os.SEEK_SET)\n"
            "raw = b''\n"
            "while True:\n"
            "    block = os.read(fd, 4096)\n"
            "    if not block:\n"
            "        break\n"
            "    raw += block\n"
            "body = json.loads(raw.decode())\n"
            "body['generation'] = 7\n"
            "payload = json.dumps(body, sort_keys=True).encode()\n"
            "os.lseek(fd, 0, os.SEEK_SET)\n"
            "os.ftruncate(fd, 0)\n"
            "os.write(fd, payload)\n"
            "os.fsync(fd)\n"
            "open(ready_path, 'w', encoding='utf-8').write('held')\n"
            "time.sleep(30)\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(fence), str(ready)],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        parent = None
        try:
            deadline = time.time() + 15
            while time.time() < deadline and not ready.exists():
                if proc.poll() is not None:
                    self.fail(proc.stderr.read().decode("utf-8", errors="replace"))
                time.sleep(0.05)
            self.assertTrue(ready.exists())
            parent = DeployEngine(
                root,
                service=ScriptedService(),
                lease=MemoryLease(),
                auth=MemoryAuth(),
                events=MemoryEvents(),
                contract_path=CONTRACT_PATH,
            )
            self.addCleanup(parent.close)
            self.assertFalse(parent.fence.release("base__host-test__web", 7, parent._engine_id))
            self.assertTrue(fence.is_file())
            self.assertEqual(json.loads(fence.read_text(encoding="utf-8"))["generation"], 7)
            current = parent.deployment("dep_w07")
            blocked = parent.recover_boot(
                replace(
                    make_request(),
                    deployment_id="dep_w07",
                    expected_revision=current["revision"],
                    fence_generation=current["fence_generation"],
                    idempotency_key="idem-release-held",
                )
            )
            self.assertEqual(blocked.state, "COMMIT")
            self.assertFalse(parent.deployment("dep_w07")["finalize"]["release_done"])
            self.assertTrue(fence.is_file())
            self.assertEqual(json.loads(fence.read_text(encoding="utf-8"))["generation"], 7)
            self.assertNotIn('"type":"lock_release"', parent.journal.log_path.read_text(encoding="utf-8"))
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        self.assertIsNotNone(parent)
        restored = json.loads(fence.read_text(encoding="utf-8"))
        restored["generation"] = stored["fence_generation"]
        fence.write_text(json.dumps(restored), encoding="utf-8")
        released = parent.recover_boot(
            replace(
                make_request(),
                deployment_id="dep_w07",
                expected_revision=parent.deployment("dep_w07")["revision"],
                fence_generation=stored["fence_generation"],
                idempotency_key="idem-release-proved",
            )
        )
        self.assertEqual(released.state, "COMMIT")
        proved = parent.deployment("dep_w07")
        self.assertTrue(proved["finalize"]["release_done"])
        self.assertFalse(fence.exists())
        self.assertTrue(parent.fence.proven_released("base__host-test__web", stored["fence_generation"]))
        self.assertIn('"type":"lock_release"', parent.journal.log_path.read_text(encoding="utf-8"))

    def test_recovery_edge_unknown_delivery_and_resume_scope(self):
        session = open_session(self)
        self._drive(session, "plan", "stage", "verify", "switch")
        calls = []
        real_append = session.events.append_deployment

        def append_and_lose_receipt(fact):
            calls.append(fact["event_id"])
            if fact.get("event_type") == "deployment.health" and session.events.facts == []:
                real_append(fact)
                raise RuntimeError("lost receipt")
            return real_append(fact)

        session.events.append_deployment = append_and_lose_receipt
        health = session.call("apply", "idem-w07-health")
        self.assertEqual(health.state, "HEALTH")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(session.events.facts), 1)
        unknown = [
            record
            for record in session.engine.journal.replay()["deliveries"].values()
            if record["status"] == "DELIVERY_UNKNOWN"
        ]
        self.assertEqual(len(unknown), 1)
        session.engine._submit_delivery(unknown[0])
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            session.engine.journal.replay()["deliveries"][unknown[0]["event_id"]]["status"],
            "DELIVERY_UNKNOWN",
        )

        def reconcile_missing(_fact):
            return None

        session.events.reconcile_deployment = reconcile_missing
        session.call("recover_boot", "idem-w07-reconcile-missing")
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            session.engine.journal.replay()["deliveries"][unknown[0]["event_id"]]["status"],
            "DELIVERY_UNKNOWN",
        )

        def reconcile_corrupt(fact):
            return {
                "outcome": "accepted",
                "payload_digest": "f" * 64,
                "event_id": fact.get("event_id"),
                "producer_id": fact.get("producer_id"),
                "stream_id": fact.get("stream_id"),
                "stream_epoch": fact.get("stream_epoch"),
                "producer_seq": fact.get("producer_seq"),
                "idempotency_key": fact.get("idempotency_key"),
                "app_id": fact.get("app_id"),
                "deployment_id": fact.get("deployment_id"),
                "receipt": {
                    "event_id": fact.get("event_id"),
                    "stream_id": fact.get("stream_id"),
                    "stream_epoch": fact.get("stream_epoch"),
                    "producer_seq": fact.get("producer_seq"),
                    "received_at": "server",
                    "fact_state": "ACCEPTED",
                },
            }

        session.events.reconcile_deployment = reconcile_corrupt
        session.call("recover_boot", "idem-w07-reconcile-corrupt")
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(
            session.engine.journal.replay()["deliveries"][unknown[0]["event_id"]]["status"],
            "DELIVERED",
        )

        fact = unknown[0]["fact"]
        receipt = {
            "event_id": fact["event_id"],
            "stream_id": fact["stream_id"],
            "stream_epoch": fact["stream_epoch"],
            "producer_seq": fact["producer_seq"],
            "received_at": "server",
            "fact_state": "ACCEPTED",
        }

        def reconcile_exact(item):
            proof = {
                "outcome": "accepted",
                "payload_digest": fact_digest(item),
                "idempotent": True,
                "receipt": dict(receipt),
            }
            for name in (
                "event_id",
                "producer_id",
                "stream_id",
                "stream_epoch",
                "producer_seq",
                "idempotency_key",
                "app_id",
                "deployment_id",
            ):
                proof[name] = item.get(name)
            return proof

        session.events.reconcile_deployment = reconcile_exact
        before_facts = len(session.events.facts)
        session.call("recover_boot", "idem-w07-reconcile-exact")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(session.events.facts), before_facts)
        self.assertEqual(
            session.engine.journal.replay()["deliveries"][fact["event_id"]]["status"],
            "DELIVERED",
        )

        fresh = open_session(self)
        self._drive(fresh, "plan", "stage", "verify", "switch")
        fresh_calls = []
        fresh_append = fresh.events.append_deployment

        def append_fail(fact):
            fresh_calls.append(fact["event_id"])
            raise RuntimeError("delivery down")

        fresh.events.append_deployment = append_fail
        fresh.call("apply", "idem-w07-health")
        self.assertEqual(fresh.events.facts, [])
        self.assertEqual(len(fresh_calls), 1)
        unknown_fact = [
            record["fact"]
            for record in fresh.engine.journal.replay()["deliveries"].values()
            if record["status"] == "DELIVERY_UNKNOWN"
        ][0]

        def reconcile_not_accepted(item):
            proof = {
                "outcome": "not_accepted",
                "payload_digest": fact_digest(item),
                "idempotent": True,
            }
            for name in (
                "event_id",
                "producer_id",
                "stream_id",
                "stream_epoch",
                "producer_seq",
                "idempotency_key",
                "app_id",
                "deployment_id",
            ):
                proof[name] = item.get(name)
            return proof

        def append_count(item):
            fresh_calls.append(item["event_id"])
            return fresh_append(item)

        fresh.events.append_deployment = append_count
        fresh.events.reconcile_deployment = reconcile_not_accepted
        fresh.call("recover_boot", "idem-w07-not-accepted")
        self.assertEqual(len(fresh_calls), 2)
        self.assertEqual(len(fresh.events.facts), 1)
        self.assertEqual(fresh.events.facts[0]["event_id"], unknown_fact["event_id"])
        self.assertEqual(
            fresh.engine.journal.replay()["deliveries"][unknown_fact["event_id"]]["status"],
            "DELIVERED",
        )

        rejected = open_session(self)
        rejected.events.reject_reason = "schema_rejected"
        self._drive(rejected, "plan", "stage", "verify", "switch")
        rejected.call("apply", "idem-w07-health")
        quarantined = [
            record
            for record in rejected.engine.journal.replay()["deliveries"].values()
            if record["event_type"] == "deployment.health"
        ]
        self.assertEqual(quarantined[-1]["status"], "QUARANTINED")
        rejected.engine._submit_delivery(quarantined[-1])
        self.assertEqual(rejected.events.facts, [])
        self.assertEqual(
            rejected.engine.journal.replay()["deliveries"][quarantined[-1]["event_id"]]["status"],
            "QUARANTINED",
        )

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        events = MemoryEvents()
        other = Session(
            root,
            ScriptedService(),
            MemoryLease(),
            MemoryAuth(),
            events,
            make_request(app_id="other", deployment_id="dep_other", service_id="api"),
        )
        self.addCleanup(other.close)
        for key in ("plan", "stage", "verify", "switch", "health", "pending"):
            method = "plan" if key == "plan" else "apply"
            self.assertEqual(other.call(method, f"idem-other-{key}").http, 200)
        other.engine.halt_at = "after_terminal_intent:COMMIT"
        with self.assertRaises(Interrupted):
            other.call("apply", "idem-other-commit")
        stored = other.engine.deployment("dep_other")
        self.assertEqual(stored["state"], "COMMIT")
        self.assertFalse(stored["finalize"]["release_done"])
        other_fence = root / "fence" / "other__host-test__api.json"
        self.assertTrue(other_fence.is_file())
        generation = stored["fence_generation"]
        revision = stored["revision"]
        other.close()

        base = Session(root, ScriptedService(), MemoryLease(), MemoryAuth(), events, make_request())
        self.addCleanup(base.close)
        planned = base.call("plan", "idem-base-plan")
        self.assertEqual(planned.state, "PLAN")
        self.assertFalse(base.engine.deployment("dep_other")["finalize"]["release_done"])
        self.assertTrue(other_fence.is_file())
        self.assertEqual(base.engine.fence.read("other__host-test__api")["generation"], generation)
        self.assertEqual([fact["event_type"] for fact in events.facts if fact["event_type"] == "deployment.commit"], [])
        base.call(
            "recover_boot",
            "idem-base-boot",
            deployment_id=base.deployment_id,
        )
        self.assertFalse(base.engine.deployment("dep_other")["finalize"]["release_done"])
        self.assertTrue(other_fence.is_file())
        base.auth.resume_scopes = set()
        denied = base.call(
            "recover_boot",
            "idem-other-denied",
            deployment_id="dep_other",
            app_id="other",
            service_id="api",
            expected_revision=revision,
            fence_generation=generation,
        )
        self.assertEqual(denied.state, "COMMIT")
        self.assertFalse(base.engine.deployment("dep_other")["finalize"]["release_done"])
        self.assertTrue(other_fence.is_file())
        self.assertEqual([fact["event_type"] for fact in events.facts if fact["event_type"] == "deployment.commit"], [])
        base.auth.resume_scopes = {("other", "dep_other")}
        allowed = base.call(
            "recover_boot",
            "idem-other-allowed",
            deployment_id="dep_other",
            app_id="other",
            service_id="api",
            expected_revision=revision,
            fence_generation=generation,
        )
        self.assertEqual(allowed.state, "COMMIT")
        self.assertTrue(base.engine.deployment("dep_other")["finalize"]["release_done"])
        self.assertFalse(other_fence.exists())
        commits = [fact for fact in events.facts if fact["event_type"] == "deployment.commit"]
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0]["app_id"], "other")


if __name__ == "__main__":
    unittest.main()
