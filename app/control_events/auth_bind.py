"""Canonical authorization document for one event or one read.

W02 verifies this document. Every binding field is taken from the event or
from the resource named by the call. Nothing here is copied from a principal,
a request registry, or a hash of the producer id.
"""

from __future__ import annotations

POLICY_REVISION = 1

SCOPE_FOR_OPERATION = {
    "event.append": "event.append",
    "event.read": "event.read",
    "diagnostic.read": "read",
    "diagnostic.cursor": "diagnostic.cursor",
    "report.submit": "report.submit",
    "report.read": "report.read",
}

# A rejected event is not stored and does not consume producer_seq.
# Submit the valid event again at that same seq. A hold or a rejected
# state stays on its own stream; other streams keep accepting. Fill a gap
# with the missing historical or authoritative event, or seal the epoch
# with declare_counter_loss and continue on the next epoch. A low-authority
# observation is history and does not own the fact, so the authoritative
# owner can still advance it. An epoch that contains an unproven grant
# cannot close; seal it and open the next epoch with a proven grant.
STREAM_REPAIR = (
    "Rejected events are not stored and do not consume producer_seq; "
    "submit the valid event again at that seq. "
    "A hold stays on its own stream, so another stream can still accept. "
    "Fill a gap with the missing historical or authoritative event. "
    "Low-authority observations do not own the fact, so the authoritative owner can still advance it. "
    "An epoch that already contains an unproven grant cannot close; "
    "do not treat that grant as authority, and continue on a stream whose prefix is proven."
)


def host_text(value: object) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    return ""


def object_id_for(event: dict) -> str:
    kind = event.get("event_class")
    if kind == "task":
        return _text(event.get("run_id"))
    if kind == "deployment":
        return _text(event.get("deployment_id"))
    if kind == "authorization":
        return "|".join((
            _text(event.get("task_id")),
            _text(event.get("candidate_id")),
            _text(event.get("operation")),
        ))
    if kind == "diagnostic":
        return _text(event.get("subject_id"))
    if kind == "candidate":
        return _text(event.get("candidate_id"))
    if kind == "resource":
        return _text(event.get("resource_id"))
    if kind == "evidence":
        return _text(event.get("evidence_ref"))
    return _text(event.get("event_id"))


def guard_key(event: dict) -> tuple:
    operation = event.get("operation") if isinstance(event.get("operation"), str) and event.get("operation") else event.get("event_type")
    return (object_id_key(event), event.get("revision"), operation)


def object_id_key(event: dict) -> str | None:
    from .reducer import object_key

    return object_key(event)


def evidence_key(event: dict) -> tuple:
    return (object_id_key(event), event.get("revision"), event.get("operation"))


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def document_for_event(event: dict, *, operation: str, authorizer: str, path: str) -> dict:
    """Projection of an event into the W02 payload. Extra event fields stay unsigned."""

    return _document(
        operation=operation,
        authorizer=authorizer,
        repo_id=_text(event.get("repo_id")),
        app_id=_text(event.get("app_id")),
        producer_id=_text(event.get("producer_id")),
        stream_id=_text(event.get("stream_id")),
        task_id=_text(event.get("task_id")),
        object_id=object_id_for(event),
        phase=_text(event.get("stage")),
        candidate_id=_text(event.get("candidate_id")),
        head=_text(event.get("head")),
        base=_text(event.get("base")),
        merge=_text(event.get("merge")),
        tree=_text(event.get("tree")),
        idempotency_key=_text(event.get("idempotency_key")),
        expected_revision=_revision(event.get("expected_revision")),
        run_id=_text(event.get("run_id")),
        method="POST",
        path=path,
    )


def document_for_resource(
    resource: dict,
    *,
    operation: str,
    authorizer: str,
    path: str,
    method: str = "GET",
) -> dict:
    return _document(
        operation=operation,
        authorizer=authorizer,
        repo_id=_text(resource.get("repo_id")),
        app_id=_text(resource.get("app_id")),
        producer_id=_text(resource.get("producer_id")),
        stream_id=_text(resource.get("stream_id")),
        task_id=_text(resource.get("task_id")),
        object_id=_text(resource.get("object_id")) or _text(resource.get("stream_id")),
        phase=_text(resource.get("phase")),
        candidate_id=_text(resource.get("candidate_id")),
        head=_text(resource.get("head")),
        base=_text(resource.get("base")),
        merge=_text(resource.get("merge")),
        tree=_text(resource.get("tree")),
        idempotency_key=_text(resource.get("idempotency_key")) or operation,
        expected_revision=_revision(resource.get("expected_revision")),
        run_id=_text(resource.get("run_id")),
        method=method,
        path=path,
    )


def _document(
    *,
    operation: str,
    authorizer: str,
    repo_id: str,
    app_id: str,
    producer_id: str,
    stream_id: str,
    task_id: str,
    object_id: str,
    phase: str,
    candidate_id: str,
    head: str,
    base: str,
    merge: str,
    tree: str,
    idempotency_key: str,
    expected_revision: int,
    run_id: str,
    method: str,
    path: str,
) -> dict:
    scope_name = SCOPE_FOR_OPERATION[operation]
    scope = {
        "repo": repo_id,
        "task_id": task_id,
        "app_id": app_id,
        "credential_scope": scope_name,
        "phase": phase,
        "object_id": object_id,
    }
    return {
        "schema_version": 2,
        "operation": operation,
        "authorizer": authorizer,
        "repo_id": repo_id,
        "task_id": task_id,
        "app_id": app_id,
        "target_app_id": app_id,
        "producer_id": producer_id,
        "stream_id": stream_id,
        "object_id": object_id,
        "phase": phase,
        "action": "",
        "result": "",
        "candidate_id": candidate_id,
        "head": head,
        "base": base,
        "merge": merge,
        "tree": tree,
        "policy_revision": POLICY_REVISION,
        "credential_scope": scope_name,
        "credential_generation": 1,
        "scope": scope,
        "idempotency_key": idempotency_key,
        "expected_revision": expected_revision,
        "run_id": run_id,
        "lease_generation": 0,
        "lease_token": "",
        "session_id": "",
        "effect_id": "",
        "requested_operation": "",
        "scope_to_rotate": "",
        "target_signer": "",
        "recovery": "",
        "release_version": "",
        "method": method,
        "path": path,
        "query": "",
    }
