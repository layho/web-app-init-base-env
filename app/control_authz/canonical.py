"""W00 canonical encoding: version v1, U+001F field separators, domain separation.

Candidate ids keep the contract's newline encoding. Proof bytes use U+001F so
a value cannot absorb the next field. The shared contract file is not edited.
"""

from __future__ import annotations

import hashlib

UNIT = "\u001f"
PROOF_DOMAIN = "w00.control.proof.v1"
PAYLOAD_DOMAIN = "w00.control.payload.v1"
SCOPE_DOMAIN = "w00.control.scope.v1"
SECRET_DOMAIN = "w00.control.secret.v1"
IDEMPOTENCY_DOMAIN = "w00.control.idempotency.v1"
RATELIMIT_DOMAIN = "w00.control.ratelimit.v1"
QUERY_DOMAIN = "w00.control.query.v1"

DOMAIN = PROOF_DOMAIN

PROOF_FIELDS = (
    "payload_hash",
    "signer_id",
    "key_id",
    "issued_at",
    "expires_at",
    "nonce",
    "alg",
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
    "declared_kind",
    "declared_id",
    "declared_proof_ref",
)

PAYLOAD_FIELDS = (
    "schema_version",
    "operation",
    "authorizer",
    "repo_id",
    "task_id",
    "app_id",
    "target_app_id",
    "producer_id",
    "stream_id",
    "object_id",
    "phase",
    "action",
    "result",
    "candidate_id",
    "head",
    "base",
    "merge",
    "tree",
    "policy_revision",
    "credential_scope",
    "credential_generation",
    "scope.repo",
    "scope.task_id",
    "scope.app_id",
    "scope.credential_scope",
    "scope.phase",
    "scope.object_id",
    "scope_digest",
    "idempotency_key",
    "expected_revision",
    "run_id",
    "lease_generation",
    "lease_token",
    "session_id",
    "effect_id",
    "requested_operation",
    "scope_to_rotate",
    "target_signer",
    "recovery",
    "release_version",
    "method",
    "path",
    "query",
)

SCOPE_KEYS = ("repo", "task_id", "app_id", "credential_scope", "phase", "object_id")
INT_FIELDS = {"schema_version", "policy_revision", "credential_generation", "lease_generation", "expected_revision"}
SECRET_FIELDS = {"lease_token", "session_id"}
BODY_STRINGS = (
    "operation",
    "authorizer",
    "repo_id",
    "task_id",
    "app_id",
    "target_app_id",
    "producer_id",
    "stream_id",
    "object_id",
    "phase",
    "action",
    "result",
    "candidate_id",
    "head",
    "base",
    "merge",
    "tree",
    "credential_scope",
    "idempotency_key",
    "run_id",
    "effect_id",
    "requested_operation",
    "scope_to_rotate",
    "target_signer",
    "recovery",
    "release_version",
    "method",
    "path",
    "query",
)


class CanonicalError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _reject_text(value: str, reason: str) -> None:
    if UNIT in value or "\u001e" in value or "\n" in value or "\r" in value:
        raise CanonicalError(reason)


def canonical_bytes(fields: list[tuple[str, str]], *, domain: str) -> bytes:
    """Encode ``v1``, the domain, then name/value pairs, separated by U+001F."""

    if not isinstance(domain, str) or domain == "" or domain == "v1":
        raise CanonicalError("domain")
    _reject_text(domain, "domain")
    parts = ["v1", domain]
    seen = set()
    for name, value in fields:
        if not isinstance(name, str) or not isinstance(value, str) or name == "":
            raise CanonicalError("type")
        _reject_text(name, "separator")
        _reject_text(value, "separator")
        if name in seen:
            raise CanonicalError("duplicate")
        seen.add(name)
        parts.extend((name, value))
    return UNIT.join(parts).encode("utf-8")


def parse_canonical(raw: bytes) -> list[tuple[str, str]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CanonicalError("encoding") from exc
    parts = text.split(UNIT)
    if len(parts) < 2 or parts[0] != "v1" or (len(parts) - 2) % 2 != 0:
        raise CanonicalError("framing")
    fields = [("version", "v1"), ("domain", parts[1])]
    rest = parts[2:]
    for index in range(0, len(rest), 2):
        fields.append((rest[index], rest[index + 1]))
    return fields


def secret_digest(name: str, value: str) -> str:
    """Bind a secret without copying it into the signed document or the store."""

    if value == "":
        return ""
    material = canonical_bytes([("field", name), ("value", value)], domain=SECRET_DOMAIN)
    return hashlib.sha256(material).hexdigest()


def scope_digest(scope: dict) -> str:
    if not isinstance(scope, dict) or set(scope) != set(SCOPE_KEYS):
        raise CanonicalError("scope")
    pairs = []
    for key in SCOPE_KEYS:
        value = scope[key]
        if not isinstance(value, str):
            raise CanonicalError("scope_type")
        pairs.append((key, value))
    return hashlib.sha256(canonical_bytes(pairs, domain=SCOPE_DOMAIN)).hexdigest()


def canonical_query(query: str) -> str:
    if query == "":
        return ""
    if not isinstance(query, str) or query.startswith("?") or UNIT in query:
        raise CanonicalError("query")
    pairs = []
    seen = set()
    for piece in query.split("&"):
        if "=" not in piece:
            raise CanonicalError("query")
        key, value = piece.split("=", 1)
        if key == "" or key in seen:
            raise CanonicalError("query")
        seen.add(key)
        pairs.append((key, value))
    pairs.sort()
    return hashlib.sha256(canonical_bytes(pairs, domain=QUERY_DOMAIN)).hexdigest()


def payload_values(operation: str, body: dict) -> list[tuple[str, str]]:
    if not isinstance(body, dict):
        raise CanonicalError("body")
    allowed = set(PAYLOAD_FIELDS) - {"scope.repo", "scope.task_id", "scope.app_id", "scope.credential_scope", "scope.phase", "scope.object_id", "scope_digest"}
    allowed.update({"scope", "actor", "proof"})
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise CanonicalError("unsigned:" + ",".join(unknown))
    missing = sorted(name for name in allowed if name not in body and name not in {"actor", "proof"})
    if missing:
        raise CanonicalError("missing:" + ",".join(missing))
    if body.get("operation") != operation:
        raise CanonicalError("operation")
    if body.get("schema_version") != 2 or isinstance(body.get("schema_version"), bool):
        raise CanonicalError("schema_version")
    for name in INT_FIELDS:
        value = body[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise CanonicalError("type:" + name)
    for name in BODY_STRINGS:
        if not isinstance(body[name], str):
            raise CanonicalError("type:" + name)
    for name in SECRET_FIELDS:
        if not isinstance(body[name], str):
            raise CanonicalError("type:" + name)
    if not isinstance(body["authorizer"], str):
        raise CanonicalError("type:authorizer")
    digest = scope_digest(body["scope"])
    scope = body["scope"]
    matches = {
        "repo_id": scope["repo"],
        "task_id": scope["task_id"],
        "app_id": scope["app_id"],
        "credential_scope": scope["credential_scope"],
        "phase": scope["phase"],
        "object_id": scope["object_id"],
    }
    for name, expected in matches.items():
        if body[name] != expected:
            raise CanonicalError("scope_mismatch:" + name)
    encoded = {
        "schema_version": str(body["schema_version"]),
        "operation": operation,
        "authorizer": body["authorizer"],
        "scope.repo": scope["repo"],
        "scope.task_id": scope["task_id"],
        "scope.app_id": scope["app_id"],
        "scope.credential_scope": scope["credential_scope"],
        "scope.phase": scope["phase"],
        "scope.object_id": scope["object_id"],
        "scope_digest": digest,
    }
    for name in PAYLOAD_FIELDS:
        if name in encoded or name.startswith("scope."):
            continue
        if name in INT_FIELDS:
            encoded[name] = str(body[name])
        elif name in SECRET_FIELDS:
            encoded[name] = secret_digest(name, body[name])
        else:
            encoded[name] = body[name]
    return [(name, encoded[name]) for name in PAYLOAD_FIELDS]


def payload_hash(operation: str, body: dict) -> str:
    raw = canonical_bytes(payload_values(operation, body), domain=PAYLOAD_DOMAIN)
    return hashlib.sha256(raw).hexdigest()


def proof_message(values: dict[str, str], *, domain: str = PROOF_DOMAIN) -> bytes:
    missing = [name for name in PROOF_FIELDS if name not in values]
    if missing:
        raise CanonicalError("missing:" + ",".join(missing))
    ordered = [(name, values[name]) for name in PROOF_FIELDS]
    for name, value in ordered:
        if not isinstance(value, str):
            raise CanonicalError("type:" + name)
    return canonical_bytes(ordered, domain=domain)


def candidate_id_for(repo: str, task: str, head: str, base: str, merge: str, tree: str) -> str:
    raw = "\n".join(["v1", repo, task, head, base, merge, tree]).encode("utf-8")
    return "cand_" + hashlib.sha256(raw).hexdigest()


def idempotency_digest(*, principal_id: str, kind: str, repo_id: str, app_id: str, producer_id: str,
                       stream_id: str, task_id: str, credential_scope: str, phase: str, object_id: str,
                       operation: str, idempotency_key: str, target_app_id: str) -> str:
    pairs = [
        ("principal_id", principal_id),
        ("kind", kind),
        ("repo_id", repo_id),
        ("app_id", app_id),
        ("producer_id", producer_id),
        ("stream_id", stream_id),
        ("task_id", task_id),
        ("target_app_id", target_app_id),
        ("credential_scope", credential_scope),
        ("phase", phase),
        ("object_id", object_id),
        ("operation", operation),
        ("idempotency_key", idempotency_key),
    ]
    return hashlib.sha256(canonical_bytes(pairs, domain=IDEMPOTENCY_DOMAIN)).hexdigest()


def rate_limit_key(*, principal_id: str, kind: str, repo_id: str, app_id: str, operation: str) -> str:
    pairs = [
        ("principal_id", principal_id),
        ("kind", kind),
        ("repo_id", repo_id),
        ("app_id", app_id),
        ("operation", operation),
    ]
    return hashlib.sha256(canonical_bytes(pairs, domain=RATELIMIT_DOMAIN)).hexdigest()
