"""Pre-authorization policy stored outside any candidate checkout.

Gate evidence is injected on the server and is bound to one candidate.
A missing flag, a missing tuple, or a validity window that does not cover
the server clock denies. Evidence for candidate A does not authorize
candidate B. Unknown actions are denied. A known action is not allowed
merely because its name is in the role matrix. A publisher role and the
publish scope are not authorization to publish. Acceptance and the
production gate stay separate, and production stays disabled.

``production_verification_enabled`` stays false. This module does not
enable a high-risk operation in production. A high-risk allow exists only
when a server grant names the object, the subject, the operation, and the
principal, and marks special authorization.
"""

from __future__ import annotations

from app.control_authz.authorize import MATRIX, _scopes_hold
from app.control_authz.canonical import candidate_id_for

from .errors import ControlError

HIGH_RISK = frozenset({
    "gate.change",
    "identity.change",
    "branch_rules.change",
    "host.privilege",
    "db.migrate",
    "hostops.dispatch",
    "hostops.write",
    "path.expand",
    "policy.change",
})
EVIDENCE_FLAGS = ("macci_passed", "same_repo", "queue_allowed", "no_conflict", "candidate_exact", "test_passed")
EVIDENCE_GATED = frozenset({"preview.merge", "deploy.apply", "draft.undraft"})
PUBLISH_NOT_AUTHORIZED = "publish_not_authorized"
TUPLE_FIELDS = ("head", "base", "merge", "tree")
ROUTINE = frozenset({
    "feedback.write",
    "pull.read",
    "diagnostic.read",
    "diagnostic.cursor",
    "event.append",
    "event.read",
    "report.submit",
    "report.read",
    "run.claim",
    "run.renew",
    "run.start",
    "run.complete",
    "run.release",
    "run.heartbeat",
    "run.reconcile",
    "ci.status.write",
    "deploy.switch",
    "release.accept",
    "release.reject",
    "proxy.rotate",
    "proxy.authorize",
    "policy.evaluate",
    "session.issue",
    "session.revoke",
    "session.check",
    "limit.consume",
    "github.effect",
    "branch.read",
    "signer.revoke",
})


class Decision:
    def __init__(self, allow: bool, code: str | None, scope_class: str):
        self.allow = allow
        self.code = code
        self.scope_class = scope_class


def _deny(scope_class: str) -> Decision:
    return Decision(False, "POLICY_DENIED", scope_class)


def evidence_allows(policy: dict, body: dict, *, now: int) -> bool:
    """True only for this request's candidate, repo, task, and validity window."""

    evidence = policy.get("evidence") if isinstance(policy, dict) else None
    if not isinstance(evidence, dict) or not isinstance(body, dict):
        return False
    if "frozen" not in evidence or "policy_revision" not in evidence:
        return False
    if evidence.get("policy_revision") != policy.get("revision"):
        return False
    if evidence.get("frozen") is not False:
        return False
    if not all(evidence.get(name) is True for name in EVIDENCE_FLAGS):
        return False
    repo = evidence.get("repo_id")
    task = evidence.get("task_id")
    if not isinstance(repo, str) or not repo or repo != body.get("repo_id"):
        return False
    if not isinstance(task, str) or not task or task != body.get("task_id"):
        return False
    parts = []
    for name in TUPLE_FIELDS:
        value = evidence.get(name)
        if not isinstance(value, str) or not value or value != body.get(name):
            return False
        parts.append(value)
    try:
        expected = candidate_id_for(repo, task, *parts)
    except (TypeError, ValueError):
        return False
    if evidence.get("candidate_id") != expected or body.get("candidate_id") != expected:
        return False
    start = evidence.get("not_before")
    end = evidence.get("not_after")
    if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
        return False
    if isinstance(now, bool) or not isinstance(now, int):
        return False
    if start >= end or now < start or now >= end:
        return False
    return True


def scopes_hold(principal, action: str, body: dict) -> bool:
    """Missing and unknown scopes deny. A role entry is not a scope grant."""

    return _scopes_hold(action, str(body.get("credential_scope") or ""), set(principal.scopes))


def _request_subject(body: dict) -> str:
    """Subject of a high-risk request, taken from signed phase and candidate fields.

    The canonical document has no separate client subject. The frozen phase
    rule is the subject. A missing phase or candidate does not become a subject.
    """

    phase = str(body.get("phase") or "")
    task_id = str(body.get("task_id") or "")
    candidate_id = str(body.get("candidate_id") or "")
    if phase == "":
        return ""
    from .lease import phase_subject

    try:
        return phase_subject(phase, task_id, candidate_id)
    except ControlError:
        return ""


def _high_risk_bound(principal, action: str, body: dict, policy: dict) -> bool:
    """A name in high_risk_allow is not a grant.

    The server grant must name this principal, this operation, an explicit
    object_id, and the phase subject derived from the signed request, and it
    must carry special authorization.
    """

    object_id = str(body.get("object_id") or "")
    subject = _request_subject(body)
    if object_id == "" or subject == "":
        return False
    grants = policy.get("high_risk_grants") if isinstance(policy, dict) else None
    if not isinstance(grants, list):
        return False
    for grant in grants:
        if not isinstance(grant, dict) or grant.get("special_authorization") is not True:
            continue
        if grant.get("principal_id") != principal.principal_id:
            continue
        if grant.get("operation") != action or grant.get("object_id") != object_id or grant.get("subject") != subject:
            continue
        return True
    return False


def decide(principal, operation: str, body: dict, policy: dict, *, now: int) -> Decision:
    """Allow only an explicit role, scope, and, where required, evidence.

    Membership in ``MATRIX`` is not an allow. ``policy.evaluate`` checks the
    target action, not the fact that evaluation itself is a known operation.
    """

    action = body.get("action") if operation == "policy.evaluate" else operation
    if not isinstance(action, str) or action == "":
        return _deny("unknown_action")
    if principal.kind == "candidate" and action == "release.publish":
        return _deny("denied")
    if action not in MATRIX:
        return _deny("unknown_action")
    if principal.kind not in MATRIX[action]:
        return _deny("role")
    if not scopes_hold(principal, action, body):
        return _deny("scope")
    if action in HIGH_RISK:
        if _high_risk_bound(principal, action, body, policy):
            return Decision(True, None, "high_risk")
        return _deny("high_risk")
    if action in EVIDENCE_GATED:
        ordinary = set(policy.get("ordinary_allow") or [])
        if action not in ordinary or not evidence_allows(policy, body, now=now):
            return _deny("ordinary")
        return Decision(True, None, "ordinary")
    if action == "release.publish":
        return Decision(False, None, PUBLISH_NOT_AUTHORIZED)
    if action in ROUTINE:
        return Decision(True, None, "ordinary")
    return _deny("unknown_action")


def denied(decision: Decision) -> ControlError:
    return ControlError(
        "POLICY_DENIED",
        403,
        "policy denied",
        reason=decision.scope_class,
        extra={"allow": False, "scope_class": decision.scope_class},
    )
