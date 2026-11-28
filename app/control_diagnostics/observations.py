"""Normalize supplied host observations. Nothing here executes a host command."""

from app.control_diagnostics.extract import KNOWN_OPERATIONS, SERVICE_IDS

_SERVICE_STATES = frozenset({
    "running",
    "loaded_idle",
    "last_exit_failed",
    "not_visible",
    "unknown",
    "unsupported",
})


def normalize_observation(observation):
    if not isinstance(observation, dict):
        return _quarantine("SCHEMA_UNSUPPORTED", "Observation payload is not an object.")
    operation = observation.get("operation")
    parameters = observation.get("parameters", {})
    if parameters not in ({}, None):
        return _quarantine(
            "SCHEMA_UNSUPPORTED",
            "Diagnostic operations accept no parameters and were not executed.",
        )
    if operation not in KNOWN_OPERATIONS:
        return _quarantine(
            "SCHEMA_UNSUPPORTED",
            "Operation is outside the read-only diagnostic set. Dispatch was not expanded and was not executed.",
            extra={"operation": operation if isinstance(operation, str) else None, "dispatch_expansion": False},
        )
    system = observation.get("system")
    if system is None and isinstance(observation.get("result"), dict):
        system = observation["result"].get("system")
    if operation == "host.health":
        body = _health(observation.get("result"), system)
    elif operation == "service.inventory":
        body = _services(observation.get("result"), system, observation.get("inventory_source"))
    else:
        body = _snapshot(observation.get("result"), system, observation.get("inventory_source"))
    body.update({
        "operation": operation,
        "executed": False,
        "launchctl_invoked": False,
        "dispatch_expansion": False,
        "dispatch_switch": {
            "value": None,
            "evidence_state": "missing",
            "reason": "The local dispatch switch was not read.",
        },
        "runtime_evidence": False,
        "origin": "synthetic_fixture",
    })
    return {
        "disposition": "stream",
        "http": 200,
        "code": None,
        "evidence_state": body["evidence_state"],
        "diagnostic_class": "observation",
        "severity": "info",
        "body": body,
        "intervention": None,
        "executed": False,
    }


def _health(result, system):
    supplied_cpu = isinstance(result, dict) and result.get("cpu_percent") is not None
    supplied_memory = isinstance(result, dict) and result.get("memory_percent") is not None
    disk = None
    disk_evidence = "missing"
    if isinstance(result, dict) and isinstance(result.get("disk"), dict):
        disk = {
            "total_bytes": result["disk"].get("total_bytes"),
            "used_bytes": result["disk"].get("used_bytes"),
            "free_bytes": result["disk"].get("free_bytes"),
        }
        if all(isinstance(disk[key], int) and not isinstance(disk[key], bool) for key in disk):
            disk_evidence = "present"
        else:
            disk = None
            disk_evidence = "unknown"
    return {
        "kind": "host.health",
        "system": system if isinstance(system, str) else None,
        "system_evidence": "present" if isinstance(system, str) else "missing",
        "cpu_percent": None,
        "cpu_evidence": "missing",
        "memory_percent": None,
        "memory_evidence": "missing",
        "supplied_cpu_ignored": supplied_cpu,
        "supplied_memory_ignored": supplied_memory,
        "disk": disk,
        "disk_evidence": disk_evidence,
        "evidence_state": "present" if isinstance(system, str) or disk_evidence == "present" else "missing",
    }


def _services(result, system, inventory_source):
    rows = _five(result, system, inventory_source)
    evidence_values = {row["evidence_state"] for row in rows}
    if evidence_values == {"present"}:
        evidence_state = "present"
    elif "unknown" in evidence_values or "missing" in evidence_values:
        evidence_state = "unknown" if system is None else "missing"
    else:
        evidence_state = "present"
    return {
        "kind": "service.inventory",
        "system": system if isinstance(system, str) else None,
        "system_evidence": "present" if isinstance(system, str) else "missing",
        "services": rows,
        "cpu_percent": None,
        "cpu_evidence": "missing",
        "memory_percent": None,
        "memory_evidence": "missing",
        "evidence_state": evidence_state,
        "inventory_source": inventory_source if inventory_source == "observed" else "not_observed",
    }


def _snapshot(result, system, inventory_source):
    health = _health(result.get("health") if isinstance(result, dict) else None, system)
    services = _services(result.get("services") if isinstance(result, dict) else None, system, inventory_source)
    supplied = []
    if isinstance(result, dict) and isinstance(result.get("capabilities"), list):
        supplied = [item for item in result["capabilities"] if isinstance(item, str)]
    accepted = sorted(item for item in supplied if item in KNOWN_OPERATIONS)
    rejected = sorted(item for item in supplied if item not in KNOWN_OPERATIONS)
    return {
        "kind": "agent.snapshot",
        "health": health,
        "services": services["services"],
        "capabilities": accepted or sorted(KNOWN_OPERATIONS),
        "rejected_capabilities": rejected,
        "transport": "local_only",
        "cpu_percent": None,
        "cpu_evidence": "missing",
        "memory_percent": None,
        "memory_evidence": "missing",
        "evidence_state": services["evidence_state"],
        "system": services["system"],
    }


def _five(result, system, inventory_source):
    claimed = {}
    if isinstance(result, list):
        for row in result:
            if isinstance(row, dict) and row.get("service_id") in SERVICE_IDS:
                claimed[row["service_id"]] = row.get("state")
    rows = []
    for service_id in SERVICE_IDS:
        claim = claimed.get(service_id)
        if not isinstance(system, str):
            rows.append(_row(service_id, "unknown", "unknown", claim))
        elif system != "Darwin":
            rows.append(_row(service_id, "unsupported", "present", claim, rejected=claim not in (None, "unsupported")))
        elif inventory_source != "observed":
            rows.append(_row(service_id, "unknown", "missing", claim, rejected=claim not in (None, "unknown")))
        elif claim in _SERVICE_STATES:
            rows.append(_row(service_id, claim, "synthetic_present", claim))
        else:
            rows.append(_row(service_id, "unknown", "unknown", claim, rejected=True))
    return rows


def _row(service_id, state, evidence_state, claim, rejected=False):
    public_evidence = "present" if evidence_state == "synthetic_present" else evidence_state
    return {
        "service_id": service_id,
        "state": state,
        "evidence_state": public_evidence,
        "runtime_evidence": False,
        "claimed_state": claim if rejected else None,
        "claimed_state_rejected": rejected,
        "launchctl_invoked": False,
    }


def _quarantine(code, reason, extra=None):
    body = {
        "executed": False,
        "dispatch_expansion": False,
        "launchctl_invoked": False,
        "runtime_evidence": False,
        "evidence_state": "unknown",
        "outcome": None,
        "success": False,
        "reason": reason,
    }
    if extra:
        body.update(extra)
    return {
        "disposition": "quarantine",
        "http": 422,
        "code": code,
        "evidence_state": "unknown",
        "diagnostic_class": "observation",
        "severity": "warning",
        "body": body,
        "intervention": {
            "required": True,
            "category": "unsupported_operation",
        },
        "executed": False,
    }
