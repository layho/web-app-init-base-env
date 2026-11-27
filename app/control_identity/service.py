"""Isolated identity service.

Authenticate, authorize, nonce, policy revision, and the durable effect are
one transaction. A rejected request raises before that transaction writes,
so it does not consume a nonce or change business state. Reads follow
READ_REPLAY_POLICY: every call is checked again and nothing is stored.
Production verification stays off.
"""

from __future__ import annotations

import re
import secrets
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.control_authz.canonical import idempotency_digest, rate_limit_key
from app.control_authz.errors import AuthError
from app.control_authz.principal import PrincipalRegistry
from app.control_authz.routes import route_path_for
from app.control_authz.service import READ_OPERATIONS, READ_REPLAY_POLICY, WRITE_REPLAY_POLICY, AuthzService

from .durable import DurableStore
from .effects import DurableEffectProxy
from .errors import ControlError, Result
from .lease import LEASE_TTL, assert_bound, fence, new_token, token_hash
from .policy import decide, denied
from .proxy import OPERATION_SCOPE, check_proxy, rotate

_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")
_REPO = re.compile(r"^[A-Za-z0-9_.:/-]+$")
TERMINAL_RUNS = {"SUCCEEDED", "FAILED", "SUPERSEDED", "CANCELLED"}
IDEMPOTENT = {
    "run.claim",
    "run.renew",
    "run.start",
    "run.complete",
    "run.release",
    "run.heartbeat",
    "ci.status.write",
    "release.accept",
    "release.reject",
    "preview.merge",
    "feedback.write",
    "proxy.rotate",
}


class ControlIdentityService:
    def __init__(self, root, *, isolated: bool, clock=None, max_nonces: int = 8192, max_effects: int = 1024):
        self.store = DurableStore(root, max_nonces=max_nonces, max_effects=max_effects)
        self.isolated = bool(isolated)
        self.test_identities_enabled = bool(isolated)
        self._clock = clock or time.time
        self.effects = DurableEffectProxy(root)
        self.effect_hold = None
        self.transaction_probe = None
        self.branch_observations = []
        self.authz = AuthzService(
            registry=PrincipalRegistry(isolated=bool(isolated)),
            isolated=bool(isolated),
            policy_revision=1,
        )

    @property
    def production_verification_enabled(self) -> bool:
        return False

    @property
    def read_replay_policy(self) -> str:
        return READ_REPLAY_POLICY

    @property
    def write_replay_policy(self) -> str:
        return WRITE_REPLAY_POLICY

    def now(self) -> int:
        return int(self._clock())

    def register_principal(self, *, principal_id: str, kind: str, key_id: str, public_key_hex: str,
                           test_identity: bool, app_id: str = "", scopes: list | None = None,
                           repo_id: str = "", producer_id: str = "", stream_id: str = "",
                           task_ids: list | None = None, admin: bool = False) -> None:
        if not self.isolated or not test_identity:
            raise ControlError("NOT_AUTHORIZED", 403, "production principals are not registered here",
                               reason="production_registration_disabled")
        if not _REPO.fullmatch(repo_id or "") or not all(_ID.fullmatch(item or "") for item in (principal_id, kind, key_id, producer_id, stream_id)):
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "principal is incomplete", reason="principal")
        tasks = [str(item) for item in (task_ids or [])]
        if not tasks or any(not _ID.fullmatch(item) for item in tasks):
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "principal is incomplete", reason="principal")
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        stored_scopes = [str(item) for item in (scopes or []) if str(item) != "admin"]
        for task_id in tasks:
            token = f"task:{task_id}"
            if token not in stored_scopes:
                stored_scopes.append(token)
        if admin:
            stored_scopes.append("admin")

        def mutate(state):
            if principal_id in state["principals"]:
                raise ControlError("CONFLICT_REVISION", 409, "principal already registered", reason="principal")
            state["principals"][principal_id] = {
                "principal_id": principal_id,
                "kind": kind,
                "key_id": key_id,
                "public_key": public_key_hex,
                "app_id": app_id,
                "repo_id": repo_id,
                "producer_id": producer_id,
                "stream_id": stream_id,
                "task_ids": tasks,
                "scopes": stored_scopes,
                "test_identity": True,
                "admin": bool(admin),
            }
            return state, None

        self.store.update(mutate)

    def rotate_signer_key(self, principal_id: str, *, key_id: str, public_key_hex: str) -> None:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))

        def mutate(state):
            record = state["principals"].get(principal_id)
            if record is None:
                raise ControlError("NOT_AUTHORIZED", 403, "unknown signer", reason="unknown_signer")
            record["key_id"] = key_id
            record["public_key"] = public_key_hex
            return state, {"key_id": key_id, "scopes": list(record["scopes"])}

        self.store.update(mutate)

    def revoke_signer(self, signer_id: str) -> None:
        def mutate(state):
            if signer_id not in state["principals"]:
                raise ControlError("NOT_AUTHORIZED", 403, "unknown signer", reason="unknown_signer")
            if signer_id not in state["revoked_signers"]:
                state["revoked_signers"].append(signer_id)
            return state, {"revoked": True}

        self.store.update(mutate)

    def register_candidate(self, *, repo: str, task: str, head: str, base: str, merge: str, tree: str) -> str:
        from app.control_authz.canonical import candidate_id_for

        candidate_id = candidate_id_for(repo, task, head, base, merge, tree)

        def mutate(state):
            state["candidates"][candidate_id] = {
                "repo_id": repo,
                "task_id": task,
                "head": head,
                "base": base,
                "merge": merge,
                "tree": tree,
            }
            return state, candidate_id

        return self.store.update(mutate)

    def inject_gate_evidence(self, evidence: dict) -> None:
        required = ("macci_passed", "same_repo", "queue_allowed", "no_conflict", "frozen", "candidate_exact", "test_passed")
        if not isinstance(evidence, dict) or any(name not in evidence for name in required):
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "gate evidence is incomplete", reason="evidence_missing")
        for name in required:
            if not isinstance(evidence[name], bool):
                raise ControlError("SCHEMA_UNSUPPORTED", 422, "gate evidence types are ambiguous", reason="evidence_type")

        def mutate(state):
            state["policy"]["evidence"] = {name: evidence[name] for name in required}
            state["policy"]["evidence"]["policy_revision"] = state["policy"]["revision"]
            return state, None

        self.store.update(mutate)

    def clear_gate_evidence(self) -> None:
        def mutate(state):
            state["policy"]["evidence"] = None
            return state, None

        self.store.update(mutate)

    def record_branch_observations(self, observations: list[dict]) -> None:
        clean = []
        for item in observations:
            if item.get("method") != "GET":
                raise ControlError("NOT_AUTHORIZED", 403, "branch evidence is read-only", reason="rules_write_rejected")
            clean.append(dict(item))
        self.branch_observations = clean

    def trusted_fence(self, run_id: str, *, fenced: bool, effect_known: bool) -> dict:
        """Server-side reconciliation. Client actor_role is not an input."""

        def examiner():
            def mutate(state):
                run = state["runs"].get(run_id)
                if not fenced or effect_known or run is None or run["state"] not in {"LEASED", "RUNNING"}:
                    raise ControlError(
                        "EVIDENCE_MISSING", 409, "unknown requires fencing and an unclassified effect",
                        reason="fence", extra={"changed": False, "state": None if run is None else run["state"],
                                               "github_fencing": False},
                    )
                run["state"] = "UNKNOWN"
                run["token_hash"] = ""
                run["revision"] = int(run["revision"]) + 1
                return state, {"code": "SIDE_EFFECT_UNKNOWN", "state": "UNKNOWN", "changed": True, "github_fencing": False}

            return self.store.update(mutate)

        return self._guard(lambda: self.effects.reconcile(
            run_id, fenced=fenced, classification="unproven", examiner=examiner,
        ))

    def trusted_classify(self, run_id: str, *, fenced: bool, classification: str) -> dict:
        mapping = {"succeeded": "SUCCEEDED", "failed": "FAILED", "not_occurred": "RETRY_WAIT"}

        def examiner():
            def mutate(state):
                run = state["runs"].get(run_id)
                if not fenced:
                    raise ControlError(
                        "NOT_AUTHORIZED", 403, "classification requires fencing", reason="fencing_required",
                        extra={"changed": False, "state": None if run is None else run["state"], "github_fencing": False},
                    )
                if run is None or run["state"] != "UNKNOWN" or classification not in mapping:
                    raise ControlError(
                        "EVIDENCE_MISSING", 409, "classification needs an unknown run and a real class",
                        reason="classify", extra={"changed": False, "state": None if run is None else run["state"]},
                    )
                run["state"] = mapping[classification]
                run["revision"] = int(run["revision"]) + 1
                return state, {"state": run["state"], "changed": True, "github_fencing": False}

            return self.store.update(mutate)

        return self._guard(lambda: self.effects.reconcile(
            run_id, fenced=fenced, classification=classification, examiner=examiner,
        ))

    def execute(self, operation: str, body: dict, *, route_class: str) -> Result:
        self._precheck(body)
        if not isinstance(body.get("proof"), dict):
            actor = body.get("actor") if isinstance(body.get("actor"), dict) else {}
            raise ControlError("PROOF_INVALID", 403, f"rejected {actor.get('id') or ''}", reason="signature")
        if operation in READ_OPERATIONS:
            return self._read(operation, body, route_class)
        if operation == "github.effect":
            effect_id = str(body.get("effect_id") or "rejected")
            return self.effects.check(
                effect_id,
                lambda: self._write(operation, body, route_class),
                hold=self.effect_hold,
            )
        return self._write(operation, body, route_class)

    def _read(self, operation: str, body: dict, route_class: str) -> Result:
        before = self.store.text()
        state = self.store.read()
        try:
            principal, declared = self._admit(state, operation, body, route_class, write=False)
            if operation == "proxy.authorize":
                self._proxy_gate(state, operation, body)
                result = self._read_result(principal, declared, {
                    "allow": True,
                    "scope": body.get("credential_scope"),
                    "requested_operation": body.get("requested_operation"),
                })
            elif operation == "diagnostic.read":
                result = self._read_result(principal, declared, {"allow": True, "app_id": body.get("target_app_id") or ""})
            elif operation == "session.check":
                result = self._check_session(state, principal, declared, body)
            elif operation == "branch.read":
                from .branch_evidence import summarize
                report = summarize(self.branch_observations)
                report.update({
                    "evidence_source": "server_readonly_observation",
                    "permission": "branch.read",
                    "client_query_is_evidence": False,
                    "rules_modified": False,
                    "note": "Empty or partial observations stay unproven. This route does not change branch protection rules.",
                })
                result = self._read_result(principal, declared, report)
            elif operation == "pull.read":
                self._proxy_gate(state, operation, body)
                result = self._read_result(principal, declared, {
                    "state": "VISIBLE",
                    "operation": operation,
                    "scope": body.get("credential_scope"),
                })
            else:
                decision = decide(principal, operation, body, state["policy"])
                if not decision.allow:
                    raise denied(decision)
                result = self._read_result(principal, declared, {
                    "allow": True,
                    "scope_class": decision.scope_class,
                    "action": body.get("action"),
                })
        except AuthError as exc:
            self._reraise(exc)
        if self.store.text() != before:
            raise ControlError("POLICY_DENIED", 500, "read wrote state", reason="side_effect")
        return result

    def _write(self, operation: str, body: dict, route_class: str) -> Result:
        now = self.now()

        def mutate(state):
            try:
                principal, declared = self._admit(state, operation, body, route_class, write=True)
                if self.transaction_probe is not None:
                    self.transaction_probe(state)
                    principal, declared = self._admit(state, operation, body, route_class, write=True)
                self.authz.consume_nonce(state, principal, body["proof"], now, max_nonces=self.store.max_nonces)
                return self._apply(state, operation, principal, declared, body, now)
            except AuthError as exc:
                self._reraise(exc)

        return self.store.update(mutate)

    def _admit(self, state, operation, body, route_class, *, write: bool):
        if body.get("path") != route_path_for(operation, body):
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "path is not the contract path", reason="path")
        expected_method = "GET" if operation == "branch.read" else "POST"
        if body.get("method") != expected_method:
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "method is not the contract method", reason="method")
        principal = self.authz.authenticate(
            operation=operation, body=body, route_class=route_class, now=self.now(), state=state, write=write,
        )
        if str(body.get("authorizer") or "") == "":
            raise ControlError("NOT_AUTHORIZED", 403, "empty authorizer is denied", reason="empty_authorizer")
        binding = self.authz.binding_for(principal, operation, body)
        self.authz.authorize(principal, operation, binding)
        if operation in OPERATION_SCOPE or operation == "proxy.authorize":
            self._proxy_gate(state, operation, body)
        if operation not in {"proxy.authorize", "session.check", "diagnostic.read", "branch.read", "pull.read"}:
            decision = decide(principal, operation, body, state["policy"])
            if not decision.allow:
                raise denied(decision)
        if operation == "release.publish":
            self._block_publish(state, body)
        actor = body.get("actor") if isinstance(body.get("actor"), dict) else {}
        declared = {"id": str(actor.get("id") or ""), "kind": str(actor.get("kind") or "")}
        return principal, declared

    def _apply(self, state, operation, principal, declared, body, now):
        if operation == "run.reconcile":
            raise ControlError("NOT_AUTHORIZED", 403, "a client cannot fence or reconcile",
                               reason="client_cannot_fence", extra={"changed": False})
        if operation in {"limit.consume", "session.issue", "session.revoke", "signer.revoke"}:
            return self._direct(state, operation, principal, declared, body)
        if operation == "github.effect":
            return self._effect(state, principal, declared, body, now)
        if operation not in IDEMPOTENT:
            raise ControlError("NOT_AUTHORIZED", 403, "operation is not allowed", reason="default_deny")
        return self._idempotent(state, operation, principal, declared, body, now)

    def _precheck(self, body: dict) -> None:
        if not isinstance(body, dict) or body.get("schema_version") != 2:
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "schema_version must be 2", reason="schema")
        if "received_at" in body:
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "received_at is server-owned", reason="received_at")

    def _proxy_gate(self, state, operation: str, body: dict) -> None:
        target = str(body.get("requested_operation") or "") if operation == "proxy.authorize" else operation
        try:
            generation = int(body.get("credential_generation"))
        except (TypeError, ValueError) as exc:
            raise ControlError("NOT_AUTHORIZED", 403, "credential generation rejected", reason="stale_generation") from exc
        if isinstance(body.get("credential_generation"), bool):
            raise ControlError("NOT_AUTHORIZED", 403, "credential generation rejected", reason="stale_generation")
        check_proxy(state["proxy"], scope=str(body.get("credential_scope") or ""), generation=generation, operation=target)

    def _block_publish(self, state, body: dict) -> None:
        candidate_id = self._tuple(state, body)
        record = state["acceptances"].get(candidate_id)
        if record is None or record.get("signer_kind") != "user_acceptance":
            raise ControlError("EVIDENCE_MISSING", 409, "user acceptance is missing", reason="acceptance",
                               extra={"allow": False, "changed": False})
        raise ControlError(
            "PRODUCTION_VERIFICATION_DISABLED",
            403,
            "production publish is not enabled",
            reason="production_verification_disabled",
            extra={"allow": False, "production_verification_enabled": False, "changed": False},
        )

    def _tuple(self, state: dict, body: dict) -> str:
        from app.control_authz.canonical import candidate_id_for

        repo = str(body.get("repo_id") or "")
        task = str(body.get("task_id") or "")
        parts = {name: str(body.get(name) or "") for name in ("head", "base", "merge", "tree")}
        if not all([repo, task, *parts.values()]):
            raise ControlError("EVIDENCE_MISSING", 409, "candidate tuple is incomplete", reason="tuple")
        candidate_id = candidate_id_for(repo, task, parts["head"], parts["base"], parts["merge"], parts["tree"])
        if str(body.get("candidate_id") or "") != candidate_id:
            raise ControlError("CANDIDATE_STALE", 409, "candidate id does not match the tuple", reason="tuple",
                               extra={"changed": False})
        registered = state["candidates"].get(candidate_id)
        if registered is None:
            raise ControlError("CANDIDATE_STALE", 409, "candidate tuple is not the registered candidate", reason="tuple",
                               extra={"changed": False})
        if any(str(registered[name]) != parts[name] for name in parts) or registered["repo_id"] != repo or registered["task_id"] != task:
            raise ControlError("CANDIDATE_STALE", 409, "candidate tuple does not match", reason="tuple",
                               extra={"changed": False})
        return candidate_id

    def _idempotent(self, state, operation, principal, declared, body, now):
        from app.control_authz.canonical import payload_hash

        if not body.get("idempotency_key"):
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "idempotency_key is required", reason="idempotency")
        digest = payload_hash(operation, body)
        key = self._idempotency_key(principal, body, operation)
        if key not in state["idempotency"] and len(state["idempotency"]) >= self.store.max_idempotency:
            raise ControlError("CAPACITY", 429, "idempotency capacity exceeded", reason="capacity")
        existing = state["idempotency"].get(key)
        if existing:
            if existing["payload_hash"] != digest:
                raise ControlError("IDEMPOTENCY_PAYLOAD_MISMATCH", 409, "idempotency key reused with a different payload",
                                   reason="payload", extra={"changed": False})
            current = existing["original_result"].get("state")
            subject = existing.get("subject") or ""
            if subject and subject in state["runs"]:
                current = state["runs"][subject]["state"]
            return state, Result(200, {
                "schema_version": 2,
                "http": 200,
                "code": "IDEMPOTENCY_REPLAY",
                "original_result": dict(existing["original_result"]),
                "current_state": current,
                "lease_token": None,
                "replayed": True,
                "side_effect": False,
                "production_verification_enabled": False,
            })
        public, private = self._transition(state, operation, principal, body, now)
        state["idempotency"][key] = {
            "payload_hash": digest,
            "original_result": dict(public),
            "subject": private.get("subject") or "",
        }
        return state, self._ok(public, principal, declared, token=private.get("lease_token"))

    def _idempotency_key(self, principal, body, operation: str) -> str:
        return idempotency_digest(
            principal_id=principal.principal_id,
            kind=principal.kind,
            repo_id=principal.repo_id,
            app_id=principal.app_id,
            producer_id=principal.producer_id,
            stream_id=principal.stream_id,
            task_id=str(body.get("task_id") or ""),
            credential_scope=str(body.get("credential_scope") or ""),
            phase=str(body.get("phase") or ""),
            object_id=str(body.get("object_id") or ""),
            operation=operation,
            idempotency_key=str(body.get("idempotency_key") or ""),
            target_app_id=str(body.get("target_app_id") or ""),
        )

    def _transition(self, state, operation, principal, body, now):
        if operation == "run.claim":
            return self._claim(state, principal, body, now)
        if operation == "run.renew":
            return self._renew(state, principal, body, now)
        if operation == "run.heartbeat":
            return self._heartbeat(state, principal, body, now)
        if operation == "run.start":
            return self._move(state, principal, body, now, source="LEASED", target="RUNNING")
        if operation == "run.complete":
            result = str(body.get("result") or "SUCCEEDED")
            if result not in {"SUCCEEDED", "FAILED"}:
                raise ControlError("SCHEMA_UNSUPPORTED", 422, "result is not a terminal run result", reason="result")
            return self._move(state, principal, body, now, source=("LEASED", "RUNNING"), target=result)
        if operation == "run.release":
            return self._release(state, principal, body, now)
        if operation == "proxy.rotate":
            scope = str(body.get("scope_to_rotate") or "")
            generation = rotate(state["proxy"], scope)
            return {"state": "ROTATED", "scope": scope, "generation": generation}, {}
        if operation == "feedback.write":
            state["writes"].append({
                "operation": operation,
                "scope": str(body.get("credential_scope") or ""),
                "principal_id": principal.principal_id,
            })
            return {"state": "ACCEPTED", "operation": operation, "scope": str(body.get("credential_scope") or "")}, {}
        if operation == "preview.merge":
            state["merges"].append({
                "repo": str(body.get("repo_id") or ""),
                "task_id": str(body.get("task_id") or ""),
                "isolated": True,
                "production": False,
            })
            return {"state": "ALLOWED", "isolated": True, "production_enabled": False}, {}
        if operation == "ci.status.write":
            candidate_id = self._tuple(state, body)
            state["statuses"][candidate_id] = {"signer_kind": principal.kind, "signer_id": principal.principal_id}
            return {"state": "RECORDED", "candidate_id": candidate_id}, {}
        if operation in {"release.accept", "release.reject"}:
            candidate_id = self._tuple(state, body)
            outcome = "accepted" if operation == "release.accept" else "rejected"
            state["acceptances"][candidate_id] = {
                "signer_kind": principal.kind,
                "signer_id": principal.principal_id,
                "outcome": outcome,
                "head": str(body.get("head") or ""),
                "base": str(body.get("base") or ""),
                "merge": str(body.get("merge") or ""),
                "tree": str(body.get("tree") or ""),
            }
            return {"state": "ACCEPTED" if outcome == "accepted" else "ACCEPTANCE", "candidate_id": candidate_id, "outcome": outcome}, {}
        raise ControlError("NOT_AUTHORIZED", 403, "operation is not allowed", reason="default_deny")

    def _claim(self, state, principal, body, now):
        if str(body.get("run_id") or ""):
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "run_id is assigned by the server", reason="client_run_id")
        recovery = str(body.get("recovery") or "")
        if recovery not in {"", "timeout"}:
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "recovery is not recognized", reason="recovery")
        phase = str(body.get("phase") or "")
        active = self._active_runs(state, principal.repo_id, str(body.get("task_id") or ""), phase)
        if recovery == "timeout":
            current = active[0]["state"] if active else None
            raise ControlError("RECLAIM_DENIED", 409, "timeout does not reclaim a run", reason="timeout",
                               extra={"changed": False, "state": current})
        if any(run["state"] == "UNKNOWN" for run in active):
            raise ControlError("RECLAIM_DENIED", 409, "unknown run blocks a new claim", reason="unknown_unreconciled",
                               extra={"changed": False, "state": "UNKNOWN"})
        for run in list(active):
            if run["state"] == "RETRY_WAIT":
                run["state"] = "SUPERSEDED"
                run["token_hash"] = ""
                run["revision"] = int(run["revision"]) + 1
        active = [run for run in active if run["state"] not in TERMINAL_RUNS]
        if active:
            raise ControlError("RECLAIM_DENIED", 409, "an active lease already exists", reason="active_lease",
                               extra={"changed": False, "state": active[0]["state"]})
        if body.get("expected_revision") != 0:
            raise ControlError("CONFLICT_REVISION", 409, "revision rejected", reason="revision", extra={"changed": False})
        attempt = 1 + max((int(run.get("attempt") or 0) for run in self._scoped_runs(state, principal.repo_id, str(body.get("task_id") or ""), phase)), default=0)
        run_id = "run_" + secrets.token_hex(16)
        token = new_token()
        state["runs"][run_id] = {
            "run_id": run_id,
            "attempt": attempt,
            "principal_id": principal.principal_id,
            "repo_id": principal.repo_id,
            "task_id": str(body.get("task_id") or ""),
            "phase": phase,
            "scope": principal.repo_id,
            "operation_scope": str(body.get("credential_scope") or "") or "run",
            "generation": 1,
            "state": "LEASED",
            "token_hash": token_hash(token),
            "expires_at": now + LEASE_TTL,
            "revision": 1,
            "candidate_id": str(body.get("candidate_id") or ""),
            "app_id": principal.app_id,
            "producer_id": principal.producer_id,
            "stream_id": principal.stream_id,
        }
        return {
            "state": "LEASED",
            "run_id": run_id,
            "attempt": attempt,
            "lease_generation": 1,
            "revision": 1,
        }, {"lease_token": token, "subject": run_id}

    def _scoped_runs(self, state, repo, task, phase):
        for run in state["runs"].values():
            if run.get("repo_id") == repo and run.get("task_id") == task and run.get("phase") == phase:
                yield run

    def _active_runs(self, state, repo, task, phase):
        return [run for run in self._scoped_runs(state, repo, task, phase) if run["state"] not in TERMINAL_RUNS]

    def _bound(self, state, principal, body, now):
        run_id = str(body.get("run_id") or "")
        run = state["runs"].get(run_id)
        try:
            generation = int(body.get("lease_generation"))
        except (TypeError, ValueError) as exc:
            raise fence(run, "generation") from exc
        if isinstance(body.get("lease_generation"), bool):
            raise fence(run, "generation")
        assert_bound(
            run,
            principal_id=principal.principal_id,
            token=str(body.get("lease_token") or ""),
            generation=generation,
            now=now,
            repo=str(body.get("repo_id") or ""),
            operation_scope=str(body.get("credential_scope") or "") or "run",
        )
        if run.get("task_id") != str(body.get("task_id") or ""):
            raise fence(run, "task")
        if run.get("phase") != str(body.get("phase") or ""):
            raise fence(run, "phase")
        if run.get("repo_id") != principal.repo_id:
            raise fence(run, "repo")
        presented = body.get("expected_revision")
        if isinstance(presented, bool) or not isinstance(presented, int) or presented != int(run["revision"]):
            raise ControlError("CONFLICT_REVISION", 409, "revision rejected", reason="revision",
                               extra={"changed": False, "state": run["state"]})
        return run

    def _renew(self, state, principal, body, now):
        run = self._bound(state, principal, body, now)
        token = new_token()
        run["generation"] = int(run["generation"]) + 1
        run["token_hash"] = token_hash(token)
        run["expires_at"] = now + LEASE_TTL
        run["revision"] = int(run["revision"]) + 1
        public = {
            "state": run["state"],
            "run_id": run["run_id"],
            "attempt": run["attempt"],
            "lease_generation": run["generation"],
            "revision": run["revision"],
        }
        return public, {"lease_token": token, "subject": run["run_id"]}

    def _heartbeat(self, state, principal, body, now):
        run = self._bound(state, principal, body, now)
        if run["state"] not in {"LEASED", "RUNNING"}:
            raise ControlError("CONFLICT_REVISION", 409, "run is not in the required state", reason="state",
                               extra={"changed": False, "state": run["state"]})
        run["expires_at"] = now + LEASE_TTL
        run["revision"] = int(run["revision"]) + 1
        return {
            "state": run["state"],
            "run_id": run["run_id"],
            "attempt": run["attempt"],
            "lease_generation": run["generation"],
            "revision": run["revision"],
        }, {"subject": run["run_id"]}

    def _move(self, state, principal, body, now, *, source, target):
        run = self._bound(state, principal, body, now)
        allowed = {source} if isinstance(source, str) else set(source)
        if run["state"] not in allowed:
            raise ControlError("CONFLICT_REVISION", 409, "run is not in the required state", reason="state",
                               extra={"changed": False, "state": run["state"]})
        run["state"] = target
        run["revision"] = int(run["revision"]) + 1
        return {
            "state": target,
            "run_id": run["run_id"],
            "attempt": run["attempt"],
            "lease_generation": run["generation"],
            "revision": run["revision"],
        }, {"subject": run["run_id"]}

    def _release(self, state, principal, body, now):
        run = self._bound(state, principal, body, now)
        if run["state"] != "LEASED" or run.get("external_effect"):
            raise ControlError("CONFLICT_REVISION", 409, "run cannot be released", reason="state",
                               extra={"changed": False, "state": run["state"]})
        run["state"] = "PENDING"
        run["token_hash"] = ""
        run["revision"] = int(run["revision"]) + 1
        return {"state": "PENDING", "run_id": run["run_id"], "revision": run["revision"]}, {"subject": run["run_id"]}

    def _effect(self, state, principal, declared, body, now):
        from app.control_authz.canonical import payload_hash

        if not body.get("idempotency_key") or not body.get("effect_id"):
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "effect id and idempotency key are required", reason="effect")
        digest = payload_hash("github.effect", body)
        key = self._idempotency_key(principal, body, "github.effect")
        effect_id = str(body["effect_id"])
        existing = state["idempotency"].get(key)
        if existing:
            if existing["payload_hash"] != digest:
                raise ControlError("IDEMPOTENCY_PAYLOAD_MISMATCH", 409, "idempotency key reused with a different payload",
                                   reason="payload", extra={"changed": False})
            return state, Result(200, {
                "schema_version": 2,
                "http": 200,
                "code": "IDEMPOTENCY_REPLAY",
                "original_result": dict(existing["original_result"]),
                "current_state": existing["original_result"].get("state"),
                "lease_token": None,
                "replayed": True,
                "side_effect": False,
            })
        if len(state["effects"]) >= self.store.max_effects:
            raise ControlError("CAPACITY", 429, "effect capacity exceeded", reason="capacity", extra={"changed": False})
        run = self._bound(state, principal, body, now)
        state["effects"].append({
            "id": effect_id,
            "run_id": run["run_id"],
            "generation": run["generation"],
            "result": "checked",
            "github_fencing": False,
        })
        run["external_effect"] = effect_id
        run["revision"] = int(run["revision"]) + 1
        public = {"state": run["state"], "effect_id": effect_id, "checked": True, "github_fencing": False}
        state["idempotency"][key] = {"payload_hash": digest, "original_result": dict(public), "subject": run["run_id"]}
        return state, self._ok(public, principal, declared)

    def _direct(self, state, operation, principal, declared, body):
        if operation == "limit.consume":
            return self._limit(state, principal, declared, operation)
        if operation == "session.issue":
            raw = new_token()
            state["sessions"][token_hash(raw)] = {"principal_id": principal.principal_id, "revoked": False}
            return state, self._ok({"state": "ISSUED", "session_id": raw}, principal, declared)
        if operation == "session.revoke":
            return self._revoke_session(state, principal, declared, body)
        if operation == "signer.revoke":
            target = str(body.get("target_signer") or "")
            if target == "" or target not in state["principals"]:
                raise ControlError("NOT_AUTHORIZED", 403, "unknown signer", reason="unknown_signer")
            if not self._admin(state, principal):
                raise ControlError("NOT_AUTHORIZED", 403, "signer revocation requires admin authorization", reason="admin")
            if target not in state["revoked_signers"]:
                state["revoked_signers"].append(target)
            return state, self._ok({"state": "REVOKED", "target_signer": target}, principal, declared)
        raise ControlError("NOT_AUTHORIZED", 403, "operation is not allowed", reason="default_deny")

    def _admin(self, state, principal) -> bool:
        record = state["principals"].get(principal.principal_id) or {}
        return bool(record.get("admin")) and "admin" in (record.get("scopes") or [])

    def _limit(self, state, principal, declared, operation):
        key = rate_limit_key(
            principal_id=principal.principal_id,
            kind=principal.kind,
            repo_id=principal.repo_id,
            app_id=principal.app_id,
            operation=operation,
        )
        now = self.now()
        bucket = state["limits"].get(key)
        if bucket is None or now - int(bucket["window_start"]) >= 3600:
            bucket = {"count": 0, "window_start": now}
        bucket["count"] = int(bucket["count"]) + 1
        state["limits"][key] = bucket
        if bucket["count"] > 3:
            return state, Result(429, {
                "schema_version": 2,
                "http": 429,
                "code": "RATE_LIMITED",
                "detail": "rate limit exceeded",
                "count": bucket["count"],
                "side_effect": True,
            })
        return state, self._ok({"state": "COUNTED", "count": bucket["count"]}, principal, declared)

    def _revoke_session(self, state, principal, declared, body):
        raw = str(body.get("session_id") or "")
        digest = token_hash(raw)
        row = state["sessions"].get(digest)
        if not row:
            raise ControlError("NOT_AUTHORIZED", 403, "session is not owned by this principal", reason="session_owner",
                               extra={"changed": False})
        if row["principal_id"] != principal.principal_id and not self._admin(state, principal):
            raise ControlError("NOT_AUTHORIZED", 403, "session is not owned by this principal", reason="session_owner",
                               extra={"changed": False})
        if digest not in state["revoked_sessions"]:
            state["revoked_sessions"].append(digest)
        row["revoked"] = True
        return state, self._ok({"state": "REVOKED", "revoked": True}, principal, declared)

    def _check_session(self, state, principal, declared, body):
        digest = token_hash(str(body.get("session_id") or ""))
        row = state["sessions"].get(digest)
        if row and row["principal_id"] != principal.principal_id:
            raise ControlError("NOT_AUTHORIZED", 403, "session is not owned by this principal", reason="session_owner")
        active = bool(row) and digest not in state["revoked_sessions"] and not row.get("revoked")
        if not active:
            raise ControlError("NOT_AUTHORIZED", 401, "session is revoked or unknown", reason="session_revoked")
        return self._read_result(principal, declared, {"active": True})

    def _read_result(self, principal, declared, public: dict) -> Result:
        body = {
            "schema_version": 2,
            "http": 200,
            "side_effect": False,
            "replayed": False,
            "production_verification_enabled": False,
            "server_principal": {"id": principal.principal_id, "kind": principal.kind},
            "declared": declared,
            "read_replay_policy": READ_REPLAY_POLICY,
        }
        body.update(public)
        body["side_effect"] = False
        return Result(200, body)

    def _ok(self, public: dict, principal, declared, token: str | None = None) -> Result:
        body = {
            "schema_version": 2,
            "http": 200,
            "replayed": False,
            "side_effect": True,
            "production_verification_enabled": False,
            "server_principal": {"id": principal.principal_id, "kind": principal.kind},
            "declared": declared,
            "write_replay_policy": WRITE_REPLAY_POLICY,
        }
        body.update(public)
        body["lease_token"] = token
        return Result(200, body)

    def _guard(self, fn):
        try:
            return fn()
        except AuthError as exc:
            self._reraise(exc)

    def _reraise(self, exc: AuthError):
        raise ControlError(exc.code, exc.status, exc.detail, reason=exc.reason, extra=exc.extra) from exc
