"""Server principal records for the isolated W01 tests.

Private keys stay in this process. The engine only loads the public record.
Nothing here is a second ``app.control_authz``.
"""

from __future__ import annotations

import secrets
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.control_authz.proof import sign_request
from app.control_events.auth_bind import document_for_event, document_for_report, document_for_resource
from app.control_events.auth_state import save_state

_PRODUCERS = (
    ("fixture", "agent", "task", ""),
    ("w05", "app_service", "task", ""),
    ("fixture_b", "agent", "task", ""),
    ("fixture_c", "agent", "task", ""),
    ("host_deployer", "host", "deployment", "host-test"),
    ("other_deployer", "host", "deployment", "host-test"),
    ("side_observer", "agent", "deployment", "host-test"),
    ("policy_service", "policy_service", "authorization", ""),
    ("policy_service_b", "policy_service", "authorization", ""),
    ("auth_observer", "agent", "authorization", ""),
    ("hostops", "hostops", "diagnostic", ""),
    ("hostops_b", "hostops", "diagnostic", ""),
    ("macci-isolated", "macci", "evidence", ""),
)

_SCOPES = {
    "agent": ("event.append", "event.read"),
    "app_service": ("event.append", "event.read", "delivery.intent"),
    "host": ("event.append", "event.read"),
    "policy_service": ("event.append", "event.read", "preview.merge", "preview.observe"),
    "hostops": ("event.append", "event.read", "read", "diagnostic.cursor"),
    "macci": ("report.submit", "report.read", "event.read"),
}

_KEYS: dict[str, dict] = {}


def host_for(producer_id: str) -> str:
    for producer, _kind, _channel, host in _PRODUCERS:
        if producer == producer_id:
            return host
    return ""


def ensure_principals(engine) -> dict:
    root = str(engine.root)
    if root in _KEYS and engine.auth_path.exists():
        engine.reload_auth_state()
        return _KEYS[root]
    if engine.auth_path.exists():
        engine.reload_auth_state()
        return _KEYS.get(root, {})
    keys = {}
    principals = {}
    for producer, kind, channel, host in _PRODUCERS:
        private = Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        scopes = list(_SCOPES[kind]) + ["task:tsk_w01"]
        principals[producer] = {
            "principal_id": producer,
            "kind": kind,
            "key_id": producer + "-key",
            "public_key": public,
            "app_id": "base",
            "repo_id": engine.repo_id,
            "producer_id": producer,
            "stream_id": f"{producer}:{channel}",
            "host_id": host,
            "task_ids": ["tsk_w01"],
            "scopes": scopes,
            "test_identity": True,
        }
        keys[producer] = private
    save_state(engine.auth_path, {
        "policy": {"revision": 1},
        "principals": principals,
        "nonces": {},
        "revoked_signers": [],
    })
    _KEYS[root] = keys
    engine.reload_auth_state()
    return keys


def sign_event(engine, payload: dict, *, signer: str | None = None, nonce: str | None = None) -> dict:
    keys = ensure_principals(engine)
    signer = signer or payload.get("producer_id") or "fixture"
    authorizer = payload["authorizer"] if "authorizer" in payload else signer
    payload["authorizer"] = authorizer
    payload["actor"] = {
        "kind": "test",
        "id": authorizer if isinstance(authorizer, str) and authorizer else signer,
        "proof_ref": f"test-proof://w01/{signer}",
    }
    document = document_for_event(
        payload,
        operation="event.append",
        authorizer=authorizer,
        path="/api/v1/control/events",
    )
    signed = sign_request(
        keys[signer],
        signer_id=signer,
        key_id=signer + "-key",
        operation="event.append",
        body=document,
        declared_kind="test",
        declared_id=payload["actor"]["id"],
        declared_proof_ref=payload["actor"]["proof_ref"],
        issued_at=int(time.time()) - 5,
        expires_at=int(time.time()) + 86400,
        nonce=nonce or secrets.token_hex(16),
    )
    payload["proof"] = signed["proof"]
    payload["actor"] = signed["actor"]
    return payload


def sign_report(engine, report: dict, *, nonce: str | None = None) -> dict:
    keys = ensure_principals(engine)
    signer = "macci-isolated"
    report["authorizer"] = signer
    report["producer_id"] = report.get("producer_id") or signer
    document = document_for_report(report)
    signed = sign_request(
        keys[signer],
        signer_id=signer,
        key_id=signer + "-key",
        operation="report.submit",
        body=document,
        declared_kind="test",
        declared_id=signer,
        declared_proof_ref=f"test-proof://w01/{signer}",
        issued_at=int(time.time()) - 5,
        expires_at=int(time.time()) + 86400,
        nonce=nonce or secrets.token_hex(16),
    )
    report["proof"] = signed["proof"]
    report["actor"] = signed["actor"]
    return report


def sign_read(engine, producer: str, operation: str, **fields) -> dict:
    keys = ensure_principals(engine)
    resource = {
        "repo_id": engine.repo_id,
        "app_id": "base",
        "producer_id": producer,
        "task_id": "tsk_w01",
        "host_id": host_for(producer),
        "stream_id": "",
        "object_id": "",
    }
    resource.update(fields)
    if not resource["object_id"]:
        resource["object_id"] = resource["stream_id"]
    document = document_for_resource(
        resource,
        operation=operation,
        authorizer=producer,
        path="/api/v1/control/events",
        method=fields.get("method", "GET"),
    )
    signed = sign_request(
        keys[producer],
        signer_id=producer,
        key_id=producer + "-key",
        operation=operation,
        body=document,
        declared_kind="test",
        declared_id=producer,
        declared_proof_ref=f"test-proof://w01/{producer}",
        issued_at=int(time.time()) - 5,
        expires_at=int(time.time()) + 86400,
        nonce=fields.pop("nonce", None) or secrets.token_hex(16),
    )
    signed["host_id"] = resource["host_id"]
    return signed
