"""Domain-separated canonical encoding for W02 proofs.

Each field is a 4-byte length, the name, a 4-byte length, and the value.
Fields are joined by a unit separator so one value cannot absorb the next.
The domain is the first field and is not chosen by the caller.
"""

from __future__ import annotations

import hashlib

DOMAIN = "w02.control_identity.proof.v1"
PAYLOAD_DOMAIN = "w02.control_identity.payload.v1"
UNIT = b"\x1f"

PROOF_FIELDS = (
    "payload_hash",
    "signer_id",
    "key_id",
    "issued_at",
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
    "nonce",
    "expires_at",
    "alg",
    "declared_kind",
    "declared_id",
    "declared_proof_ref",
)

PAYLOAD_FIELDS = (
    "operation",
    "object_id",
    "repo_id",
    "task_id",
    "candidate_id",
    "head",
    "base",
    "merge",
    "tree",
    "policy_revision",
    "scope",
    "idempotency_key",
    "expected_revision",
    "action",
    "phase",
    "result",
    "app_id",
    "target_app_id",
    "run_id",
    "lease_generation",
    "credential_scope",
    "credential_generation",
    "session_id",
    "effect_id",
    "requested_operation",
    "scope_to_rotate",
    "gates",
    "recovery",
    "limit_key",
)

TUPLE_FIELDS = ("head", "base", "merge", "tree")


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _piece(name: str, value: str) -> bytes:
    if UNIT.decode("ascii") in name or UNIT.decode("ascii") in value or "\x1e" in value:
        raise ValueError("reserved separator in canonical field")
    name_raw = name.encode("utf-8")
    value_raw = value.encode("utf-8")
    return len(name_raw).to_bytes(4, "big") + name_raw + len(value_raw).to_bytes(4, "big") + value_raw


def canonical_bytes(fields: list[tuple[str, str]], *, domain: str) -> bytes:
    parts = [_piece("domain", domain)]
    parts.extend(_piece(name, value) for name, value in fields)
    return UNIT.join(parts)


def parse_canonical(raw: bytes) -> list[tuple[str, str]]:
    parsed = []
    for part in raw.split(UNIT):
        name_len = int.from_bytes(part[:4], "big")
        name = part[4:4 + name_len].decode("utf-8")
        rest = part[4 + name_len:]
        value_len = int.from_bytes(rest[:4], "big")
        value = rest[4:4 + value_len].decode("utf-8")
        if 4 + value_len != len(rest):
            raise ValueError("canonical field length mismatch")
        parsed.append((name, value))
    return parsed


def gates_text(gates) -> str:
    if not isinstance(gates, dict):
        return ""
    parts = []
    for key in sorted(gates):
        parts.append(f"{key}={_text(gates[key])}")
    return ",".join(parts)


def repo_of(body: dict) -> str:
    if body.get("repo_id"):
        return str(body["repo_id"])
    scope = body.get("scope")
    if isinstance(scope, dict) and scope.get("repo"):
        return str(scope["repo"])
    return ""


def credential_scope_of(body: dict) -> str:
    if body.get("credential_scope") is not None:
        return str(body.get("credential_scope") or "")
    return ""


def payload_values(operation: str, body: dict) -> dict[str, str]:
    source = {
        "operation": operation,
        "object_id": body.get("object_id", ""),
        "repo_id": repo_of(body),
        "task_id": body.get("task_id", ""),
        "candidate_id": body.get("candidate_id", ""),
        "head": body.get("head", ""),
        "base": body.get("base", ""),
        "merge": body.get("merge", ""),
        "tree": body.get("tree", ""),
        "policy_revision": body.get("policy_revision", ""),
        "scope": credential_scope_of(body),
        "idempotency_key": body.get("idempotency_key", ""),
        "expected_revision": body.get("expected_revision", ""),
        "action": body.get("action", ""),
        "phase": body.get("phase", ""),
        "result": body.get("result", ""),
        "app_id": body.get("app_id", ""),
        "target_app_id": body.get("target_app_id", ""),
        "run_id": body.get("run_id", ""),
        "lease_generation": body.get("lease_generation", ""),
        "credential_scope": credential_scope_of(body),
        "credential_generation": body.get("credential_generation", ""),
        "session_id": body.get("session_id", ""),
        "effect_id": body.get("effect_id", ""),
        "requested_operation": body.get("requested_operation", ""),
        "scope_to_rotate": body.get("scope_to_rotate", ""),
        "gates": gates_text(body.get("gates")),
        "recovery": body.get("recovery", ""),
        "limit_key": body.get("limit_key", ""),
    }
    return {name: _text(source.get(name, "")) for name in PAYLOAD_FIELDS}


def payload_hash(operation: str, body: dict) -> str:
    ordered = list(payload_values(operation, body).items())
    raw = canonical_bytes(ordered, domain=PAYLOAD_DOMAIN)
    return hashlib.sha256(raw).hexdigest()


def proof_field_values(operation: str, body: dict, headers: dict, declared_kind: str, declared_id: str,
                       declared_proof_ref: str) -> dict[str, str]:
    hashed = payload_hash(operation, body)
    payload = payload_values(operation, body)
    return {
        "payload_hash": hashed,
        "signer_id": _text(headers.get("signer_id")),
        "key_id": _text(headers.get("key_id")),
        "issued_at": _text(headers.get("issued_at")),
        "operation": operation,
        "object_id": payload["object_id"],
        "repo_id": payload["repo_id"],
        "candidate_id": payload["candidate_id"],
        "head": payload["head"],
        "base": payload["base"],
        "merge": payload["merge"],
        "tree": payload["tree"],
        "policy_revision": payload["policy_revision"],
        "scope": payload["scope"],
        "nonce": _text(headers.get("nonce")),
        "expires_at": _text(headers.get("expires_at")),
        "alg": _text(headers.get("alg")),
        "declared_kind": declared_kind,
        "declared_id": declared_id,
        "declared_proof_ref": declared_proof_ref,
    }


def proof_message(values: dict[str, str], *, domain: str = DOMAIN) -> bytes:
    missing = [name for name in PROOF_FIELDS if name not in values]
    if missing:
        raise ValueError("missing canonical fields: " + ",".join(missing))
    ordered = [(name, _text(values[name])) for name in PROOF_FIELDS]
    return canonical_bytes(ordered, domain=domain)


def candidate_id_for(repo: str, task: str, head: str, base: str, merge: str, tree: str) -> str:
    raw = "\n".join(["v1", repo, task, head, base, merge, tree]).encode("utf-8")
    return "cand_" + hashlib.sha256(raw).hexdigest()
