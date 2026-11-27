"""Opaque lease tokens bound to principal, run, scope, and generation.

Phase subject identity follows the frozen contract: attempt increases on the
same repo, app, task, phase, and subject. Producer, stream, and principal
identify the holder of a round. They do not split that subject into another
active round. The phase list is the contract list. Chain phases bind the
candidate, so independent candidate chains stay parallel. Task-aggregate
phases bind the task. This is the lease fence. It is not a scheduler.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from .errors import ControlError

LEASE_TTL = 60
TASK_PHASES = frozenset({"feedback_sync", "claim", "close"})
CHAIN_PHASES = frozenset({
    "develop",
    "review",
    "test",
    "integrate",
    "preview_merge",
    "deploy",
    "accept",
    "publish",
})


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def phase_subject(phase: str, task_id: str, candidate_id: str) -> str:
    """Server subject for one phase.

    Chain phases use ``chain:{phase}:{candidate_id}``. Task phases use
    ``task:{phase}:{task_id}``. A client does not choose this value.
    """

    if phase in CHAIN_PHASES:
        if not candidate_id or not task_id:
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "chain phase requires a candidate subject", reason="subject")
        return f"chain:{phase}:{candidate_id}"
    if phase in TASK_PHASES:
        if candidate_id:
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "task phase subject is the task", reason="subject")
        if not task_id:
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "task phase requires a task subject", reason="subject")
        return f"task:{phase}:{task_id}"
    raise ControlError("SCHEMA_UNSUPPORTED", 422, "phase is not in the frozen contract", reason="phase")


def fence(run: dict | None, reason: str) -> ControlError:
    state = run["state"] if run else None
    return ControlError(
        "LEASE_FENCE_REJECTED",
        409,
        "lease rejected",
        reason=reason,
        extra={"changed": False, "state": state},
    )


def assert_bound(run: dict | None, *, principal_id: str, token: str, generation: int, now: int,
                 repo: str, operation_scope: str) -> None:
    if run is None:
        raise fence(None, "missing")
    if token is None or token == "" or str(token) == str(generation) or str(token) == str(run.get("generation")):
        raise fence(run, "token")
    try:
        requested = int(generation)
    except (TypeError, ValueError):
        raise fence(run, "generation") from None
    if requested != int(run["generation"]):
        raise fence(run, "generation")
    if not hmac.compare_digest(token_hash(str(token)), str(run.get("token_hash") or "")):
        raise fence(run, "token")
    if run.get("principal_id") != principal_id:
        raise fence(run, "principal")
    if run.get("scope") != repo or run.get("operation_scope") != operation_scope:
        raise fence(run, "scope")
    if int(now) >= int(run["expires_at"]):
        raise fence(run, "expired")


def assert_lease_scope(run: dict | None, *, candidate_id: str, app_id: str, producer_id: str, stream_id: str,
                       phase: str, task_id: str, repo_id: str, head: str, base: str, merge: str, tree: str) -> None:
    """Bind the lease to the candidate, the phase subject, and the full scope."""

    if run is None:
        raise fence(None, "missing")
    if str(run.get("candidate_id") or "") != candidate_id:
        raise fence(run, "candidate")
    if phase in CHAIN_PHASES:
        from app.control_authz.canonical import candidate_id_for

        if not all((head, base, merge, tree)) or candidate_id != candidate_id_for(repo_id, task_id, head, base, merge, tree):
            raise fence(run, "candidate")
        for name, value in (("head", head), ("base", base), ("merge", merge), ("tree", tree)):
            if str(run.get(name) or "") != value:
                raise fence(run, "candidate")
    subject = phase_subject(phase, task_id, candidate_id)
    if str(run.get("subject") or "") != subject:
        raise fence(run, "subject")
    if str(run.get("app_id") or "") != app_id:
        raise fence(run, "app")
    if str(run.get("producer_id") or "") != producer_id:
        raise fence(run, "producer")
    if str(run.get("stream_id") or "") != stream_id:
        raise fence(run, "stream")
    if str(run.get("phase") or "") != phase:
        raise fence(run, "phase")
    if str(run.get("task_id") or "") != task_id:
        raise fence(run, "task")
