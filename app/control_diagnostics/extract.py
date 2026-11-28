"""Structured failure extraction for phases the legacy summary left empty.

The extractor stores grammar identifiers only. It does not keep raw lines,
and it does not treat an empty extract as a pass.
"""

import re

MAX_ITEMS = 12
MAX_LINE = 512

SERVICE_IDS = (
    "macci.runner",
    "macci.preview",
    "macci.publish",
    "macci.receipts",
    "panel.merge",
)
KNOWN_OPERATIONS = frozenset({"host.health", "service.inventory", "agent.snapshot"})

_PYTEST = re.compile(
    r"FAILED (tests/[A-Za-z0-9_/]{1,120}\.py)::([A-Za-z_][A-Za-z0-9_]{0,79})"
    r"(?:::(?:([A-Za-z_][A-Za-z0-9_]{0,79})))?(?:\[[^\r\n]*\])?(?: - .*)?"
)
_TS = re.compile(
    r"(?:frontend/)?(src/[A-Za-z0-9_/-]{1,120}\.tsx?)\((\d{1,6}),(\d{1,6})\): error (TS\d{4}):.*"
)
_COMPILE = re.compile(r"COMPILE ([A-Za-z0-9_./-]{1,160}):(\d{1,6}): error (C\d{1,6}):.*")
_DEPENDENCY = re.compile(
    r"DEPENDENCY ([A-Za-z0-9_.-]{1,80}) (missing|conflict|resolution_failed|registry_unreachable)"
)
_CONTAINER = re.compile(r"CONTAINER (build|run|start) (exit_nonzero|image_missing|oom|network_disabled)")
_TIMEOUT = re.compile(r"TIMEOUT (setup|fetch|context|dependencies|compile|test|startup|frontend) ([0-9]{1,6})")
_STARTUP = re.compile(
    r"STARTUP ([a-z0-9._-]{1,40}) (exit_nonzero|port_in_use|schema_check_failed|frontend_missing)"
)

_GRAMMARS = {
    "pytest": _PYTEST,
    "typescript": _TS,
    "compile": _COMPILE,
    "dependency": _DEPENDENCY,
    "container": _CONTAINER,
    "timeout": _TIMEOUT,
    "startup": _STARTUP,
}
_PHASES = {
    "pytest-failed": ("pytest",),
    "frontend-failed": ("typescript",),
    "compile-failed": ("compile",),
    "dependencies-failed": ("dependency",),
    "container-failed": ("container",),
    "context-failed": ("container",),
    "timeout-failed": ("timeout",),
    "test-failed": ("timeout", "pytest"),
    "startup-failed": ("startup",),
    "setup-failed": ("startup",),
    "fetch-failed": ("timeout",),
}
_FAILURE_PHASES = frozenset(_PHASES)


def _item_from(kind, match):
    if kind == "pytest":
        test = match[2] + ("::" + match[3] if match[3] else "")
        return {"file": match[1], "test": test, "category": "pytest_failure"}
    if kind == "typescript":
        return {
            "file": "frontend/" + match[1],
            "line": int(match[2]),
            "column": int(match[3]),
            "category": "typescript_error",
            "code": match[4],
        }
    if kind == "compile":
        path = match[1]
        if ".." in path.split("/"):
            return None
        return {"file": path, "line": int(match[2]), "code": match[3], "category": "compile_failure"}
    if kind == "dependency":
        return {"name": match[1], "reason_code": match[2], "category": "dependency_failure"}
    if kind == "container":
        return {"container_phase": match[1], "reason_code": match[2], "category": "container_failure"}
    if kind == "timeout":
        return {"timeout_phase": match[1], "seconds": int(match[2]), "category": "timeout"}
    return {"component": match[1], "reason_code": match[2], "category": "startup_failure"}


def summarize_failure(raw, phase):
    """Return a bounded summary. ``outcome`` is never a pass.

    A failure phase with no grammar match is ``unknown``, not an empty
    success. ``passed`` does not become success without separate evidence.
    """
    if not isinstance(raw, str) or not isinstance(phase, str):
        return {
            "phase": phase if isinstance(phase, str) else None,
            "items": [],
            "unparsed_count": 0,
            "evidence_state": "unknown",
            "outcome": None,
            "success": False,
            "runtime_evidence": False,
            "truncated": False,
        }
    grammars = _PHASES.get(phase, ())
    items = []
    unparsed = 0
    truncated = False
    for line in raw.splitlines():
        if len(line) > MAX_LINE:
            unparsed += 1
            continue
        matched = None
        for kind in grammars:
            found = _GRAMMARS[kind].fullmatch(line)
            if found:
                matched = _item_from(kind, found)
                break
        if matched is None:
            if line.strip():
                unparsed += 1
            continue
        if matched not in items:
            if len(items) >= MAX_ITEMS:
                truncated = True
                continue
            items.append(matched)
    failure_phase = phase in _FAILURE_PHASES or phase.endswith("-failed")
    if items:
        evidence_state = "present"
        outcome = "failed"
    elif failure_phase:
        evidence_state = "unknown"
        outcome = None
        items = [{
            "category": "unattributed",
            "phase": phase,
            "evidence_state": "unknown",
        }]
    else:
        evidence_state = "missing" if phase == "passed" else "unknown"
        outcome = None
    return {
        "phase": phase,
        "items": items,
        "unparsed_count": unparsed,
        "evidence_state": evidence_state,
        "outcome": outcome,
        "success": False,
        "runtime_evidence": False,
        "truncated": truncated,
        "origin": "structured_extract",
    }


def external_report_evidence(ref):
    """An external report that was not re-fetched is missing, not zero failures."""
    return {
        "ref": ref if isinstance(ref, str) else None,
        "evidence_state": "missing",
        "failures": None,
        "outcome": None,
        "success": False,
        "runtime_evidence": False,
        "side_effect": False,
        "reason": "External report was not re-fetched. An empty failure list is not evidence of success.",
    }
