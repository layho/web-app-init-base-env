"""Pre-authorization policy stored outside any candidate checkout.

Gate evidence is server-injected. A missing flag denies. ``frozen`` must be
present and false; a missing freeze flag does not mean the candidate is
unfrozen. Unknown actions are denied. Production verification stays off.
"""

from __future__ import annotations

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


class Decision:
    def __init__(self, allow: bool, code: str | None, scope_class: str):
        self.allow = allow
        self.code = code
        self.scope_class = scope_class


def evidence_allows(policy: dict) -> bool:
    evidence = policy.get("evidence") if isinstance(policy, dict) else None
    if not isinstance(evidence, dict):
        return False
    if "frozen" not in evidence or "policy_revision" not in evidence:
        return False
    if evidence.get("policy_revision") != policy.get("revision"):
        return False
    if evidence.get("frozen") is not False:
        return False
    return all(evidence.get(name) is True for name in EVIDENCE_FLAGS)


def decide(principal, operation: str, body: dict, policy: dict) -> Decision:
    from app.control_authz.authorize import MATRIX

    action = body.get("action") if operation == "policy.evaluate" else operation
    if not isinstance(action, str) or action == "":
        return Decision(False, "POLICY_DENIED", "unknown_action")
    kind = principal.kind
    if kind == "candidate" and action == "release.publish":
        return Decision(False, "POLICY_DENIED", "denied")
    if action in HIGH_RISK:
        allowed = set(policy.get("high_risk_allow") or [])
        if action not in allowed:
            return Decision(False, "POLICY_DENIED", "high_risk")
        return Decision(True, None, "high_risk")
    if action not in MATRIX:
        return Decision(False, "POLICY_DENIED", "unknown_action")
    if action == "preview.merge":
        ordinary = set(policy.get("ordinary_allow") or [])
        if action not in ordinary or kind != "host" or not evidence_allows(policy):
            return Decision(False, "POLICY_DENIED", "ordinary")
        return Decision(True, None, "ordinary")
    if action == "release.publish":
        return Decision(True, None, "publish")
    return Decision(True, None, "ordinary")


def denied(decision: Decision) -> ControlError:
    return ControlError(
        "POLICY_DENIED",
        403,
        "policy denied",
        reason=decision.scope_class,
        extra={"allow": False, "scope_class": decision.scope_class},
    )
