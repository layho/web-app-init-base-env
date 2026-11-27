"""Isolated W07 deploy transaction and recovery.

These tests use a temporary directory and a synthetic service. They are not
MacCI, not a host deploy, and not production acceptance.
"""

import ast
import json
import tempfile
import threading
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
from app.control_deploy.ports import Effect


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
        self.force_allow = False

    def allow(self, run_id, token=TOKEN, generation=1):
        self.bindings.add((run_id, token, generation))

    def check(self, *, token, principal_id, run_id, scope, generation, server_now):
        if self.force_allow:
            return True
        if server_now >= self.expires_at or principal_id != self.principal or scope != self.scope:
            return False
        return (run_id, token, generation) in self.bindings


class MemoryAuth:
    def __init__(self):
        self.calls = []
        self.authenticated = True
        self.allow = True
        self.high_risk = False
        self.recovery = True

    def authenticate(self, actor):
        self.calls.append("authenticate")
        return self.authenticated

    def authorize(self, actor, scope, operation, candidate_id):
        self.calls.append("authorize")
        return self.allow

    def high_risk_allowed(self, actor, operation):
        self.calls.append("high_risk")
        return self.high_risk

    def recovery_authorized(self, actor, deployment_id):
        self.calls.append(("recovery", deployment_id))
        return self.recovery


class MemoryEvents:
    def __init__(self):
        self.facts = []
        self.fail = False

    def append_deployment(self, fact):
        if "received_at" in fact:
            raise ValueError("caller supplied received_at")
        if self.fail:
            raise RuntimeError("delivery down")
        stored = dict(fact)
        stored["received_at"] = "server"
        self.facts.append(stored)


class ScriptedService:
    real_host = False

    def __init__(self):
        self.calls = []
        self.queue = {}
        self.running = {
            "ok": True,
            "running_sha": OLD,
            "service_id": "web",
            "artifact_digest": digest_of(OLD_BYTES),
        }

    def _next(self, name, default):
        self.calls.append(name)
        pending = self.queue.get(name) or []
        if pending:
            return pending.pop(0)
        return default()

    def stage(self, spec):
        return self._next("stage", lambda: Effect("ok"))

    def verify(self, spec):
        return self._next("verify", lambda: Effect("ok"))

    def switch(self, spec):
        effect = self._next("switch", lambda: Effect("ok"))
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

        return self._next("health", default)

    def commit(self, spec):
        return self._next("commit", lambda: Effect("ok"))

    def rollback(self, spec):
        effect = self._next("rollback", lambda: Effect("ok", resources_removed=False))
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
        denied = session.call(
            "reconcile",
            "idem-w07-backfill-denied",
            observation={"status": "healthy", "public": {"ok": True, "running_sha": HEAD}},
        )
        self.assertEqual(denied.code, "EVIDENCE_MISSING")
        self.assertEqual(denied.state, "RECONCILE")
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
        expired = session.call("start", "idem-w07-expired", server_now=10_000)
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


if __name__ == "__main__":
    unittest.main()
