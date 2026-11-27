"""Deploy transaction runner.

States and guards come from the W00 contract fixture. Happy path:
PLAN -> STAGE -> VERIFY -> SWITCH -> HEALTH -> COMMIT_PENDING -> COMMIT.
Failure path: RECONCILE -> ROLLBACK -> VERIFY_OLD -> ROLLED_BACK, or
SAFE_FROZEN. An unknown external result is reconciled before any retry and
is never recorded as success.

The runner writes only under the directory it was given. It does not call a
host service manager, does not execute SQL, and does not enable production.
"""

import copy
import hashlib
import json
from dataclasses import dataclass, field

from app.control_deploy import (
    MANAGES_REAL_DEPLOY_ENTRY,
    PRODUCTION_ENABLEMENT,
    REAL_SERVICE_SWITCH,
)
from app.control_deploy.artifacts import FilesystemArtifacts, ImmutableConflict
from app.control_deploy.boot import evaluate_boot
from app.control_deploy.contract_machine import (
    RECONCILE_SOURCES,
    TERMINAL,
    ContractError,
    allows,
    candidate_id,
    digest_of,
    is_digest,
    is_sha,
    load_contract,
)
from app.control_deploy.health import (
    health_ok,
    identity_matches,
    misdiagnosed,
    no_change_ready,
    old_health_ready,
)
from app.control_deploy.journal import Fence, Journal
from app.control_deploy.migrations import check_migrations
from app.control_deploy.ports import Effect


SKIP_IDEMPOTENCY = {
    "NOT_AUTHORIZED",
    "POLICY_DENIED",
    "PRODUCTION_DB_FORBIDDEN",
    "LEASE_FENCE_REJECTED",
    "CONFLICT_REVISION",
    "IDEMPOTENCY_REPLAY",
    "IDEMPOTENCY_PAYLOAD_MISMATCH",
    "UNKNOWN_EXECUTOR",
}


class Interrupted(Exception):
    def __init__(self, point):
        super().__init__(point)
        self.point = point


@dataclass
class DeployResult:
    http: int
    code: str | None
    state: str | None = None
    deployment_id: str | None = None
    revision: int | None = None
    replayed: bool = False
    original_result: dict | None = None
    current_state: str | None = None
    lease_token: None = None
    fence_generation: int | None = None
    details: dict = field(default_factory=dict)


@dataclass
class Request:
    actor: dict
    principal_id: str
    scope: str
    expected_revision: int
    idempotency_key: str
    run_id: str
    lease_token: str
    lease_generation: int
    server_now: int = 1
    candidate: dict | None = None
    candidate_id: str | None = None
    artifact_digest: str | None = None
    artifact_bytes: bytes | None = None
    service_id: str | None = None
    app_id: str = "base"
    host_id: str = "host-test"
    install_kind: str = "upgrade"
    previous_healthy_sha: str | None = None
    previous_artifact_digest: str | None = None
    previous_artifact_bytes: bytes | None = None
    deployment_id: str | None = None
    no_change: bool = False
    running_sha: str | None = None
    migrations: dict | None = None
    apply_migrations: bool = False
    enable_production: bool = False
    real_switch: bool = False
    high_risk: bool = False
    recovers_deployment_id: str | None = None
    observation: dict | None = None
    hostops_pass_count: int | None = None
    takeover: bool = False
    fence_generation: int | None = None
    resources_removed: bool | None = None


class DeployEngine:
    def __init__(self, root, *, service, lease, auth, events=None, artifacts=None, contract_path=None):
        if PRODUCTION_ENABLEMENT or REAL_SERVICE_SWITCH or MANAGES_REAL_DEPLOY_ENTRY:
            raise ContractError("production enablement is disabled")
        self.journal = Journal(root)
        self.root = self.journal.root
        self.fence = Fence(self.root)
        self.artifacts = artifacts or FilesystemArtifacts(self.root)
        self.contract, self.edges, self.candidate_spec = load_contract(contract_path)
        self.service = service
        self.lease = lease
        self.auth = auth
        self.events = events
        self.halt_at = None
        self._engine_id = id(self)

    def close(self):
        self.fence.crash(self._engine_id)
        self.journal.close()

    def transitions(self, deployment_id):
        store = self.journal.replay()
        return [
            {
                "from": note["from_state"],
                "to": note["to_state"],
                "guard": note.get("guard"),
            }
            for note in store["notes"]
            if note.get("name") == "transition" and note.get("deployment_id") == deployment_id
        ]

    def deployment(self, deployment_id):
        return self._dep(deployment_id)

    def plan(self, request):
        with self.journal.exclusive():
            denied = self._gate(request, "plan")
            if denied:
                return denied
            replay = self._replay(request, "plan")
            if replay:
                return replay
            return self._finish(request, "plan", self._plan(request))

    def apply(self, request):
        with self.journal.exclusive():
            denied = self._gate(request, "apply")
            if denied:
                return denied
            replay = self._replay(request, "apply")
            if replay:
                return replay
            return self._finish(request, "apply", self._apply(request))

    def reconcile(self, request):
        with self.journal.exclusive():
            denied = self._gate(request, "reconcile")
            if denied:
                return denied
            replay = self._replay(request, "reconcile")
            if replay:
                return replay
            return self._finish(request, "reconcile", self._reconcile(request))

    def rollback(self, request):
        with self.journal.exclusive():
            denied = self._gate(request, "rollback")
            if denied:
                return denied
            replay = self._replay(request, "rollback")
            if replay:
                return replay
            return self._finish(request, "rollback", self._rollback(request))

    def verify_old(self, request):
        with self.journal.exclusive():
            denied = self._gate(request, "verify_old")
            if denied:
                return denied
            replay = self._replay(request, "verify_old")
            if replay:
                return replay
            return self._finish(request, "verify_old", self._verify_old(request))

    def start(self, request):
        with self.journal.exclusive():
            denied = self._gate(request, "start")
            if denied:
                return denied
            replay = self._replay(request, "start")
            if replay:
                return replay
            body = self._takeover(request) if request.takeover else self._start(request)
            return self._finish(request, "start", body)

    def recover_boot(self, request):
        with self.journal.exclusive():
            denied = self._gate(request, "recover_boot")
            if denied:
                return denied
            replay = self._replay(request, "recover_boot")
            if replay:
                return replay
            return self._finish(request, "recover_boot", self._recover_boot(request))

    def _plan(self, request):
        binding = self._validate_candidate(request)
        if binding:
            return binding
        if request.install_kind == "first_install":
            if request.previous_healthy_sha is not None or request.no_change:
                return self._result(
                    409,
                    "EVIDENCE_MISSING",
                    details={"record": "first_install_no_change", "update_no_change": 0},
                )
            if request.artifact_bytes is None:
                return self._result(409, "EVIDENCE_MISSING", details={"record": "artifact_missing"})
        elif request.install_kind == "upgrade":
            if not is_sha(request.previous_healthy_sha):
                return self._result(409, "EVIDENCE_MISSING", details={"record": "previous_sha_required"})
            if request.previous_artifact_bytes is None or digest_of(request.previous_artifact_bytes) != request.previous_artifact_digest:
                return self._result(409, "EVIDENCE_MISSING", details={"record": "previous_artifact_mismatch"})
            if request.no_change and not self._no_change_identity(request):
                return self._result(
                    409,
                    "EVIDENCE_MISSING",
                    details={"record": "no_change_identity", "update_no_change": 0},
                )
        else:
            return self._result(422, "SCHEMA_UNSUPPORTED", details={"record": "install_kind"})
        if request.migrations is not None:
            report = check_migrations(request.migrations)
            if not report["ok"]:
                return self._result(
                    409,
                    "EVIDENCE_MISSING",
                    details={**report, "record": "schema_blocked"},
                )
        store = self.journal.replay()
        if request.candidate_id in store["holds"]:
            return self._result(
                409,
                "EVIDENCE_MISSING",
                details={
                    "record": "update_held",
                    "held": True,
                    "candidate_id": request.candidate_id,
                    "reason": store["holds"][request.candidate_id],
                },
            )
        residual = self._inspect_residuals(request, store)
        if not residual["ok"]:
            return self._result(409, "EVIDENCE_MISSING", details=residual)
        key = self._fence_key(request.app_id, request.host_id, request.service_id)
        if key is None:
            return self._result(422, "SCHEMA_UNSUPPORTED", details={"record": "service_identity"})
        legacy = self.fence.read(key)
        if isinstance(legacy, dict) and legacy.get("legacy_directory"):
            return self._result(
                409,
                "EVIDENCE_MISSING",
                details={"record": "lock_unproven", "deleted": False},
            )
        active = self._active(store, request)
        frozen = [item for item in active if item["state"] == "SAFE_FROZEN"]
        running = [item for item in active if item["state"] != "SAFE_FROZEN"]
        if running:
            return self._result(409, "RESERVATION_CONFLICT", details={"record": "update_locked"})
        if len(frozen) > 1:
            return self._result(409, "EVIDENCE_MISSING", details={"record": "multiple_frozen"})
        deployment_id = request.deployment_id or f"dep_{len(store['deployments']) + 1}"
        if deployment_id in store["deployments"]:
            return self._result(409, "CONFLICT_REVISION", details={"record": "deployment_exists"})
        if frozen:
            if request.recovers_deployment_id != frozen[0]["deployment_id"]:
                return self._result(409, "RESERVATION_CONFLICT", details={"record": "frozen_occupies_service"})
            if not self.auth.recovery_authorized(request.actor, frozen[0]["deployment_id"]):
                return self._result(403, "POLICY_DENIED", details={"record": "recovery_not_authorized"})
            current = self.fence.read(key)
            taken = self.fence.takeover(
                key,
                request.run_id,
                deployment_id,
                current["generation"],
                self._engine_id,
            )
            if not taken["ok"]:
                return self._result(taken.get("http", 409), taken["code"], details={"record": taken.get("record")})
            lock = taken["lock"]
        else:
            taken = self.fence.acquire(key, request.run_id, deployment_id, self._engine_id)
            if not taken["ok"]:
                return self._result(409, taken["code"], details={"record": taken.get("record")})
            lock = taken["lock"]
        self.journal.commit({"type": "lock", "lock": lock})
        if request.install_kind == "upgrade":
            self.artifacts.put(request.previous_artifact_digest, request.previous_artifact_bytes)
            inventory = [
                {
                    "service_id": request.service_id,
                    "running_sha": request.previous_healthy_sha,
                    "artifact_digest": request.previous_artifact_digest,
                    "app_id": request.app_id,
                    "host_id": request.host_id,
                }
            ]
        else:
            inventory = []
        self.journal.commit({"type": "inventory", "items": inventory})
        deployment = {
            "deployment_id": deployment_id,
            "revision": 1,
            "state": "PLAN",
            "install_kind": request.install_kind,
            "previous_healthy_sha": request.previous_healthy_sha,
            "previous_artifact_digest": request.previous_artifact_digest,
            "candidate": {
                "repo_id": request.candidate["repo_id"],
                "task_id": request.candidate["task_id"],
                "head": request.candidate["head"],
                "base": request.candidate["base"],
                "merge": request.candidate["merge"],
                "tree": request.candidate["tree"],
            },
            "candidate_id": request.candidate_id,
            "artifact_digest": request.artifact_digest,
            "service_id": request.service_id,
            "app_id": request.app_id,
            "host_id": request.host_id,
            "running_sha": request.running_sha,
            "fence_generation": lock["generation"],
            "owner_run_id": request.run_id,
            "recovers_deployment_id": request.recovers_deployment_id,
            "pending": None,
            "local_health": None,
            "public_observation": None,
            "health_checked_before_no_change": False,
            "commit_ack": None,
            "failure_class": None,
            "failure_reason": None,
            "restore_ok": False,
            "inventory": inventory,
            "no_change_recorded": False,
            "update_no_change": 0,
            "schema_executed": False,
            "production_enablement": False,
        }
        self._put(deployment)
        if request.no_change:
            return self._no_change(self._dep(deployment_id), request)
        return self._result(200, None, self._dep(deployment_id), residuals_kept=residual["kept"])

    def _no_change(self, deployment, request):
        self._note(
            name="health_check_started",
            deployment_id=deployment["deployment_id"],
            before_no_change=True,
        )
        effect = self.service.health(self._spec(deployment))
        deployment = self._dep(deployment["deployment_id"])
        deployment["local_health"] = effect.local
        deployment["public_observation"] = effect.public
        deployment["health_checked_before_no_change"] = True
        if effect.status == "unknown":
            deployment["pending"] = effect.as_pending("health")
            deployment["update_no_change"] = 0
            deployment = self._put(deployment)
            return self._result(409, "SIDE_EFFECT_UNKNOWN", deployment, update_no_change=0)
        deployment["update_no_change"] = 0
        deployment["no_change_recorded"] = False
        deployment = self._put(deployment)
        if effect.status != "ok" or not no_change_ready(deployment):
            self._note(name="no_change_rejected", deployment_id=deployment["deployment_id"])
            return self._result(
                409,
                "EVIDENCE_MISSING",
                deployment,
                record="no_change_unhealthy",
                update_no_change=0,
                no_change_recorded=False,
            )
        try:
            self.artifacts.bind(
                deployment["artifact_digest"],
                deployment["candidate_id"],
                deployment["service_id"],
            )
        except ImmutableConflict:
            return self._result(409, "CANDIDATE_STALE", deployment, record="artifact_binding")
        deployment = self._dep(deployment["deployment_id"])
        deployment["commit_ack"] = self._ack(deployment, no_change=True)
        deployment["no_change_recorded"] = True
        deployment["update_no_change"] = 1
        deployment["running_sha"] = deployment["previous_healthy_sha"]
        deployment = self._put(deployment)
        deployment = self._transition(
            deployment,
            "PLAN",
            "COMMIT",
            "no_change_and_healthy_and_previous_sha",
        )
        self._note(name="update_no_change", deployment_id=deployment["deployment_id"])
        return self._result(200, None, deployment, update_no_change=1, no_change_recorded=True)

    def _apply(self, request):
        located = self._locate(request)
        if isinstance(located, DeployResult):
            return located
        deployment = located
        pending = deployment.get("pending") or {}
        if pending.get("phase") == "unknown":
            return self._enter_reconcile_for_unknown(deployment)
        if deployment["failure_class"] == "deterministic" and deployment["state"] == "PLAN":
            return self._result(
                409,
                "EVIDENCE_MISSING",
                deployment,
                record="update_held",
                held=True,
            )
        state = deployment["state"]
        if state == "PLAN":
            return self._apply_stage(deployment, request)
        if state == "STAGE":
            return self._apply_verify(deployment)
        if state == "VERIFY":
            return self._apply_switch(deployment)
        if state == "SWITCH":
            return self._apply_health(deployment)
        if state == "HEALTH":
            return self._apply_commit_pending(deployment)
        if state == "COMMIT_PENDING":
            return self._apply_commit(deployment)
        return self._result(409, "EVIDENCE_MISSING", deployment, record="terminal" if state in TERMINAL else "not_applicable")

    def _apply_stage(self, deployment, request):
        if deployment.get("failure_class") == "transient":
            deployment["failure_class"] = None
            deployment["failure_reason"] = None
            deployment = self._put(deployment)
        if request.artifact_bytes is None:
            return self._result(409, "EVIDENCE_MISSING", deployment, record="artifact_missing")
        effect = self._external(deployment, "stage", lambda: self.service.stage(self._spec(deployment)))
        deployment = self._dep(deployment["deployment_id"])
        if effect is None or effect.status == "unknown":
            return self._result(409, "SIDE_EFFECT_UNKNOWN", deployment, record="stage_unknown")
        if effect.status != "ok":
            return self._fail_open(deployment, effect, "stage_failed")
        try:
            self.artifacts.put(deployment["artifact_digest"], request.artifact_bytes)
            self.artifacts.bind(
                deployment["artifact_digest"],
                deployment["candidate_id"],
                deployment["service_id"],
            )
        except ImmutableConflict:
            return self._fail_open(deployment, Effect("failed", "deterministic", "immutable"), "artifact_conflict")
        if not self._bytes_match(deployment["artifact_digest"]):
            return self._fail_open(deployment, Effect("failed", "deterministic", "digest"), "artifact_mismatch")
        deployment = self._dep(deployment["deployment_id"])
        deployment["pending"] = None
        deployment = self._put(deployment)
        deployment = self._transition(deployment, "PLAN", "STAGE", None)
        return self._result(200, None, deployment)

    def _apply_verify(self, deployment):
        if not self._bytes_match(deployment["artifact_digest"]):
            return self._fail_closed(deployment, Effect("failed", "deterministic", "artifact_mismatch"), "artifact_mismatch", hold=True)
        effect = self._external(deployment, "verify", lambda: self.service.verify(self._spec(deployment)))
        deployment = self._dep(deployment["deployment_id"])
        if effect is None or effect.status == "unknown":
            deployment = self._transition(deployment, "STAGE", "RECONCILE", None, keep_pending=True)
            return self._result(409, "SIDE_EFFECT_UNKNOWN", deployment)
        if effect.status != "ok":
            return self._fail_closed(
                deployment,
                effect,
                "verify_failed",
                hold=self._classification(effect) == "deterministic",
            )
        deployment["pending"] = None
        deployment = self._put(deployment)
        deployment = self._transition(deployment, "STAGE", "VERIFY", None)
        return self._result(200, None, deployment)

    def _apply_switch(self, deployment):
        effect = self._external(deployment, "switch", lambda: self.service.switch(self._spec(deployment)))
        deployment = self._dep(deployment["deployment_id"])
        if effect is None or effect.status == "unknown":
            deployment = self._transition(deployment, "VERIFY", "RECONCILE", None, keep_pending=True)
            return self._result(409, "SIDE_EFFECT_UNKNOWN", deployment, record="switch_unknown")
        if effect.status != "ok":
            return self._fail_closed(
                deployment,
                effect,
                "switch_failed",
                hold=self._classification(effect) == "deterministic",
            )
        deployment["running_sha"] = deployment["candidate"]["head"]
        deployment["pending"] = None
        deployment = self._put(deployment)
        deployment = self._transition(deployment, "VERIFY", "SWITCH", None)
        return self._result(200, None, deployment)

    def _apply_health(self, deployment):
        effect = self._external(deployment, "health", lambda: self.service.health(self._spec(deployment)))
        deployment = self._dep(deployment["deployment_id"])
        if effect is None or effect.status == "unknown":
            deployment = self._transition(deployment, "SWITCH", "RECONCILE", None, keep_pending=True)
            return self._result(409, "SIDE_EFFECT_UNKNOWN", deployment, record="health_unknown")
        if effect.status != "ok":
            deployment["local_health"] = effect.local
            deployment["public_observation"] = effect.public
            deployment["failure_class"] = "health_failed"
            deployment["failure_reason"] = "health_failed"
            deployment["pending"] = None
            deployment = self._put(deployment)
            deployment = self._transition(deployment, "SWITCH", "RECONCILE", None)
            return self._result(
                409,
                "EVIDENCE_MISSING",
                deployment,
                record="health_failed",
                public_used_as_health=False,
            )
        deployment["local_health"] = effect.local
        deployment["public_observation"] = effect.public
        deployment["pending"] = None
        deployment = self._put(deployment)
        deployment = self._transition(deployment, "SWITCH", "HEALTH", None)
        self._emit("deployment.health", deployment)
        if health_ok(deployment):
            return self._result(200, None, deployment, public_used_as_health=False)
        reason = "health_misdiagnosis" if misdiagnosed(deployment) else "health_failed"
        deployment["failure_class"] = reason
        deployment["failure_reason"] = reason
        deployment = self._put(deployment)
        deployment = self._transition(deployment, "HEALTH", "RECONCILE", None)
        return self._result(
            409,
            "EVIDENCE_MISSING",
            deployment,
            record=reason,
            public_used_as_health=False,
        )

    def _apply_commit_pending(self, deployment):
        if not health_ok(deployment):
            deployment["failure_class"] = "health_failed"
            deployment["failure_reason"] = "health_not_proved"
            deployment = self._put(deployment)
            deployment = self._transition(deployment, "HEALTH", "RECONCILE", None)
            return self._result(409, "EVIDENCE_MISSING", deployment, record="health_not_proved")
        deployment = self._transition(deployment, "HEALTH", "COMMIT_PENDING", "health_ok")
        return self._result(200, None, deployment)

    def _apply_commit(self, deployment):
        if deployment.get("commit_ack") and deployment["commit_ack"].get("durable"):
            deployment = self._transition(deployment, "COMMIT_PENDING", "COMMIT", "commit_ack_durable")
            return self._result(200, None, deployment, record="commit_ack_durable")
        effect = self._external(deployment, "commit", lambda: self.service.commit(self._spec(deployment)))
        deployment = self._dep(deployment["deployment_id"])
        if effect is None or effect.status == "unknown":
            deployment["failure_reason"] = "commit_ack_lost"
            deployment = self._put(deployment)
            deployment = self._transition(
                deployment,
                "COMMIT_PENDING",
                "RECONCILE",
                "commit_ack_lost",
                keep_pending=True,
            )
            return self._result(409, "SIDE_EFFECT_UNKNOWN", deployment, record="commit_ack_lost")
        if effect.status != "ok":
            deployment["failure_class"] = self._classification(effect)
            deployment["failure_reason"] = "commit_failed"
            deployment["pending"] = None
            deployment = self._put(deployment)
            return self._result(409, "EVIDENCE_MISSING", deployment, record="commit_failed")
        deployment["commit_ack"] = self._ack(deployment, no_change=False)
        deployment["pending"] = None
        deployment = self._put(deployment)
        self._halt("after_ack:commit")
        deployment = self._transition(deployment, "COMMIT_PENDING", "COMMIT", "commit_ack_durable")
        return self._result(200, None, deployment, record="commit_ack_durable")

    def _reconcile(self, request):
        deployment = self._dep(request.deployment_id) if request.deployment_id else None
        if deployment is None:
            return self._result(409, "EVIDENCE_MISSING", details={"record": "deployment_missing"})
        owned = self._owned(deployment, request)
        if isinstance(owned, DeployResult):
            return owned
        if deployment["state"] in TERMINAL:
            return self._result(409, "EVIDENCE_MISSING", deployment, record="terminal")
        pending = deployment.get("pending") or {}
        if deployment["state"] == "PLAN" and pending.get("phase") == "unknown":
            return self._reconcile_plan(deployment, request)
        observation_status = (request.observation or {}).get("status")
        enter = pending.get("phase") == "unknown" or observation_status in {"unknown", "failed"}
        if deployment["state"] in RECONCILE_SOURCES and enter:
            guard = "commit_ack_lost" if deployment["state"] == "COMMIT_PENDING" else None
            if deployment["state"] == "COMMIT_PENDING" and guard is None:
                return self._result(409, "EVIDENCE_MISSING", deployment)
            if not allows(self.edges, deployment["state"], "RECONCILE", guard):
                raise ContractError("missing reconcile edge")
            if deployment["state"] != "RECONCILE":
                deployment = self._transition(deployment, deployment["state"], "RECONCILE", guard, keep_pending=True)
            if request.observation is None:
                return self._result(200, "SIDE_EFFECT_UNKNOWN", deployment, record="reconcile_required")
        if deployment["state"] != "RECONCILE":
            deployment = self._dep(deployment["deployment_id"])
        if deployment["state"] != "RECONCILE":
            return self._result(409, "EVIDENCE_MISSING", deployment, record="not_reconciling")
        observation = request.observation
        if observation is None:
            return self._result(200, "SIDE_EFFECT_UNKNOWN", deployment, record="reconcile_required")
        status = observation.get("status")
        if status == "unknown":
            deployment["failure_reason"] = "outcome_unknown"
            deployment["failure_class"] = "unknown"
            deployment["pending"] = None
            deployment = self._put(deployment)
            deployment = self._transition(deployment, "RECONCILE", "SAFE_FROZEN", "outcome_unknown")
            return self._frozen(deployment, "outcome_unknown")
        if status == "healthy":
            return self._backfill(deployment, observation)
        if status == "failed":
            deployment["failure_class"] = observation.get("classification") or "deterministic"
            deployment["failure_reason"] = observation.get("reason") or "reconcile_failed"
            deployment["pending"] = None
            deployment = self._put(deployment)
            return self._result(200, None, deployment, record="reconcile_classified")
        return self._result(409, "SIDE_EFFECT_UNKNOWN", deployment, record="observation_unclassified")

    def _reconcile_plan(self, deployment, request):
        observation = request.observation or {}
        status = observation.get("status")
        body = self.artifacts.read(deployment["artifact_digest"])
        present = body is not None and digest_of(body) == deployment["artifact_digest"]
        if status == "absent" and not present:
            deployment["pending"] = None
            deployment = self._put(deployment)
            return self._result(200, None, deployment, record="effect_absent")
        if status == "present" and present:
            try:
                self.artifacts.bind(
                    deployment["artifact_digest"],
                    deployment["candidate_id"],
                    deployment["service_id"],
                )
            except ImmutableConflict:
                return self._result(409, "CANDIDATE_STALE", deployment, record="artifact_binding")
            deployment["pending"] = None
            deployment = self._put(deployment)
            deployment = self._transition(deployment, "PLAN", "STAGE", None)
            return self._result(200, None, deployment, record="stage_observed")
        return self._result(409, "SIDE_EFFECT_UNKNOWN", deployment, record="stage_unknown")

    def _backfill(self, deployment, observation):
        local = observation.get("local")
        if local is None or local.get("ok") is not True or not identity_matches(
            local,
            deployment["candidate"]["head"],
            deployment["service_id"],
            deployment["artifact_digest"],
        ):
            return self._result(409, "EVIDENCE_MISSING", deployment, record="reconcile_without_health")
        deployment["local_health"] = local
        deployment["public_observation"] = observation.get("public")
        if not health_ok(deployment):
            return self._result(409, "EVIDENCE_MISSING", deployment, record="reconcile_without_health")
        deployment["commit_ack"] = self._ack(deployment, backfill=True)
        deployment["pending"] = None
        deployment["running_sha"] = deployment["candidate"]["head"]
        deployment = self._put(deployment)
        deployment = self._transition(
            deployment,
            "RECONCILE",
            "COMMIT",
            "reconcile_confirms_healthy_and_backfill_commit",
        )
        return self._result(200, None, deployment, record="commit_backfill")

    def _rollback(self, request):
        deployment = self._dep(request.deployment_id) if request.deployment_id else None
        if deployment is None:
            return self._result(409, "EVIDENCE_MISSING")
        owned = self._owned(deployment, request)
        if isinstance(owned, DeployResult):
            return owned
        if deployment["state"] in TERMINAL:
            return self._result(409, "EVIDENCE_MISSING", deployment, record="terminal")
        pending = deployment.get("pending") or {}
        if deployment["state"] == "RECONCILE":
            if pending.get("phase") == "unknown" or deployment.get("failure_class") in (None, "unknown"):
                return self._result(409, "SIDE_EFFECT_UNKNOWN", deployment, record="classify_before_rollback")
            deployment = self._transition(deployment, "RECONCILE", "ROLLBACK", None)
        if deployment["state"] != "ROLLBACK":
            return self._result(409, "EVIDENCE_MISSING", deployment, record="not_in_rollback")
        if deployment["install_kind"] == "first_install":
            return self._rollback_first(deployment, request)
        return self._rollback_upgrade(deployment)

    def _rollback_first(self, deployment, request):
        if deployment.get("previous_healthy_sha") is not None:
            deployment["previous_healthy_sha"] = None
            deployment = self._put(deployment)
        effect = self._external(
            deployment,
            "rollback",
            lambda: self.service.rollback(self._spec(deployment)),
        )
        deployment = self._dep(deployment["deployment_id"])
        if effect is None or effect.status == "unknown":
            return self._result(409, "SIDE_EFFECT_UNKNOWN", deployment, record="rollback_unknown")
        removed = effect.resources_removed is True and request.resources_removed is True
        if not removed or effect.status != "ok":
            deployment["failure_reason"] = "removal_unproven"
            deployment["pending"] = None
            deployment["previous_healthy_sha"] = None
            deployment = self._put(deployment)
            deployment = self._transition(deployment, "ROLLBACK", "SAFE_FROZEN", "rollback_failed")
            return self._frozen(deployment, "removal_unproven")
        deployment["pending"] = None
        deployment["previous_healthy_sha"] = None
        deployment = self._put(deployment)
        deployment = self._transition(
            deployment,
            "ROLLBACK",
            "ROLLED_BACK",
            "first_install_resources_removed",
        )
        return self._result(200, None, deployment, record="first_install_resources_removed")

    def _rollback_upgrade(self, deployment):
        if deployment.get("restore_ok"):
            return self._result(200, None, deployment, record="restore_recorded")
        body = self.artifacts.read(deployment["previous_artifact_digest"])
        if body is None or digest_of(body) != deployment["previous_artifact_digest"]:
            deployment["failure_reason"] = "old_artifact_corrupt"
            deployment = self._put(deployment)
            deployment = self._transition(deployment, "ROLLBACK", "SAFE_FROZEN", "rollback_failed")
            return self._frozen(deployment, "old_artifact_corrupt")
        effect = self._external(
            deployment,
            "rollback",
            lambda: self.service.rollback(self._spec(deployment)),
        )
        deployment = self._dep(deployment["deployment_id"])
        if effect is None or effect.status == "unknown":
            return self._result(409, "SIDE_EFFECT_UNKNOWN", deployment, record="rollback_unknown")
        if effect.status != "ok":
            deployment["failure_reason"] = "rollback_failed"
            deployment["pending"] = None
            deployment = self._put(deployment)
            deployment = self._transition(deployment, "ROLLBACK", "SAFE_FROZEN", "rollback_failed")
            return self._frozen(deployment, "rollback_failed")
        deployment["restore_ok"] = True
        deployment["running_sha"] = deployment["previous_healthy_sha"]
        deployment["pending"] = None
        deployment = self._put(deployment)
        return self._result(200, None, deployment, record="restore_recorded")

    def _verify_old(self, request):
        deployment = self._dep(request.deployment_id) if request.deployment_id else None
        if deployment is None:
            return self._result(409, "EVIDENCE_MISSING")
        owned = self._owned(deployment, request)
        if isinstance(owned, DeployResult):
            return owned
        if deployment["state"] in TERMINAL:
            return self._result(200, None, deployment, record="terminal")
        if deployment["install_kind"] == "first_install":
            return self._result(
                409,
                "EVIDENCE_MISSING",
                deployment,
                record="old_health_is_not_removal_proof",
            )
        if deployment["state"] != "ROLLBACK" or not deployment.get("restore_ok"):
            return self._result(409, "EVIDENCE_MISSING", deployment, record="restore_not_recorded")
        if (deployment.get("pending") or {}).get("phase") == "unknown":
            return self._result(409, "SIDE_EFFECT_UNKNOWN", deployment)
        deployment = self._transition(deployment, "ROLLBACK", "VERIFY_OLD", None)
        body = self.artifacts.read(deployment["previous_artifact_digest"])
        disk = digest_of(body) if isinstance(body, bytes) else None
        effect = self.service.health(self._previous_spec(deployment))
        deployment = self._dep(deployment["deployment_id"])
        deployment["local_health"] = effect.local
        deployment["public_observation"] = effect.public
        deployment = self._put(deployment)
        if effect.status == "ok" and old_health_ready(deployment, disk):
            deployment = self._transition(deployment, "VERIFY_OLD", "ROLLED_BACK", "old_health_proved")
            return self._result(200, None, deployment, record="old_health_proved")
        deployment["failure_reason"] = "old_health_unproven"
        deployment = self._put(deployment)
        deployment = self._transition(deployment, "VERIFY_OLD", "SAFE_FROZEN", "old_health_unproven")
        return self._frozen(deployment, "old_health_unproven")

    def _start(self, request):
        deployment = self._dep(request.deployment_id) if request.deployment_id else None
        if deployment is None:
            return self._result(409, "EVIDENCE_MISSING", details={"record": "deployment_missing"})
        if request.run_id != deployment["owner_run_id"] or request.fence_generation != deployment["fence_generation"]:
            return self._result(409, "LEASE_FENCE_REJECTED", deployment, record="stale_fence")
        key = self._fence_key(deployment["app_id"], deployment["host_id"], deployment["service_id"])
        if not self.fence.reclaim(key, request.run_id, deployment["fence_generation"], self._engine_id):
            return self._result(409, "LEASE_FENCE_REJECTED", deployment, record="fence_busy")
        if (deployment.get("pending") or {}).get("phase") == "unknown":
            return self._result(409, "SIDE_EFFECT_UNKNOWN", deployment, started=False)
        return self._result(200, None, deployment, started=True)

    def _takeover(self, request):
        deployment = self._dep(request.deployment_id) if request.deployment_id else None
        if deployment is None:
            return self._result(409, "EVIDENCE_MISSING")
        if deployment["state"] in TERMINAL:
            return self._result(409, "EVIDENCE_MISSING", deployment, record="terminal")
        key = self._fence_key(deployment["app_id"], deployment["host_id"], deployment["service_id"])
        current = self.fence.read(key)
        if not isinstance(current, dict) or current.get("legacy_directory") or "generation" not in current:
            return self._result(409, "EVIDENCE_MISSING", deployment, record="lock_unproven")
        taken = self.fence.takeover(
            key,
            request.run_id,
            deployment["deployment_id"],
            current["generation"],
            self._engine_id,
        )
        if not taken["ok"]:
            return self._result(409, taken["code"], deployment, record=taken.get("record"))
        self.journal.commit({"type": "lock", "lock": taken["lock"]})
        deployment["fence_generation"] = taken["lock"]["generation"]
        deployment["owner_run_id"] = request.run_id
        deployment = self._put(deployment)
        if (deployment.get("pending") or {}).get("phase") == "unknown":
            return self._result(409, "SIDE_EFFECT_UNKNOWN", deployment, record="adopted_unknown")
        return self._result(200, None, deployment, record="adopted")

    def _recover_boot(self, request):
        deployment = self._dep(request.deployment_id) if request.deployment_id else None
        if deployment is None:
            return self._result(409, "EVIDENCE_MISSING", details={"record": "deployment_missing", "boot_recovery_proved": False})
        before = (deployment["state"], deployment["revision"])
        store = self.journal.replay()
        key = self._fence_key(deployment["app_id"], deployment["host_id"], deployment["service_id"])
        lock = self.fence.read(key)
        if isinstance(lock, dict) and lock.get("legacy_directory"):
            lock = None
        if deployment["state"] == "COMMIT":
            observed_lock = None
        else:
            observed_lock = lock
        effect = self.service.observe(self._spec(deployment))
        report = evaluate_boot(
            deployment=deployment,
            journal_seq=store["seq"],
            lock=observed_lock,
            artifact_body=self.artifacts.read(deployment["artifact_digest"]),
            service_local=effect.local if isinstance(effect, Effect) else None,
            inventory=store["host_inventory"],
            migration_executed=deployment.get("schema_executed") is True,
            production_enablement=PRODUCTION_ENABLEMENT,
            hostops_pass_count=request.hostops_pass_count,
        )
        self._note(
            name="boot_chain",
            deployment_id=deployment["deployment_id"],
            links=report["links"],
            proved=report["boot_recovery_proved"],
            hostops_pass_count=request.hostops_pass_count,
        )
        after = self._dep(deployment["deployment_id"])
        if (after["state"], after["revision"]) != before:
            raise ContractError("boot recovery mutated the deployment")
        report["state_unchanged"] = True
        return self._result(200, None, after, **report)

    def _enter_reconcile_for_unknown(self, deployment):
        if deployment["state"] == "RECONCILE":
            return self._result(409, "SIDE_EFFECT_UNKNOWN", deployment, record="reconcile_required")
        if deployment["state"] == "PLAN":
            return self._result(409, "SIDE_EFFECT_UNKNOWN", deployment, record="stage_unknown")
        if deployment["state"] not in RECONCILE_SOURCES:
            return self._result(409, "SIDE_EFFECT_UNKNOWN", deployment)
        guard = "commit_ack_lost" if deployment["state"] == "COMMIT_PENDING" else None
        deployment = self._transition(deployment, deployment["state"], "RECONCILE", guard, keep_pending=True)
        return self._result(409, "SIDE_EFFECT_UNKNOWN", deployment, record="reconcile_required")

    def _fail_open(self, deployment, effect, reason):
        classification = self._classification(effect)
        deployment = self._dep(deployment["deployment_id"])
        deployment["failure_class"] = classification
        deployment["failure_reason"] = reason
        deployment["pending"] = None
        deployment = self._put(deployment)
        held = classification == "deterministic"
        if held:
            self._hold(deployment["candidate_id"], reason)
        return self._result(
            409,
            "EVIDENCE_MISSING",
            deployment,
            record="update_held" if held else reason,
            held=held,
            failure_class=classification,
        )

    def _fail_closed(self, deployment, effect, reason, hold):
        classification = self._classification(effect)
        source = deployment["state"]
        deployment = self._dep(deployment["deployment_id"])
        deployment["failure_class"] = classification
        deployment["failure_reason"] = reason
        deployment["pending"] = None
        deployment = self._put(deployment)
        if hold and classification == "deterministic":
            self._hold(deployment["candidate_id"], reason)
        deployment = self._transition(deployment, source, "RECONCILE", None)
        return self._result(
            409,
            "EVIDENCE_MISSING",
            deployment,
            record=reason,
            held=hold and classification == "deterministic",
            failure_class=classification,
        )

    def _frozen(self, deployment, reason):
        log_text = self.journal.log_path.read_text(encoding="utf-8") if self.journal.log_path.exists() else ""
        backup = True
        if deployment.get("previous_artifact_digest"):
            backup = self.artifacts.read(deployment["previous_artifact_digest"]) is not None
        self._note(name="journal_preserved", deployment_id=deployment["deployment_id"], reason=reason)
        return self._result(
            200,
            None,
            deployment,
            record=reason,
            journal_preserved=deployment["deployment_id"] in log_text,
            backup_preserved=backup,
        )

    def _external(self, deployment, action, call):
        pending = deployment.get("pending") or {}
        if pending.get("action") == action and pending.get("phase") == "unknown":
            return None
        if pending.get("action") == action and pending.get("phase") == "result":
            return Effect(
                pending["status"],
                classification=pending.get("classification") or "none",
                detail=pending.get("detail") or "",
                local=pending.get("local"),
                public=pending.get("public"),
                resources_removed=pending.get("resources_removed"),
            )
        if not (pending.get("action") == action and pending.get("phase") == "intent"):
            deployment["pending"] = {"action": action, "phase": "intent"}
            deployment = self._put(deployment)
            self._halt(f"after_intent:{action}")
        effect = call()
        if not isinstance(effect, Effect):
            raise ContractError("service port did not return an effect")
        deployment = self._dep(deployment["deployment_id"])
        deployment["pending"] = effect.as_pending(action)
        self._put(deployment)
        self._halt(f"after_result:{action}")
        return effect

    def _locate(self, request):
        deployment = self._dep(request.deployment_id) if request.deployment_id else None
        if deployment is None:
            return self._result(409, "EVIDENCE_MISSING", details={"record": "deployment_missing"})
        if request.candidate_id != deployment["candidate_id"]:
            return self._result(409, "CANDIDATE_STALE", deployment, record="candidate_binding")
        if request.artifact_digest != deployment["artifact_digest"]:
            return self._result(409, "CANDIDATE_STALE", deployment, record="artifact_binding")
        if request.service_id != deployment["service_id"]:
            return self._result(409, "CANDIDATE_STALE", deployment, record="service_binding")
        if request.expected_revision != deployment["revision"]:
            return self._result(409, "CONFLICT_REVISION", deployment)
        return self._owned(deployment, request)

    def _owned(self, deployment, request):
        if request.expected_revision != deployment["revision"]:
            return self._result(409, "CONFLICT_REVISION", deployment)
        if request.run_id != deployment["owner_run_id"] or request.fence_generation != deployment["fence_generation"]:
            return self._result(409, "LEASE_FENCE_REJECTED", deployment, record="stale_fence")
        if deployment["state"] in ("COMMIT", "ROLLED_BACK"):
            return deployment
        key = self._fence_key(deployment["app_id"], deployment["host_id"], deployment["service_id"])
        if deployment["state"] == "SAFE_FROZEN":
            return deployment
        if not self.fence.reclaim(key, request.run_id, deployment["fence_generation"], self._engine_id):
            return self._result(409, "LEASE_FENCE_REJECTED", deployment, record="fence_busy")
        return deployment

    def _gate(self, request, operation):
        if self.service is None or self.lease is None or self.auth is None:
            return self._result(409, "UNKNOWN_EXECUTOR")
        if getattr(self.service, "real_host", False):
            return self._result(403, "POLICY_DENIED", details={"record": "real_service_rejected", "production_enablement": False})
        actor = request.actor or {}
        well_formed = actor.get("kind") == "test" and str(actor.get("proof_ref", "")).startswith("test-proof://")
        authenticated = bool(self.auth.authenticate(actor)) if well_formed else False
        if not well_formed or not authenticated:
            return self._result(403, "NOT_AUTHORIZED", details={"production_enablement": False})
        if not self.auth.authorize(actor, request.scope, operation, request.candidate_id):
            return self._result(403, "POLICY_DENIED", details={"production_enablement": False})
        if request.apply_migrations:
            self.auth.high_risk_allowed(actor, operation)
            return self._result(
                403,
                "PRODUCTION_DB_FORBIDDEN",
                details={"executed": False, "production_enablement": False},
            )
        if request.enable_production or request.real_switch or request.high_risk:
            self.auth.high_risk_allowed(actor, operation)
            return self._result(
                403,
                "POLICY_DENIED",
                details={"record": "production_disabled", "production_enablement": False},
            )
        if not request.lease_token or request.lease_token == str(request.lease_generation):
            return self._result(409, "LEASE_FENCE_REJECTED", details={"record": "generation_is_not_a_token"})
        allowed = self.lease.check(
            token=request.lease_token,
            principal_id=request.principal_id,
            run_id=request.run_id,
            scope=request.scope,
            generation=request.lease_generation,
            server_now=request.server_now,
        )
        if not allowed:
            return self._result(409, "LEASE_FENCE_REJECTED")
        return None

    def _validate_candidate(self, request):
        candidate = request.candidate or {}
        fields = ("repo_id", "task_id", "head", "base", "merge", "tree")
        if any(not isinstance(candidate.get(name), str) or not candidate.get(name) for name in fields):
            return self._result(409, "CANDIDATE_STALE", details={"record": "candidate_incomplete"})
        for name in ("head", "base", "merge", "tree"):
            if not is_sha(candidate[name]):
                return self._result(409, "CANDIDATE_STALE", details={"record": "sha_shape"})
        expected = candidate_id(
            self.candidate_spec,
            candidate["repo_id"],
            candidate["task_id"],
            candidate["head"],
            candidate["base"],
            candidate["merge"],
            candidate["tree"],
        )
        if request.candidate_id != expected:
            return self._result(
                409,
                "CANDIDATE_STALE",
                details={"record": "candidate_id_mismatch", "expected": expected},
            )
        if not is_digest(request.artifact_digest):
            return self._result(409, "EVIDENCE_MISSING", details={"record": "artifact_digest"})
        if request.artifact_bytes is not None and digest_of(request.artifact_bytes) != request.artifact_digest:
            return self._result(409, "EVIDENCE_MISSING", details={"record": "artifact_digest_mismatch"})
        if not request.service_id:
            return self._result(409, "CANDIDATE_STALE", details={"record": "service_binding"})
        return None

    def _no_change_identity(self, request):
        return (
            request.candidate
            and request.candidate.get("head") == request.previous_healthy_sha
            and request.running_sha == request.previous_healthy_sha
            and request.artifact_digest == request.previous_artifact_digest
        )

    def _inspect_residuals(self, request, store):
        directory = self.root / "residuals"
        if not directory.exists():
            return {"ok": True, "kept": []}
        allowed = set()
        if request.previous_artifact_digest:
            allowed.add(request.previous_artifact_digest)
        for item in store["host_inventory"]:
            if item.get("artifact_digest"):
                allowed.add(item["artifact_digest"])
        kept = []
        for path in sorted(directory.iterdir()):
            digest = self._residual_digest(path)
            if digest is None or digest not in allowed:
                return {
                    "ok": False,
                    "record": "residual_unbound",
                    "path": path.name,
                    "deleted": False,
                }
            kept.append(path.name)
        return {"ok": True, "kept": kept, "deleted": False}

    def _residual_digest(self, path):
        if path.is_file():
            return digest_of(path.read_bytes())
        if path.is_dir():
            body = path / "BODY"
            if body.is_file():
                return digest_of(body.read_bytes())
        return None

    def _active(self, store, request):
        found = []
        for deployment in store["deployments"].values():
            if deployment["state"] in ("COMMIT", "ROLLED_BACK"):
                continue
            same = (
                deployment["app_id"] == request.app_id
                and deployment["host_id"] == request.host_id
                and deployment["service_id"] == request.service_id
            )
            if same:
                found.append(deployment)
        return found

    def _bytes_match(self, digest_value):
        body = self.artifacts.read(digest_value)
        return isinstance(body, bytes) and digest_of(body) == digest_value

    def _transition(self, deployment, src, dst, guard, keep_pending=False):
        if deployment["state"] != src:
            raise ContractError(f"{deployment['state']} is not {src}")
        if not allows(self.edges, src, dst, guard):
            raise ContractError(f"W00 contract has no {src} -> {dst} guard {guard!r}")
        pending = deployment.get("pending")
        deployment["state"] = dst
        deployment["revision"] = deployment["revision"] + 1
        deployment["pending"] = pending if keep_pending else None
        if dst == "COMMIT":
            deployment["running_sha"] = deployment["candidate"]["head"]
        stored = self._put(deployment)
        self._note(
            name="transition",
            deployment_id=stored["deployment_id"],
            from_state=src,
            to_state=dst,
            guard=guard,
        )
        if dst == "COMMIT":
            self._emit("deployment.commit", stored)
            self._install_inventory(stored)
            self._release_fence(stored)
        elif dst == "ROLLED_BACK":
            self._restore_inventory(stored)
            self._release_fence(stored)
        return self._dep(stored["deployment_id"])

    def _install_inventory(self, deployment):
        self.journal.commit(
            {
                "type": "inventory",
                "items": [
                    {
                        "service_id": deployment["service_id"],
                        "running_sha": deployment["candidate"]["head"],
                        "artifact_digest": deployment["artifact_digest"],
                        "app_id": deployment["app_id"],
                        "host_id": deployment["host_id"],
                    }
                ],
            }
        )

    def _restore_inventory(self, deployment):
        self.journal.commit({"type": "inventory", "items": deployment.get("inventory") or []})

    def _release_fence(self, deployment):
        key = self._fence_key(deployment["app_id"], deployment["host_id"], deployment["service_id"])
        self.fence.release(key, deployment["fence_generation"], self._engine_id)
        self.journal.commit(
            {"type": "lock_release", "key": key, "generation": deployment["fence_generation"]}
        )

    def _hold(self, candidate_identifier, reason):
        self.journal.commit({"type": "hold", "candidate_id": candidate_identifier, "reason": reason})

    def _emit(self, event_type, deployment):
        if self.events is None:
            return
        fact = {
            "event_id": f"evt-{deployment['deployment_id']}-{deployment['revision']}-{event_type}",
            "schema_version": 2,
            "event_type": event_type,
            "event_class": "deployment",
            "repo_id": deployment["candidate"]["repo_id"],
            "producer_id": "host_deployer",
            "stream_id": "host_deployer:deployment",
            "stream_epoch": 1,
            "producer_seq": deployment["revision"],
            "occurred_at": "server",
            "idempotency_key": f"idem-event-{deployment['deployment_id']}-{event_type}-{deployment['revision']}",
            "deployment_id": deployment["deployment_id"],
            "app_id": deployment["app_id"],
            "host_id": deployment["host_id"],
            "candidate_id": deployment["candidate_id"],
            "head": deployment["candidate"]["head"],
            "base": deployment["candidate"]["base"],
            "merge": deployment["candidate"]["merge"],
            "tree": deployment["candidate"]["tree"],
            "running_sha": deployment.get("running_sha"),
            "artifact_digest": deployment["artifact_digest"],
            "previous_healthy_sha": deployment.get("previous_healthy_sha"),
            "install_kind": deployment["install_kind"],
            "stage": "deploy",
            "state": deployment["state"],
        }
        try:
            self.events.append_deployment(fact)
            self._note(
                name="delivery",
                deployment_id=deployment["deployment_id"],
                status="DURABLE_LOCAL",
                event_type=event_type,
            )
        except Exception:
            self._note(
                name="delivery",
                deployment_id=deployment["deployment_id"],
                status="DELIVERY_UNKNOWN",
                event_type=event_type,
            )

    def _ack(self, deployment, no_change=False, backfill=False):
        return {
            "durable": True,
            "candidate_id": deployment["candidate_id"],
            "artifact_digest": deployment["artifact_digest"],
            "service_id": deployment["service_id"],
            "no_change": no_change,
            "backfill": backfill,
        }

    def _spec(self, deployment):
        return {
            "deployment_id": deployment["deployment_id"],
            "candidate_id": deployment["candidate_id"],
            "head": deployment["candidate"]["head"],
            "base": deployment["candidate"]["base"],
            "merge": deployment["candidate"]["merge"],
            "tree": deployment["candidate"]["tree"],
            "artifact_digest": deployment["artifact_digest"],
            "service_id": deployment["service_id"],
            "app_id": deployment["app_id"],
            "host_id": deployment["host_id"],
            "previous_healthy_sha": deployment.get("previous_healthy_sha"),
            "previous_artifact_digest": deployment.get("previous_artifact_digest"),
            "install_kind": deployment["install_kind"],
        }

    def _previous_spec(self, deployment):
        spec = self._spec(deployment)
        spec["head"] = deployment.get("previous_healthy_sha")
        spec["artifact_digest"] = deployment.get("previous_artifact_digest")
        return spec

    def _put(self, deployment):
        self.journal.commit({"type": "deployment", "deployment": copy.deepcopy(deployment)})
        return self._dep(deployment["deployment_id"])

    def _dep(self, deployment_id):
        if not deployment_id:
            return None
        store = self.journal.replay()
        found = store["deployments"].get(deployment_id)
        return copy.deepcopy(found) if found else None

    def _note(self, **payload):
        self.journal.commit({"type": "note", **payload})

    def _halt(self, point):
        if self.halt_at == point:
            raise Interrupted(point)

    def _classification(self, effect):
        if effect.classification in {"transient", "deterministic"}:
            return effect.classification
        if effect.status == "failed":
            return "deterministic"
        return "none"

    def _fence_key(self, app_id, host_id, service_id):
        parts = (app_id, host_id, service_id)
        for part in parts:
            if not isinstance(part, str) or not part or any(char in part for char in "/\\"):
                return None
        return "__".join(parts)

    def _result(self, http, code, deployment=None, **details):
        nested = details.pop("details", None)
        if isinstance(nested, dict):
            details = {**nested, **details}
        details.setdefault("production_enablement", False)
        details.setdefault("real_service_switch", False)
        return DeployResult(
            http=http,
            code=code,
            state=None if deployment is None else deployment["state"],
            deployment_id=None if deployment is None else deployment["deployment_id"],
            revision=None if deployment is None else deployment["revision"],
            fence_generation=None if deployment is None else deployment.get("fence_generation"),
            lease_token=None,
            details=details,
        )

    def _public(self, result):
        return {
            "http": result.http,
            "code": result.code,
            "state": result.state,
            "deployment_id": result.deployment_id,
            "revision": result.revision,
            "fence_generation": result.fence_generation,
            "details": result.details,
            "lease_token": None,
        }

    def _idem_key(self, request, operation):
        raw = "\n".join(
            [request.principal_id, request.scope, operation, request.idempotency_key]
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _payload(self, request, operation):
        body = {
            "operation": operation,
            "deployment_id": request.deployment_id,
            "candidate_id": request.candidate_id,
            "artifact_digest": request.artifact_digest,
            "artifact_bytes_digest": digest_of(request.artifact_bytes) if request.artifact_bytes is not None else None,
            "service_id": request.service_id,
            "install_kind": request.install_kind,
            "previous_healthy_sha": request.previous_healthy_sha,
            "no_change": request.no_change,
            "run_id": request.run_id,
            "observation": request.observation,
            "resources_removed": request.resources_removed,
            "takeover": request.takeover,
            "apply_migrations": request.apply_migrations,
        }
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _replay(self, request, operation):
        store = self.journal.replay()
        record = store["idempotency"].get(self._idem_key(request, operation))
        if record is None:
            return None
        if record["payload_hash"] != self._payload(request, operation):
            return self._result(409, "IDEMPOTENCY_PAYLOAD_MISMATCH")
        current = record["original_public"].get("state")
        if record.get("deployment_id"):
            found = store["deployments"].get(record["deployment_id"])
            if found:
                current = found["state"]
        original = record["original_public"]
        return DeployResult(
            http=200,
            code="IDEMPOTENCY_REPLAY",
            state=original.get("state"),
            deployment_id=record.get("deployment_id"),
            revision=original.get("revision"),
            replayed=True,
            original_result=original,
            current_state=current,
            lease_token=None,
            fence_generation=original.get("fence_generation"),
            details={"production_enablement": False, "real_service_switch": False},
        )

    def _remember(self, request, operation, result):
        if result.code in SKIP_IDEMPOTENCY or result.replayed:
            return
        self.journal.commit(
            {
                "type": "idempotency",
                "record": {
                    "key": self._idem_key(request, operation),
                    "payload_hash": self._payload(request, operation),
                    "original_public": self._public(result),
                    "deployment_id": result.deployment_id,
                },
            }
        )

    def _finish(self, request, operation, result):
        self._remember(request, operation, result)
        return result
