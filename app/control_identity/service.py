"""Isolated identity service.

Authenticate, then authorize, then touch idempotency. A failure of the first
two steps does not create an idempotency record. Production verification
stays off: this service cannot release a real publish.
"""

from __future__ import annotations

import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .authorize import authorize
from .canonical import candidate_id_for, credential_scope_of, payload_hash, repo_of
from .durable import DurableStore
from .effects import SerialEffectProxy
from .errors import ControlError, Result
from .lease import LEASE_TTL, assert_bound, fence, new_token, token_hash
from .policy import decide, denied
from .proof import checked_message, verify_signature
from .proxy import OPERATION_SCOPE, check_proxy, rotate

IDEMPOTENT = {
    "run.claim",
    "run.renew",
    "run.start",
    "run.complete",
    "run.release",
    "ci.status.write",
    "release.accept",
    "preview.merge",
    "feedback.write",
    "pull.read",
    "proxy.rotate",
}
PURE = {"policy.evaluate", "diagnostic.read", "proxy.authorize", "session.check", "branch.read"}


class ControlIdentityService:
    def __init__(self, root, *, isolated: bool, clock=None):
        self.store = DurableStore(root)
        self.isolated = bool(isolated)
        self.test_identities_enabled = bool(isolated)
        self._clock = clock or time.time
        self.effects = SerialEffectProxy()
        self.effect_hold = None

    @property
    def production_verification_enabled(self) -> bool:
        return False

    def now(self) -> int:
        return int(self._clock())

    def register_principal(self, *, principal_id: str, kind: str, key_id: str, public_key_hex: str,
                           test_identity: bool, app_id: str = "", scopes: list | None = None) -> None:
        if not self.isolated or not test_identity:
            raise ControlError("NOT_AUTHORIZED", 403, "production principals are not registered here",
                               reason="production_registration_disabled")
        if not principal_id or not kind or not key_id:
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "principal is incomplete", reason="principal")
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))

        def mutate(state):
            if principal_id in state["principals"]:
                raise ControlError("CONFLICT_REVISION", 409, "principal already registered", reason="principal")
            state["principals"][principal_id] = {
                "principal_id": principal_id,
                "kind": kind,
                "key_id": key_id,
                "public_key": public_key_hex,
                "app_id": app_id,
                "scopes": list(scopes or []),
                "test_identity": True,
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

    def trusted_fence(self, run_id: str, *, fenced: bool, effect_known: bool) -> dict:
        """Server-side reconciliation. Client actor_role is not an input."""

        def mutate(state):
            run = state["runs"].get(run_id)
            if not fenced or effect_known or run is None or run["state"] not in {"LEASED", "RUNNING"}:
                raise ControlError("EVIDENCE_MISSING", 409, "unknown requires fencing and an unclassified effect",
                                   reason="fence", extra={"changed": False, "state": None if run is None else run["state"]})
            run["state"] = "UNKNOWN"
            run["token_hash"] = ""
            return state, {"code": "SIDE_EFFECT_UNKNOWN", "state": "UNKNOWN", "changed": True}

        return self.store.update(mutate)

    def trusted_classify(self, run_id: str, *, fenced: bool, classification: str) -> dict:
        mapping = {"succeeded": "SUCCEEDED", "failed": "FAILED", "not_occurred": "RETRY_WAIT"}

        def mutate(state):
            run = state["runs"].get(run_id)
            if not fenced:
                raise ControlError("NOT_AUTHORIZED", 403, "classification requires fencing", reason="fencing_required",
                                   extra={"changed": False, "state": None if run is None else run["state"]})
            if run is None or run["state"] != "UNKNOWN" or classification not in mapping:
                raise ControlError("EVIDENCE_MISSING", 409, "classification needs an unknown run and a real class",
                                   reason="classify", extra={"changed": False, "state": None if run is None else run["state"]})
            run["state"] = mapping[classification]
            return state, {"state": run["state"], "changed": True}

        return self.store.update(mutate)

    def execute(self, operation: str, body: dict, *, route_class: str) -> Result:
        self._precheck(body)
        if operation == "policy.evaluate":
            return self._policy(body, route_class)
        if operation == "branch.read":
            principal = self._authenticate(body, operation, route_class, persist_nonce=False)
            authorize(principal, operation, body)
            from .branch_evidence import summarize
            report = summarize([])
            report.update({"schema_version": 2, "http": 200, "side_effect": False,
                           "server_principal": {"id": principal["principal_id"], "kind": principal["kind"]}})
            return Result(200, report)
        principal = self._authenticate(body, operation, route_class, persist_nonce=operation not in PURE)
        if operation == "run.reconcile":
            raise ControlError("NOT_AUTHORIZED", 403, "a client cannot fence or reconcile",
                               reason="client_cannot_fence", extra={"changed": False})
        authorize(principal, operation, body)
        if operation == "proxy.authorize":
            self._proxy_gate(operation, body)
            return self._read_result(principal, {"allow": True, "scope": credential_scope_of(body),
                                                 "requested_operation": body.get("requested_operation")})
        if operation in OPERATION_SCOPE:
            self._proxy_gate(operation, body)
        if operation == "diagnostic.read":
            return self._read_result(principal, {"allow": True, "app_id": body.get("target_app_id") or ""})
        if operation == "session.check":
            return self._check_session(principal, body)
        decision = decide(principal, operation, body, self.store.read()["policy"])
        if not decision.allow:
            raise denied(decision)
        if operation == "release.publish":
            self._block_publish(body)
        if operation == "github.effect":
            return self._effect(principal, body)
        if operation in {"limit.consume", "session.issue", "session.revoke", "signer.revoke"}:
            return self._direct(operation, principal, body)
        if operation not in IDEMPOTENT:
            raise ControlError("NOT_AUTHORIZED", 403, "operation is not allowed", reason="default_deny")
        return self._idempotent(operation, principal, body)

    def _policy(self, body: dict, route_class: str) -> Result:
        before = self.store.text()
        principal = self._authenticate(body, "policy.evaluate", route_class, persist_nonce=False)
        authorize(principal, "policy.evaluate", body)
        decision = decide(principal, "policy.evaluate", body, self.store.read()["policy"])
        if self.store.text() != before:
            raise ControlError("POLICY_DENIED", 500, "policy evaluation wrote state", reason="side_effect")
        if not decision.allow:
            error = denied(decision)
            if self.store.text() != before:
                raise ControlError("POLICY_DENIED", 500, "policy evaluation wrote state", reason="side_effect")
            raise error
        return self._read_result(principal, {"allow": True, "scope_class": decision.scope_class, "action": body.get("action")})

    def _precheck(self, body: dict) -> None:
        if not isinstance(body, dict) or body.get("schema_version") != 2:
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "schema_version must be 2", reason="schema")
        if "received_at" in body:
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "received_at is server-owned", reason="received_at")

    def _reject_route(self, body: dict, route_class: str) -> None:
        actor = body.get("actor") if isinstance(body.get("actor"), dict) else {}
        kind = str(actor.get("kind") or "")
        ref = str(actor.get("proof_ref") or "")
        declared = str(actor.get("id") or "")
        testish = kind == "test" or ref.startswith("test-proof://")
        isolated_ok = route_class == "isolated" and self.isolated and self.test_identities_enabled
        if not isolated_ok:
            reason = "test_identity_rejected" if testish else "production_verification_disabled"
            raise ControlError("NOT_AUTHORIZED", 403, f"rejected {declared}", reason=reason)
        if kind != "test" or not ref.startswith("test-proof://"):
            raise ControlError("NOT_AUTHORIZED", 403, f"rejected {declared}", reason="test_envelope_required")

    def _authenticate(self, body: dict, operation: str, route_class: str, *, persist_nonce: bool) -> dict:
        self._reject_route(body, route_class)
        proof = body.get("proof")
        actor = body.get("actor") if isinstance(body.get("actor"), dict) else {}
        declared_id = str(actor.get("id") or "")
        if not isinstance(proof, dict):
            raise ControlError("PROOF_INVALID", 403, f"rejected {declared_id}", reason="signature")
        signer_id = str(proof.get("signer_id") or "")
        state = self.store.read()
        record = state["principals"].get(signer_id)
        if record is None or not record.get("test_identity"):
            raise ControlError("NOT_AUTHORIZED", 403, f"rejected {declared_id}", reason="unknown_signer")
        if str(proof.get("key_id") or "") != record["key_id"]:
            raise ControlError("PROOF_INVALID", 403, "signing key is not current", reason="key_rotated")
        message = checked_message(operation, body, proof)
        verify_signature(record["public_key"], str(proof.get("signature") or ""), message)
        if declared_id != record["principal_id"] or signer_id != record["principal_id"]:
            raise ControlError("NOT_AUTHORIZED", 403, f"rejected {declared_id}", reason="identity_mismatch")
        self._window(proof)
        principal = {
            "principal_id": record["principal_id"],
            "kind": record["kind"],
            "key_id": record["key_id"],
            "app_id": record.get("app_id") or "",
            "scopes": list(record.get("scopes") or []),
            "test_identity": True,
            "declared_kind": str(actor.get("kind") or ""),
            "declared_id": declared_id,
        }
        nonce = str(proof.get("nonce") or "")

        def check(current, *, insert: bool):
            current_record = current["principals"].get(signer_id)
            if current_record is None or current_record["key_id"] != record["key_id"] or current_record["public_key"] != record["public_key"]:
                raise ControlError("PROOF_INVALID", 403, "signing key is not current", reason="key_rotated")
            if signer_id in current["revoked_signers"]:
                raise ControlError("PROOF_REVOKED", 403, "signer revoked", reason="revoked")
            slot = signer_id + ":" + nonce
            if slot in current["nonces"]:
                raise ControlError("PROOF_REPLAY", 403, "nonce already used", reason="replay")
            if insert:
                current["nonces"][slot] = signer_id

        if persist_nonce:
            def mutate(current):
                check(current, insert=True)
                return current, None
            self.store.update(mutate)
        else:
            check(self.store.read(), insert=False)
        return principal

    def _window(self, proof: dict) -> None:
        try:
            issued = int(proof["issued_at"])
            expires = int(proof["expires_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ControlError("PROOF_INVALID", 403, "validity window rejected", reason="window") from exc
        now = self.now()
        if issued >= expires or issued > now or now >= expires:
            raise ControlError("PROOF_EXPIRED", 403, "proof is outside its validity window", reason="expired")

    def _proxy_gate(self, operation: str, body: dict) -> None:
        target = str(body.get("requested_operation") or "") if operation == "proxy.authorize" else operation
        try:
            generation = int(body.get("credential_generation"))
        except (TypeError, ValueError) as exc:
            raise ControlError("NOT_AUTHORIZED", 403, "credential generation rejected", reason="stale_generation") from exc
        check_proxy(self.store.read()["proxy"], scope=credential_scope_of(body), generation=generation, operation=target)

    def _block_publish(self, body: dict) -> None:
        state = self.store.read()
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
        repo = repo_of(body)
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

    def _idempotent(self, operation: str, principal: dict, body: dict) -> Result:
        if not body.get("idempotency_key"):
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "idempotency_key is required", reason="idempotency")
        digest = payload_hash(operation, body)
        key = "|".join([principal["principal_id"], repo_of(body), operation, str(body["idempotency_key"])])
        now = self.now()

        def mutate(state):
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
            return state, self._ok(public, principal, token=private.get("lease_token"))

        return self.store.update(mutate)

    def _transition(self, state: dict, operation: str, principal: dict, body: dict, now: int):
        if operation == "run.claim":
            return self._claim(state, principal, body, now)
        if operation == "run.renew":
            return self._renew(state, principal, body, now)
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
        if operation in {"feedback.write", "pull.read"}:
            state["writes"].append({
                "operation": operation,
                "scope": credential_scope_of(body),
                "principal_id": principal["principal_id"],
            })
            return {"state": "ACCEPTED", "operation": operation, "scope": credential_scope_of(body)}, {}
        if operation == "preview.merge":
            state["merges"].append({
                "repo": repo_of(body),
                "task_id": str(body.get("task_id") or ""),
                "isolated": True,
                "production": False,
            })
            return {"state": "ALLOWED", "isolated": True, "production_enabled": False}, {}
        if operation == "ci.status.write":
            candidate_id = self._tuple(state, body)
            state["statuses"][candidate_id] = {"signer_kind": principal["kind"], "signer_id": principal["principal_id"]}
            return {"state": "RECORDED", "candidate_id": candidate_id}, {}
        if operation == "release.accept":
            candidate_id = self._tuple(state, body)
            state["acceptances"][candidate_id] = {
                "signer_kind": principal["kind"],
                "signer_id": principal["principal_id"],
                "head": str(body.get("head") or ""),
                "base": str(body.get("base") or ""),
                "merge": str(body.get("merge") or ""),
                "tree": str(body.get("tree") or ""),
            }
            return {"state": "ACCEPTED", "candidate_id": candidate_id}, {}
        raise ControlError("NOT_AUTHORIZED", 403, "operation is not allowed", reason="default_deny")

    def _claim(self, state, principal, body, now):
        run_id = str(body.get("run_id") or "") or token_hash(new_token())[:16]
        existing = state["runs"].get(run_id)
        recovery = str(body.get("recovery") or "")
        if existing is not None:
            if recovery == "timeout" or existing["state"] == "UNKNOWN":
                raise ControlError("RECLAIM_DENIED", 409, "timeout does not reclaim a run", reason="timeout",
                                   extra={"changed": False, "state": existing["state"]})
            raise ControlError("CONFLICT_REVISION", 409, "run already exists", reason="run", extra={"changed": False})
        if recovery == "timeout":
            raise ControlError("RECLAIM_DENIED", 409, "timeout does not reclaim a run", reason="timeout",
                               extra={"changed": False, "state": None})
        try:
            revision = int(body.get("expected_revision") or 1)
        except (TypeError, ValueError) as exc:
            raise ControlError("CONFLICT_REVISION", 409, "revision rejected", reason="revision") from exc
        if revision != 1:
            raise ControlError("CONFLICT_REVISION", 409, "revision rejected", reason="revision", extra={"changed": False})
        token = new_token()
        state["runs"][run_id] = {
            "run_id": run_id,
            "principal_id": principal["principal_id"],
            "scope": repo_of(body),
            "operation_scope": credential_scope_of(body) or "run",
            "generation": 1,
            "state": "LEASED",
            "token_hash": token_hash(token),
            "expires_at": now + LEASE_TTL,
            "revision": 1,
            "task_id": str(body.get("task_id") or ""),
        }
        return {"state": "LEASED", "run_id": run_id, "lease_generation": 1}, {"lease_token": token, "subject": run_id}

    def _bound(self, state, principal, body, now):
        run_id = str(body.get("run_id") or "")
        run = state["runs"].get(run_id)
        try:
            generation = int(body.get("lease_generation"))
        except (TypeError, ValueError) as exc:
            raise fence(run, "generation") from exc
        assert_bound(
            run,
            principal_id=principal["principal_id"],
            token=str(body.get("lease_token") or ""),
            generation=generation,
            now=now,
            repo=repo_of(body),
            operation_scope=credential_scope_of(body) or "run",
        )
        return run

    def _renew(self, state, principal, body, now):
        run = self._bound(state, principal, body, now)
        token = new_token()
        run["generation"] = int(run["generation"]) + 1
        run["token_hash"] = token_hash(token)
        run["expires_at"] = now + LEASE_TTL
        public = {"state": run["state"], "run_id": run["run_id"], "lease_generation": run["generation"]}
        return public, {"lease_token": token, "subject": run["run_id"]}

    def _move(self, state, principal, body, now, *, source, target):
        run = self._bound(state, principal, body, now)
        allowed = {source} if isinstance(source, str) else set(source)
        if run["state"] not in allowed:
            raise ControlError("CONFLICT_REVISION", 409, "run is not in the required state", reason="state",
                               extra={"changed": False, "state": run["state"]})
        if "expected_revision" in body and str(body.get("expected_revision")) not in {"", str(run["revision"])}:
            raise ControlError("CONFLICT_REVISION", 409, "revision rejected", reason="revision",
                               extra={"changed": False, "state": run["state"]})
        run["state"] = target
        return {"state": target, "run_id": run["run_id"], "lease_generation": run["generation"]}, {"subject": run["run_id"]}

    def _release(self, state, principal, body, now):
        run = self._bound(state, principal, body, now)
        if run["state"] != "LEASED" or run.get("external_effect"):
            raise ControlError("CONFLICT_REVISION", 409, "run cannot be released", reason="state",
                               extra={"changed": False, "state": run["state"]})
        run["state"] = "PENDING"
        run["token_hash"] = ""
        return {"state": "PENDING", "run_id": run["run_id"]}, {"subject": run["run_id"]}

    def _effect(self, principal: dict, body: dict) -> Result:
        if not body.get("idempotency_key") or not body.get("effect_id"):
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "effect id and idempotency key are required", reason="effect")
        digest = payload_hash("github.effect", body)
        key = "|".join([principal["principal_id"], repo_of(body), "github.effect", str(body["idempotency_key"])])
        effect_id = str(body["effect_id"])
        now = self.now()

        def examiner():
            def mutate(state):
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
                run = self._bound(state, principal, body, now)
                state["effects"].append({
                    "id": effect_id,
                    "run_id": run["run_id"],
                    "generation": run["generation"],
                    "result": "checked",
                })
                run["external_effect"] = effect_id
                public = {"state": run["state"], "effect_id": effect_id, "checked": True}
                state["idempotency"][key] = {"payload_hash": digest, "original_result": dict(public), "subject": run["run_id"]}
                return state, self._ok(public, principal)
            return self.store.update(mutate)

        return self.effects.check(effect_id, examiner, hold=self.effect_hold)

    def _direct(self, operation: str, principal: dict, body: dict) -> Result:
        def mutate(state):
            if operation == "limit.consume":
                return self._limit(state, principal, body)
            if operation == "session.issue":
                raw = new_token()
                state["sessions"][token_hash(raw)] = {"principal_id": principal["principal_id"], "revoked": False}
                return state, self._ok({"state": "ISSUED", "session_id": raw}, principal)
            if operation == "session.revoke":
                raw = str(body.get("session_id") or "")
                digest = token_hash(raw)
                if digest not in state["revoked_sessions"]:
                    state["revoked_sessions"].append(digest)
                if digest in state["sessions"]:
                    state["sessions"][digest]["revoked"] = True
                return state, self._ok({"state": "REVOKED", "revoked": True}, principal)
            if operation == "signer.revoke":
                target = str(body.get("target_signer") or "")
                if target not in state["principals"]:
                    raise ControlError("NOT_AUTHORIZED", 403, "unknown signer", reason="unknown_signer")
                if target not in state["revoked_signers"]:
                    state["revoked_signers"].append(target)
                return state, self._ok({"state": "REVOKED", "target_signer": target}, principal)
            raise ControlError("NOT_AUTHORIZED", 403, "operation is not allowed", reason="default_deny")
        return self.store.update(mutate)

    def _limit(self, state, principal, body):
        key = str(body.get("limit_key") or principal["principal_id"])
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
        return state, self._ok({"state": "COUNTED", "count": bucket["count"]}, principal)

    def _check_session(self, principal: dict, body: dict) -> Result:
        digest = token_hash(str(body.get("session_id") or ""))
        state = self.store.read()
        row = state["sessions"].get(digest)
        active = bool(row) and digest not in state["revoked_sessions"] and not row.get("revoked")
        if not active:
            raise ControlError("NOT_AUTHORIZED", 401, "session is revoked or unknown", reason="session_revoked")
        return self._read_result(principal, {"active": True})

    def _read_result(self, principal: dict, public: dict) -> Result:
        body = {
            "schema_version": 2,
            "http": 200,
            "side_effect": False,
            "replayed": False,
            "production_verification_enabled": False,
            "server_principal": {"id": principal["principal_id"], "kind": principal["kind"]},
            "declared": {"id": principal["declared_id"], "kind": principal["declared_kind"]},
        }
        body.update(public)
        return Result(200, body)

    def _ok(self, public: dict, principal: dict, token: str | None = None) -> Result:
        body = {
            "schema_version": 2,
            "http": 200,
            "replayed": False,
            "side_effect": True,
            "production_verification_enabled": False,
            "server_principal": {"id": principal["principal_id"], "kind": principal["kind"]},
            "declared": {"id": principal["declared_id"], "kind": principal["declared_kind"]},
        }
        body.update(public)
        body["lease_token"] = token
        return Result(200, body)
