"""Ed25519 proofs over the W00 canonical document.

The private key is an isolated test helper. Verification uses only the
server registry's public key. The signature covers payload_hash and is not
itself part of that hash.
"""

from __future__ import annotations

import binascii

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .canonical import PROOF_FIELDS, CanonicalError, payload_hash, payload_values, proof_message, scope_digest
from .errors import AuthnError

ECHO_FIELDS = (
    "operation",
    "repo_id",
    "object_id",
    "candidate_id",
    "head",
    "base",
    "merge",
    "tree",
    "policy_revision",
    "scope",
    "payload_hash",
    "alg",
)


def _authn(reason: str, detail: str, code: str = "PROOF_INVALID", status: int = 403) -> AuthnError:
    return AuthnError(code, status, detail, reason=reason)


def sign_request(private_key: Ed25519PrivateKey, *, signer_id: str, key_id: str, operation: str,
                 body: dict, declared_kind: str, declared_id: str, declared_proof_ref: str,
                 issued_at: int, expires_at: int, nonce: str) -> dict:
    payload = dict(body)
    payload["schema_version"] = 2
    payload["operation"] = operation
    digest = payload_hash(operation, payload)
    values = proof_values(
        operation, payload, digest,
        signer_id=signer_id, key_id=key_id, issued_at=issued_at, expires_at=expires_at,
        nonce=nonce, declared_kind=declared_kind, declared_id=declared_id,
        declared_proof_ref=declared_proof_ref,
    )
    message = proof_message(values)
    proof = dict(values)
    proof["issued_at"] = issued_at
    proof["expires_at"] = expires_at
    proof["policy_revision"] = payload["policy_revision"]
    proof["signature"] = private_key.sign(message).hex()
    signed = dict(payload)
    signed["actor"] = {"kind": declared_kind, "id": declared_id, "proof_ref": declared_proof_ref}
    signed["proof"] = proof
    return signed


def _proof_int(name: str, value) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _authn("window" if name in {"issued_at", "expires_at"} else "signature", "proof field rejected")
    return str(value)


def _proof_text(name: str, value) -> str:
    if not isinstance(value, str):
        raise _authn("signature", "proof field rejected")
    return value


def proof_values(operation: str, body: dict, digest: str, *, signer_id: str, key_id: str, issued_at: int,
                 expires_at: int, nonce: str, declared_kind: str, declared_id: str,
                 declared_proof_ref: str) -> dict[str, str]:
    payload_values(operation, body)
    return {
        "payload_hash": digest,
        "signer_id": _proof_text("signer_id", signer_id),
        "key_id": _proof_text("key_id", key_id),
        "issued_at": _proof_int("issued_at", issued_at),
        "expires_at": _proof_int("expires_at", expires_at),
        "nonce": _proof_text("nonce", nonce),
        "alg": "ed25519",
        "operation": operation,
        "repo_id": body["repo_id"],
        "object_id": body["object_id"],
        "candidate_id": body["candidate_id"],
        "head": body["head"],
        "base": body["base"],
        "merge": body["merge"],
        "tree": body["tree"],
        "policy_revision": str(body["policy_revision"]),
        "scope": scope_digest(body["scope"]),
        "declared_kind": declared_kind,
        "declared_id": declared_id,
        "declared_proof_ref": declared_proof_ref,
    }


def verify_signature(public_key_hex: str, signature_hex: str, message: bytes) -> None:
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        public_key.verify(bytes.fromhex(signature_hex), message)
    except (InvalidSignature, ValueError, binascii.Error) as exc:
        raise _authn("signature", "signature rejected") from exc


def checked_message(operation: str, body: dict, proof: dict) -> bytes:
    if not isinstance(proof, dict):
        actor = body.get("actor") if isinstance(body.get("actor"), dict) else {}
        declared = str(actor.get("id") or "")
        raise _authn("signature", f"rejected {declared}")
    if str(proof.get("alg") or "") != "ed25519":
        raise _authn("algorithm", "algorithm rejected")
    actor = body.get("actor") if isinstance(body.get("actor"), dict) else {}
    try:
        digest = payload_hash(operation, body)
        values = proof_values(
            operation, body, digest,
            signer_id=proof.get("signer_id"),
            key_id=proof.get("key_id"),
            issued_at=proof.get("issued_at"),
            expires_at=proof.get("expires_at"),
            nonce=proof.get("nonce"),
            declared_kind=str(actor.get("kind") or ""),
            declared_id=str(actor.get("id") or ""),
            declared_proof_ref=str(actor.get("proof_ref") or ""),
        )
    except CanonicalError as exc:
        raise AuthnError("SCHEMA_UNSUPPORTED", 422, "canonical encoding rejected", reason="canonical") from exc
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise _authn("payload_hash", "payload hash rejected")
    if str(proof.get("payload_hash") or "") != digest:
        raise _authn("payload_hash", "payload hash mismatch")
    for name in ECHO_FIELDS:
        if str(proof.get(name) or "") != values[name]:
            raise _authn("echo", f"proof field {name} does not match the body")
    for name in PROOF_FIELDS:
        if name not in proof:
            raise _authn("canonical", "proof field missing")
    try:
        return proof_message(values)
    except CanonicalError as exc:
        raise AuthnError("SCHEMA_UNSUPPORTED", 422, "canonical encoding rejected", reason="canonical") from exc
