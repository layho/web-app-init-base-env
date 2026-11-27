"""Ed25519 proof construction and verification.

The server never stores a private key. ``sign_request`` is the isolated
client helper used to produce a proof; verification uses only the registered
public key.
"""

from __future__ import annotations

import binascii

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .canonical import proof_field_values, proof_message
from .errors import ControlError

ECHO_FIELDS = (
    "operation",
    "object_id",
    "repo_id",
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


def sign_request(private_key: Ed25519PrivateKey, *, signer_id: str, key_id: str, operation: str,
                 body: dict, declared_kind: str, declared_id: str, declared_proof_ref: str,
                 issued_at: int, expires_at: int, nonce: str) -> dict:
    headers = {
        "signer_id": signer_id,
        "key_id": key_id,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "nonce": nonce,
        "alg": "ed25519",
    }
    values = proof_field_values(operation, body, headers, declared_kind, declared_id, declared_proof_ref)
    message = proof_message(values)
    proof = dict(values)
    proof["signature"] = private_key.sign(message).hex()
    signed = dict(body)
    signed["schema_version"] = 2
    signed["actor"] = {"kind": declared_kind, "id": declared_id, "proof_ref": declared_proof_ref}
    signed["proof"] = proof
    return signed


def verify_signature(public_key_hex: str, signature_hex: str, message: bytes) -> None:
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        public_key.verify(bytes.fromhex(signature_hex), message)
    except (InvalidSignature, ValueError, binascii.Error) as exc:
        raise ControlError("PROOF_INVALID", 403, "signature rejected", reason="signature") from exc


def checked_message(operation: str, body: dict, proof: dict) -> bytes:
    if not isinstance(proof, dict):
        raise ControlError("PROOF_INVALID", 403, "proof missing", reason="signature")
    if str(proof.get("alg") or "") != "ed25519":
        raise ControlError("PROOF_INVALID", 403, "algorithm rejected", reason="algorithm")
    actor = body.get("actor") if isinstance(body.get("actor"), dict) else {}
    declared_kind = str(actor.get("kind") or "")
    declared_id = str(actor.get("id") or "")
    declared_proof_ref = str(actor.get("proof_ref") or "")
    headers = {
        "signer_id": proof.get("signer_id"),
        "key_id": proof.get("key_id"),
        "issued_at": proof.get("issued_at"),
        "expires_at": proof.get("expires_at"),
        "nonce": proof.get("nonce"),
        "alg": proof.get("alg"),
    }
    values = proof_field_values(operation, body, headers, declared_kind, declared_id, declared_proof_ref)
    digest = values["payload_hash"]
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ControlError("PROOF_INVALID", 403, "payload hash rejected", reason="payload_hash")
    presented = str(proof.get("payload_hash") or "")
    if presented != digest:
        raise ControlError("PROOF_INVALID", 403, "payload hash mismatch", reason="payload_hash")
    for name in ECHO_FIELDS:
        if str(proof.get(name) or "") != values[name]:
            raise ControlError("PROOF_INVALID", 403, f"proof field {name} does not match the body", reason="echo")
    try:
        return proof_message(values)
    except ValueError as exc:
        raise ControlError("SCHEMA_UNSUPPORTED", 422, "canonical encoding rejected", reason="canonical") from exc
