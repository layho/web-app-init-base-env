"""Persist a W05 receipt as a delivery-journal transaction.

A receipt file that repeats its own SHA-256 and a pair of booleans is not an
issuance. The journal commit writes the transaction, the receipt projection,
and the issuance head in one locked mutation. A query accepts the receipt
only when that head still names the transaction digest.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.control_delivery.store import Journal

HEAD_KIND = "issuance-head"
HEAD_KEY = "w05"
_NAME = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def issue_w05_receipt(
    root: Path,
    *,
    operation_id: str,
    expected_revision: int,
    candidate_digest: str,
    repo_id: str,
    app_id: str,
    result: dict,
) -> dict:
    """Legacy standalone issuer is disabled: no business transaction, no proof.

    Retain the interface for existing callers, but never mint a receipt from
    an arbitrary result dictionary. The delivery service must commit a
    business operation and its evidence before an authority can issue one.
    """
    return {"status": "unknown", "wrote": False, "reason": "BUSINESS_TRANSACTION_REQUIRED"}


def feedback_submit_document(*, principal_id, producer_id, stream_id, repo_id,
                             task_id, app_id, payload, idempotency_key, expected_revision):
    """Canonical W02 document for the exact W05 feedback submission."""
    from app.control_events.auth_bind import _document
    document = _document(
        operation="feedback.write", authorizer=principal_id, repo_id=repo_id,
        app_id=app_id, producer_id=producer_id, stream_id=stream_id,
        task_id=task_id, object_id=payload["feedback_id"], phase="feedback.submit",
        candidate_id="", head="", base="", merge="", tree="", idempotency_key=idempotency_key,
        expected_revision=expected_revision, run_id="", method="POST",
        path="/isolated/w05/feedback",
    )
    document["action"] = "feedback.submit"
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False).encode("utf-8")
    document["result"] = hashlib.sha256(raw).hexdigest()
    return document


def verify_feedback_submit(engine, *, signed, scope, payload, idempotency_key, expected_revision):
    """Verify the business actor independently from the W01 producer signer.

    Signature verification is read-only here; W05 reserves the nonce in its
    own journal transaction with the matching outbox. Only the identical
    authorized intent can recover that reservation.
    """
    from app.control_events.auth_state import load_state
    from app.control_events.errors import ControlError

    try:
        state = load_state(engine.auth_path)
    except ControlError:
        return None
    return verify_feedback_submit_proof(
        engine.authz, state, repo_id=engine.repo_id, signed=signed, scope=scope,
        payload=payload, idempotency_key=idempotency_key, expected_revision=expected_revision)


def verify_feedback_submit_proof(authz, state, *, repo_id, signed, scope, payload,
                                 idempotency_key, expected_revision):
    """Verify the exact signed document against an isolated server registry.

    Rechecking uses the current policy, keys, revocations and validity
    window. An expired or revoked proof is unknown, not historical denial.
    """
    import time
    from app.control_authz import AuthnError, AuthzError

    if not isinstance(signed, dict) or scope.get("repo") != repo_id:
        return None
    try:
        actor, proof = signed["actor"], signed["proof"]
        principal_id = actor["id"]
        record = state["principals"].get(principal_id)
        if not isinstance(record, dict):
            return None
        expected = feedback_submit_document(
            principal_id=principal_id, producer_id=record["producer_id"],
            stream_id=record["stream_id"], repo_id=scope["repo"],
            task_id=scope["task_id"], app_id=scope.get("app_id", "base"),
            payload=payload, idempotency_key=idempotency_key, expected_revision=expected_revision,
        )
        if {k: v for k, v in signed.items() if k not in {"actor", "proof"}} != expected:
            return None
        principal = authz.authenticate(
            operation="feedback.write", body=signed, route_class="isolated",
            now=int(time.time()), state=state, write=False,
        )
        authz.authorize(principal, "feedback.write",
                        authz.binding_for(principal, "feedback.write", expected))
        if principal.principal_id != payload["author_id"]:
            return None
        nonce_key = hashlib.sha256(json.dumps([principal.principal_id, proof["nonce"]],
                                              separators=(",", ":")).encode()).hexdigest()
        return {"principal_id": principal.principal_id, "nonce_key": nonce_key,
                "authorization_digest": proof["payload_hash"]}
    except (AuthnError, AuthzError, KeyError, TypeError, ValueError, AttributeError):
        return None


def record_isolated_intent(engine, *, event: dict, sign_request) -> dict:
    """Send a staged W05 intent to the real isolated W01/W02 engine.

    The signer is supplied by server configuration, not request JSON. An
    accepted intent only proves that exact signed evidence is in W01; it is
    NOT a business commit receipt, permission to retry, or publish approval.
    The original W05 outbox remains unchanged.
    """
    from app.control_delivery.ports import event_payload_digest
    from app.control_events.auth_bind import event_business_digest
    from app.control_events.engine import Engine
    from app.control_events.errors import ControlError
    from app.control_events.schema import split_request
    from app.control_events.trusted_read import query_exact_event
    from datetime import datetime, timezone

    unknown = {"status": "unknown", "business_committed": False, "production_effective": False}
    if (not isinstance(engine, Engine) or not engine.isolated
            or engine.production_protocol_enabled or not callable(sign_request)
            or not isinstance(event, dict) or event.get("repo_id") != engine.repo_id
            or event.get("producer_id") != "w05" or event.get("stream_id") != "w05:task"):
        return unknown
    try:
        note = event["note"]
        digest = event_payload_digest(event)
        # The original intent digest alone is not enough to bind the outbox
        # coordinates. Sign both in separate schema-defined fields.
        body = {
            "schema_version": 2, "event_type": "delivery.intent.recorded",
            "event_class": "evidence", "event_id": event["event_id"],
            "repo_id": event["repo_id"], "app_id": event["app_id"],
            "task_id": event["task_id"], "producer_id": "w05", "stream_id": "w05:task",
            "stream_epoch": event["stream_epoch"], "producer_seq": event["producer_seq"],
            "occurred_at": datetime.fromtimestamp(event["occurred_at"], timezone.utc).isoformat(),
            "idempotency_key": digest, "expected_revision": 0,
            "run_id": event["run_id"], "evidence_ref": note, "note": note,
            "scope": {"repo": event["repo_id"], "task_id": event["task_id"]},
            "authorizer": "w05",
        }
        expected = split_request({**body, "actor": {"id": "w05"}})
        expected_digest = event_business_digest(expected)
        # The signing callback may add only the actor/proof envelope.
        # Give it a deep copy so mutation of scope cannot alter the reference.
        from copy import deepcopy
        signed = sign_request(deepcopy(body))
        if (not isinstance(signed, dict)
                or {key: value for key, value in signed.items() if key not in {"actor", "proof"}} != body
                or event_business_digest(split_request(signed)) != expected_digest):
            return unknown
        response = engine.append(signed)
        if response.get("http") != 200:
            return unknown
        # A replay may return the original held response even after a gap
        # was drained. The verified applied row, not the cached response,
        # is the final authority for this isolated intent.
        stored = query_exact_event(engine.root, event_id=body["event_id"],
                                   repo_id=body["repo_id"], app_id=body["app_id"],
                                   task_id=body["task_id"])
        if (not isinstance(stored, dict)
                or stored.get("event_type") != body["event_type"]
                or stored.get("read_receipt", {}).get("server_source") != "app_service"
                or event_business_digest(stored) != expected_digest):
            return unknown
        return {"status": "intent_verified", "event_id": body["event_id"],
                "event_digest": digest, "w01_receipt": stored["read_receipt"],
                "business_committed": False, "production_effective": False}
    except (ControlError, KeyError, TypeError, ValueError, OverflowError, OSError):
        return unknown


def issuance_digest(
    *,
    operation_id: str,
    expected_revision: int,
    candidate_digest: str,
    repo_id: str,
    app_id: str,
    result: dict,
    commit_seq: int,
    parent: str,
) -> str:
    body = {
        "app_id": app_id,
        "authority": "W05",
        "candidate_digest": candidate_digest,
        "commit_seq": commit_seq,
        "expected_revision": expected_revision,
        "operation_id": operation_id,
        "parent": parent,
        "repo_id": repo_id,
        "result": result,
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _same_transaction(current, operation_id, expected_revision, candidate_digest, repo_id, app_id, result) -> bool:
    return (
        isinstance(current, dict)
        and current.get("operation_id") == operation_id
        and current.get("expected_revision") == expected_revision
        and current.get("candidate_digest") == candidate_digest
        and current.get("repo_id") == repo_id
        and current.get("app_id") == app_id
        and current.get("result") == result
        and current.get("status") == "applied"
    )


def _binding_ok(operation_id, expected_revision, candidate_digest, repo_id, app_id, result) -> bool:
    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 80:
        return False
    if any(char not in _NAME for char in operation_id):
        return False
    if isinstance(expected_revision, bool) or type(expected_revision) is not int or expected_revision < 0:
        return False
    if not isinstance(candidate_digest, str) or len(candidate_digest) != 64:
        return False
    if any(char not in "0123456789abcdef" for char in candidate_digest):
        return False
    if not isinstance(repo_id, str) or not repo_id or len(repo_id) > 200:
        return False
    if not isinstance(app_id, str) or not app_id or len(app_id) > 200:
        return False
    if not isinstance(result, dict):
        return False
    try:
        json.dumps(result, sort_keys=True)
    except (TypeError, ValueError):
        return False
    return True
