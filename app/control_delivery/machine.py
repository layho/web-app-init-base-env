"""Task-chain transitions and the W00 close condition.

Guards are the contract's guard strings. A missing edge is denied.
SATISFIED is not close. REJECTED does not return to TESTING.
"""

from __future__ import annotations

# (from, to) -> required guard, or None when the edge is unguarded.
CHAIN_EDGES = {
    ("OPEN", "TESTING"): None,
    ("TESTING", "INTEGRATING"): "test_succeeded",
    ("TESTING", "FAILED"): "test_failed",
    ("FAILED", "SUPERSEDED"): "new_attempt_new_run",
    ("TESTING", "SUPERSEDED"): "candidate_updated",
    ("INTEGRATING", "SUPERSEDED"): "candidate_updated",
    ("OPEN", "SUPERSEDED"): "candidate_updated",
    ("INTEGRATING", "DEPLOYING"): "exact_remote_observation",
    ("DEPLOYING", "ACCEPTING"): "deployment_committed",
    ("DEPLOYING", "SUPERSEDED"): "deployment_failed_replacement",
    ("ACCEPTING", "SATISFIED"): "user_acceptance",
    ("ACCEPTING", "REJECTED"): "user_rejection",
    ("REJECTED", "SUPERSEDED"): "repair_authorized_new_candidate",
    ("SATISFIED", "PUBLISHED"): "formal_publish_evidence",
}

COMPLETION_CONDITION = (
    "every required chain is PUBLISHED and each chain has publish_evidence "
    "and task_acceptance is recorded"
)

OBSERVATION_TARGETS = {
    ("INTEGRATING", "exact_remote_observation"): "DEPLOYING",
    ("DEPLOYING", "deployment_committed"): "ACCEPTING",
    ("SATISFIED", "formal_publish_evidence"): "PUBLISHED",
}


def allows(src, dst, guard):
    if (src, dst) not in CHAIN_EDGES:
        return False
    needed = CHAIN_EDGES[(src, dst)]
    return needed is None or needed == guard


def apply_guard(src, guard):
    target = OBSERVATION_TARGETS.get((src, guard))
    if target is None:
        return None
    if not allows(src, target, guard):
        return None
    return target


def completion(task, chains):
    required = [
        chain for chain in chains
        if chain.get("required") and chain.get("state") != "SUPERSEDED"
    ]
    reasons = []
    if not required:
        reasons.append("no_required_chain")
    for chain in required:
        if chain.get("state") != "PUBLISHED":
            reasons.append(chain["chain_id"] + ":not_published")
        if not chain.get("publish_evidence"):
            reasons.append(chain["chain_id"] + ":publish_evidence_missing")
    if not (task or {}).get("task_acceptance"):
        reasons.append("task_acceptance_missing")
    return {
        "condition": COMPLETION_CONDITION,
        "met": bool(required) and not reasons,
        "reasons": reasons,
        "required_chain_ids": [chain["chain_id"] for chain in required],
        "runtime_acceptance": False,
        "production_publish_authorized": False,
    }
