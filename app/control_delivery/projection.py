"""Chain projection for F06 and F07.

A missing acceptance decision is not filled from an older overall status.
A historical blocked flag does not replace the current chain phase.
Closed superseded pull requests are left out of the failure list.
"""

from __future__ import annotations

_PHASE = {
    "OPEN": "claim",
    "TESTING": "tests",
    "INTEGRATING": "integrate",
    "DEPLOYING": "deploy",
    "ACCEPTING": "accept",
    "SATISFIED": "satisfied",
    "PUBLISHED": "published",
    "FAILED": "failed",
    "REJECTED": "rejected",
    "SUPERSEDED": "superseded",
}

_ORDER = ("claim", "tests", "integrate", "deploy", "accept", "satisfied", "published")


def project_chain(chain):
    decision = chain.get("acceptance_decision")
    legacy = chain.get("legacy_overall_status")
    phase = _PHASE.get(chain.get("state"), chain.get("state"))
    # A stored historical_blocked flag is not allowed to replace this phase.
    blocked_overrode = False
    steps = []
    current_index = _ORDER.index(phase) if phase in _ORDER else -1
    for index, name in enumerate(_ORDER):
        if current_index < 0:
            state = "pending"
        elif index < current_index:
            recorded = chain.get("recorded_steps", {}).get(name)
            state = "complete" if recorded else "unrecorded"
        elif index == current_index:
            state = "failed" if phase in {"failed", "rejected"} else "complete"
        else:
            state = "pending"
        steps.append({"key": name, "state": state})
    failures = []
    if chain.get("state") == "FAILED" and not chain.get("closed_superseded"):
        failures.append({
            "chain_id": chain["chain_id"],
            "pr_number": chain.get("pr_number"),
            "candidate_id": chain.get("candidate_id"),
        })
    return {
        "chain_id": chain["chain_id"],
        "pr_number": chain.get("pr_number"),
        "state": chain.get("state"),
        "phase": phase,
        "acceptance_decision": decision,
        "fell_back_to_legacy_accepted": False,
        "legacy_overall_status_ignored": legacy,
        "blocked_overrode_phase": blocked_overrode,
        "steps": steps,
        "failures": failures,
        "evidence_valid": bool(chain.get("evidence_valid")),
        "candidate_id": chain.get("candidate_id"),
        "draft": bool(chain.get("draft")),
        "freeze_is_acceptance": False,
    }


def project_task(task, chains):
    views = [project_chain(chain) for chain in chains]
    failures = []
    for chain, view in zip(chains, views):
        if chain.get("state") == "SUPERSEDED" or chain.get("closed_superseded"):
            continue
        failures.extend(view["failures"])
    return {
        "task_id": task["task_id"] if task else None,
        "phase": None if task is None else task.get("phase"),
        "milestone_source": "required_chains",
        "single_developer_pr_decides_task": False,
        "chains": views,
        "failures": failures,
        "task_acceptance": None if task is None else task.get("task_acceptance"),
        "runtime_acceptance": False,
        "production_publish_authorized": False,
    }
