"""Read-only branch-protection observations.

A 403, an empty ruleset, a 404, or a partial document stays unproven.
This module has no write path toward GitHub rules.
"""

from __future__ import annotations

from .errors import ControlError

REQUIRED = ("main_protection", "preview_protection", "repo_rulesets", "org_rulesets", "bypass")


def observe(*, name: str, method: str, http_status: int, body) -> dict:
    if method != "GET":
        raise ControlError("NOT_AUTHORIZED", 403, "branch evidence is read-only", reason="rules_write_rejected")
    return {
        "name": name,
        "method": "GET",
        "http_status": http_status,
        "reason": _reason(http_status, body),
        "classification": "unproven",
        "rules_modified": False,
    }


def _reason(http_status: int, body) -> str:
    if http_status == 403:
        return "credential_cannot_read"
    if http_status == 404:
        return "not_visible"
    if http_status == 200 and body == []:
        return "empty_ruleset_not_proof"
    if http_status == 200:
        return "incomplete_without_enforcement_bypass_and_org_rules"
    return "not_proven"


def summarize(observations: list[dict]) -> dict:
    recorded = []
    for item in observations:
        if item.get("method") != "GET":
            raise ControlError("NOT_AUTHORIZED", 403, "branch evidence is read-only", reason="rules_write_rejected")
        recorded.append({
            "name": item.get("name"),
            "method": "GET",
            "http_status": item.get("http_status"),
            "reason": item.get("reason") or _reason(int(item.get("http_status") or 0), None),
            "classification": "unproven",
            "rules_modified": False,
        })
    names = {item["name"] for item in recorded}
    missing = [name for name in REQUIRED if name not in names]
    return {
        "overall": "unproven",
        "rules_modified": False,
        "bypass": "not_queried",
        "missing": missing,
        "observations": recorded,
        "note": "403, empty rulesets, and missing org or bypass reads are not evidence that protection is off or on.",
    }
