"""Pre-authorization policy stored outside any candidate checkout."""

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
ORDINARY_GATES = ("macci_passed", "same_repo", "queue_allowed", "no_conflict")


class Decision:
    def __init__(self, allow: bool, code: str | None, scope_class: str):
        self.allow = allow
        self.code = code
        self.scope_class = scope_class


def _gates_ok(body: dict) -> bool:
    gates = body.get("gates")
    if not isinstance(gates, dict):
        return False
    if gates.get("frozen") is True:
        return False
    return all(gates.get(name) is True for name in ORDINARY_GATES)


def decide(principal: dict, operation: str, body: dict, policy: dict) -> Decision:
    action = body.get("action") if operation == "policy.evaluate" else operation
    action = str(action or "")
    if principal["kind"] == "candidate" and action == "release.publish":
        return Decision(False, "POLICY_DENIED", "denied")
    if action in HIGH_RISK:
        allowed = set(policy.get("high_risk_allow") or [])
        if action not in allowed:
            return Decision(False, "POLICY_DENIED", "high_risk")
        return Decision(True, None, "high_risk")
    if action == "preview.merge":
        ordinary = set(policy.get("ordinary_allow") or [])
        if action not in ordinary or principal["kind"] != "host" or not _gates_ok(body):
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
