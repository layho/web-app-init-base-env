"""AuthzService is constructed only from server configuration.

``authenticate`` verifies a proof against the server registry. Isolated test
identities verify only when that registry is enabled. Real routes reject them.
"""

from __future__ import annotations

from .authorize import authorize, binding_for
from .canonical import CanonicalError, idempotency_digest
from .errors import AuthnError, AuthzError
from .principal import Principal, PrincipalRegistry, server_source
from .proof import checked_message, verify_signature

READ_REPLAY_POLICY = "recheck_every_time_without_consuming_nonce_or_idempotency"
WRITE_REPLAY_POLICY = "single_use_nonce_committed_only_with_authorized_side_effect"

READ_OPERATIONS = frozenset({
    "policy.evaluate",
    "diagnostic.read",
    "proxy.authorize",
    "session.check",
    "branch.read",
    "pull.read",
})


class AuthzService:
    def __init__(self, *, registry: PrincipalRegistry, isolated: bool, policy_revision: int):
        if not isinstance(registry, PrincipalRegistry) or isinstance(registry, dict):
            raise AuthzError("NOT_AUTHORIZED", 403, "AuthzService accepts a server registry", reason="server_configuration")
        if isinstance(isolated, dict) or not isinstance(isolated, bool):
            raise AuthzError("NOT_AUTHORIZED", 403, "isolation is server configuration", reason="server_configuration")
        if isinstance(policy_revision, bool) or not isinstance(policy_revision, int):
            raise AuthzError("NOT_AUTHORIZED", 403, "policy revision is server configuration", reason="server_configuration")
        self.registry = registry
        self.isolated = isolated
        self.policy_revision = policy_revision

    def authenticate(self, *, operation: str, body: dict, route_class: str, now: int, state: dict, write: bool) -> Principal:
        self._route_gate(body, route_class)
        proof = body.get("proof") if isinstance(body, dict) else None
        if not isinstance(proof, dict):
            actor = body.get("actor") if isinstance(body, dict) and isinstance(body.get("actor"), dict) else {}
            raise AuthnError("PROOF_INVALID", 403, f"rejected {actor.get('id') or ''}", reason="signature")
        if str(body.get("authorizer") or "") == "":
            raise AuthnError("NOT_AUTHORIZED", 403, "empty authorizer is denied", reason="empty_authorizer")
        try:
            message = checked_message(operation, body, proof)
        except AuthnError:
            raise
        except CanonicalError as exc:
            raise AuthnError("SCHEMA_UNSUPPORTED", 422, "canonical encoding rejected", reason="canonical") from exc
        signer_id = str(proof.get("signer_id") or "")
        record = state.get("principals", {}).get(signer_id) if isinstance(state.get("principals"), dict) else None
        if not isinstance(record, dict) or not record.get("test_identity"):
            raise AuthnError("NOT_AUTHORIZED", 403, f"rejected {body.get('authorizer')}", reason="unknown_signer")
        if not self.registry.test_identities_enabled or not self.isolated or route_class != "isolated":
            raise AuthnError("NOT_AUTHORIZED", 403, "test identity rejected", reason="test_identity_rejected")
        if str(proof.get("key_id") or "") != record.get("key_id"):
            raise AuthnError("PROOF_INVALID", 403, "signing key is not current", reason="key_rotated")
        verify_signature(str(record.get("public_key") or ""), str(proof.get("signature") or ""), message)
        self._window(proof, now)
        self._revision(body, state)
        if signer_id in state.get("revoked_signers", []):
            raise AuthnError("PROOF_REVOKED", 403, "signer revoked", reason="revoked")
        if str(body.get("authorizer")) != record.get("principal_id"):
            raise AuthnError("NOT_AUTHORIZED", 403, "authorizer does not match the server principal", reason="empty_authorizer")
        actor = body.get("actor") if isinstance(body.get("actor"), dict) else {}
        if str(actor.get("id") or "") != record.get("principal_id") or signer_id != record.get("principal_id"):
            raise AuthnError("NOT_AUTHORIZED", 403, f"rejected {actor.get('id') or ''}", reason="identity_mismatch")
        principal = self._principal_from_record(record)
        nonce = str(proof.get("nonce") or "")
        if nonce == "":
            raise AuthnError("PROOF_INVALID", 403, "nonce rejected", reason="replay")
        slot = idempotency_digest(
            principal_id=principal.principal_id, kind="nonce", repo_id=principal.repo_id,
            app_id=principal.app_id, producer_id=principal.producer_id, stream_id=principal.stream_id,
            task_id="", credential_scope="", phase="", object_id="", operation="nonce",
            idempotency_key=nonce, target_app_id="",
        )
        if write and slot in state.get("nonces", {}):
            raise AuthnError("PROOF_REPLAY", 403, "nonce already used", reason="replay")
        return principal

    def authorize(self, principal: Principal, operation: str, resource_binding) -> None:
        authorize(principal, operation, resource_binding)

    def binding_for(self, principal: Principal, operation: str, body: dict):
        return binding_for(principal, operation, body)

    def consume_nonce(self, state: dict, principal: Principal, proof: dict, now: int, *, max_nonces: int) -> None:
        nonce = str(proof.get("nonce") or "")
        slot = idempotency_digest(
            principal_id=principal.principal_id, kind="nonce", repo_id=principal.repo_id,
            app_id=principal.app_id, producer_id=principal.producer_id, stream_id=principal.stream_id,
            task_id="", credential_scope="", phase="", object_id="", operation="nonce",
            idempotency_key=nonce, target_app_id="",
        )
        nonces = state["nonces"]
        if slot in nonces:
            raise AuthnError("PROOF_REPLAY", 403, "nonce already used", reason="replay")
        expired = [key for key, row in nonces.items() if int(row.get("expires_at") or 0) <= now]
        for key in expired:
            del nonces[key]
        if len(nonces) >= max_nonces:
            raise AuthnError("CAPACITY", 429, "nonce capacity exceeded", reason="capacity")
        nonces[slot] = {"principal_id": principal.principal_id, "expires_at": int(proof["expires_at"])}

    def _principal_from_record(self, record: dict) -> Principal:
        scopes = tuple(str(item) for item in record.get("scopes") or [])
        return Principal(
            principal_id=str(record.get("principal_id") or ""),
            kind=str(record.get("kind") or ""),
            repo_id=str(record.get("repo_id") or ""),
            app_id=str(record.get("app_id") or ""),
            producer_id=str(record.get("producer_id") or ""),
            stream_id=str(record.get("stream_id") or ""),
            scopes=scopes,
            trusted_source=server_source(str(record.get("kind") or "")),
        )

    def _route_gate(self, body: dict, route_class: str) -> None:
        actor = body.get("actor") if isinstance(body, dict) and isinstance(body.get("actor"), dict) else {}
        kind = str(actor.get("kind") or "")
        ref = str(actor.get("proof_ref") or "")
        testish = kind == "test" or ref.startswith("test-proof://")
        isolated_ok = route_class == "isolated" and self.isolated and self.registry.test_identities_enabled
        if not isolated_ok:
            reason = "test_identity_rejected" if testish else "production_verification_disabled"
            raise AuthnError("NOT_AUTHORIZED", 403, f"rejected {actor.get('id') or ''}", reason=reason)
        if kind != "test" or not ref.startswith("test-proof://"):
            raise AuthnError("NOT_AUTHORIZED", 403, f"rejected {actor.get('id') or ''}", reason="test_envelope_required")

    def _window(self, proof: dict, now: int) -> None:
        try:
            issued = int(proof["issued_at"])
            expires = int(proof["expires_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthnError("PROOF_INVALID", 403, "validity window rejected", reason="window") from exc
        if isinstance(proof["issued_at"], bool) or isinstance(proof["expires_at"], bool):
            raise AuthnError("PROOF_INVALID", 403, "validity window rejected", reason="window")
        if issued >= expires or issued > now or now >= expires:
            raise AuthnError("PROOF_EXPIRED", 403, "proof is outside its validity window", reason="expired")

    def _revision(self, body: dict, state: dict) -> None:
        durable = self.policy_revision
        policy = state.get("policy")
        if isinstance(policy, dict) and "revision" in policy:
            durable = policy["revision"]
        if durable != self.policy_revision or body.get("policy_revision") != durable:
            raise AuthnError("NOT_AUTHORIZED", 403, "policy revision mismatch", reason="policy_revision")
