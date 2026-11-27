"""Projection rules: transitions, revision authority, and per-run progress."""

import json

from .contract_view import contract
from .errors import ControlError

PROTECTED = {"authorization", "deployment", "candidate"}
PROGRESS_RANK = {
    "blocked": 0,
    "claimed": 1,
    "in_progress": 2,
    "development_finished": 3,
    "submitted": 4,
    "ready_for_review": 5,
    "accepted": 6,
}
MACHINE_OF = {
    "task": "run",
    "candidate": "candidate",
    "deployment": "deployment",
    "resource": "resource",
}


def _illegal(message: str) -> ControlError:
    return ControlError("ILLEGAL_TRANSITION", 409, message)


def machine(name: str) -> dict:
    entities = contract()["entities"]
    if name == "run":
        return entities["run"]
    return entities[name]


def check_transition(event_class: str, current: str | None, new: str | None, guard: str | None) -> None:
    machine_name = MACHINE_OF.get(event_class)
    if machine_name is None or new is None:
        return
    spec = machine(machine_name)
    if new not in spec["states"]:
        raise _illegal(f"{new} is not a {machine_name} state")
    if current is None:
        return
    if current == new:
        edges = [edge for edge in spec["transitions"] if edge["from"] == current and edge["to"] == new]
        if not edges:
            return
    else:
        edges = [edge for edge in spec["transitions"] if edge["from"] == current and edge["to"] == new]
        if not edges:
            raise _illegal(f"{current} cannot move to {new}")
    guards = [edge.get("guard") for edge in edges]
    if any(item is None for item in guards):
        return
    if guard not in guards:
        raise ControlError("EVIDENCE_MISSING", 409, "transition guard is missing")


def object_key(event: dict) -> str | None:
    kind = event["event_class"]
    if kind == "deployment":
        return "deployment:" + event["deployment_id"]
    if kind == "candidate":
        return "candidate:" + event["candidate_id"]
    if kind == "authorization":
        return "authorization:" + "|".join((event["task_id"], event["candidate_id"], event["operation"]))
    if kind == "resource":
        return "resource:" + event["resource_id"]
    if kind == "task":
        return "task:" + str(event.get("run_id") or event.get("task_id"))
    if kind == "evidence":
        return "evidence:" + str(event.get("evidence_ref"))
    if kind == "diagnostic":
        return "diagnostic:" + event["subject_id"]
    return None


def prefer_fact(existing: dict, incoming: dict, incoming_authoritative: bool) -> dict:
    if incoming["event_class"] == "diagnostic" and existing["event_class"] in PROTECTED:
        return existing
    if incoming.get("revision", 0) != existing["revision"]:
        return incoming if incoming.get("revision", 0) > existing["revision"] else existing
    if incoming_authoritative and not existing["authoritative"]:
        return incoming
    return existing


def progress_rank(status: str | None) -> int:
    return PROGRESS_RANK.get(status or "", -1)


def load_times(raw: str) -> dict:
    value = json.loads(raw or "{}")
    return value if isinstance(value, dict) else {}
