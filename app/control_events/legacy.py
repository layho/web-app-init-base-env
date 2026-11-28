"""Dual-read of the old operations log and comment protocol.

Rows stay legacy_unscoped. This reader does not invent producer_seq and does
not turn an old accepted comment into a schema 2 authorization.
"""

from pathlib import Path
import json
import re

from app.feedback_progress import EXECUTION_MARKER, MARKER, STATUSES, TRUSTED_ASSOCIATIONS
from app.operations import EVENT_CODES

from .redact import redact

KNOWN_AGENTS = {"Codex", "Grok", "Claude", "DeepSeek", "Qwen"}
SHA = re.compile(r"[0-9a-f]{40}")
PROTOCOL_MARKERS = (
    MARKER,
    "[APP-CLAIM] ",
    EXECUTION_MARKER,
    "[MACCI_REPORT] ",
    "[BASE_PUBLISHED]",
    "[BASE_REJECTION]",
    "[BASE_REPAIR_REVIEW]",
    "[BASE_DEPLOY_REPAIR_REVIEW]",
)
STATUS_CONTEXTS = {
    "macmini/isolated-tests-v1",
    "macmini/progress-v1",
    "macmini/deployment-v1",
}


def _blank(extra: dict | None = None) -> dict:
    record = {
        "legacy_unscoped": True,
        "producer_seq": None,
        "run_id": None,
        "authorizes": False,
        "schema_version": 1,
    }
    if extra:
        record.update(extra)
    return record


def read_operations(path: Path) -> dict:
    try:
        if path.is_dir():
            return {"kind": "read_failed", "reason": "not_a_file", "rows": [], "format_errors": 0, "total": 0}
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"kind": "read_failed", "reason": "not_found", "rows": [], "format_errors": 0, "total": 0}
    except OSError:
        return {"kind": "read_failed", "reason": "os_error", "rows": [], "format_errors": 0, "total": 0}
    if text.strip() == "":
        return {
            "kind": "empty",
            "rows": [],
            "format_errors": 0,
            "total": 0,
            "truncated": False,
            "recent_view": [],
            "producer_seq_invented": False,
        }
    rows = []
    format_errors = 0
    for line in text.splitlines():
        if line == "":
            continue
        fields = line.split("\t")
        row = _blank({"raw": redact(line)})
        if len(fields) not in (3, 4):
            row["unrecognized"] = True
            row["format_error"] = True
            format_errors += 1
            rows.append(row)
            continue
        timestamp, code, sha = fields[:3]
        known = code in EVENT_CODES and SHA.fullmatch(sha) is not None
        row.update({"at": timestamp, "code": code, "sha": sha, "unrecognized": not known, "format_error": False})
        if len(fields) == 4:
            row["detail"] = redact(fields[3])
        rows.append(row)
    return {
        "kind": "ok",
        "rows": rows,
        "format_errors": format_errors,
        "total": len(rows),
        "truncated": len(rows) > 100,
        "recent_view": rows[-100:],
        "recent_limit": 100,
        "producer_seq_invented": False,
    }


def _looks_like_protocol(body: str) -> bool:
    if any(body.startswith(marker) or marker in body.splitlines()[0] for marker in PROTOCOL_MARKERS):
        return True
    stripped = body.lstrip()
    if stripped.startswith("{") and any(context in stripped for context in STATUS_CONTEXTS):
        return True
    return False


def parse_comments(comments) -> dict:
    records = []
    format_errors = 0
    if not isinstance(comments, list):
        return {"records": [], "format_errors": 1}
    for comment in comments:
        if not isinstance(comment, dict):
            format_errors += 1
            continue
        body = comment.get("body")
        if not isinstance(body, str) or not _looks_like_protocol(body):
            continue
        if comment.get("author_association") not in TRUSTED_ASSOCIATIONS:
            records.append(_blank({"untrusted": True, "status": None}))
            continue
        parsed = _parse_body(body, comment)
        if parsed is None:
            format_errors += 1
            records.append(_blank({"format_error": True}))
            continue
        records.append(parsed)
    return {"records": records, "format_errors": format_errors}


def _parse_body(body: str, comment: dict) -> dict | None:
    if body.startswith("[APP-CLAIM] "):
        return _blank({"kind": "app_claim", "status": "claimed", "reply": redact(body[len("[APP-CLAIM] "):])})
    if body.startswith(EXECUTION_MARKER):
        try:
            receipt = json.loads(body[len(EXECUTION_MARKER):])
        except ValueError:
            return None
        if not isinstance(receipt, dict):
            return None
        return _blank({"kind": "app_execution", "execution": redact(receipt), "status": None})
    if body.startswith("[MACCI_REPORT] "):
        try:
            report = json.loads(body[len("[MACCI_REPORT] "):])
        except ValueError:
            return None
        if not isinstance(report, dict):
            return None
        return _blank({"kind": "macci_report", "report": redact(report), "status": None})
    for marker in ("[BASE_PUBLISHED]", "[BASE_REJECTION]", "[BASE_REPAIR_REVIEW]", "[BASE_DEPLOY_REPAIR_REVIEW]"):
        if marker in body:
            return _blank({"kind": marker.strip("[]").lower(), "status": None})
    stripped = body.lstrip()
    if stripped.startswith("{") and any(context in stripped for context in STATUS_CONTEXTS):
        try:
            payload = json.loads(stripped)
        except ValueError:
            return None
        if not isinstance(payload, dict) or payload.get("context") not in STATUS_CONTEXTS:
            return None
        return _blank({"kind": "commit_status", "context": payload.get("context"), "status": None})
    lines = [line for line in body.splitlines() if line.startswith(MARKER)]
    if len(lines) != 1:
        return None
    try:
        event = json.loads(lines[0][len(MARKER):])
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    status = event.get("status")
    if status not in STATUSES:
        return None
    agent = event.get("agent") if event.get("agent") in KNOWN_AGENTS else None
    record = _blank({
        "kind": "app_progress",
        "status": status,
        "agent": agent,
        "sha": event.get("sha") if isinstance(event.get("sha"), str) else None,
        "pr": event.get("pr") if type(event.get("pr")) is int else None,
        "candidate_id": event.get("candidate_id") if isinstance(event.get("candidate_id"), str) else None,
        "attempt": event.get("attempt") if type(event.get("attempt")) is int else 0,
    })
    if isinstance(event.get("run_id"), str) and event["run_id"]:
        record["run_id"] = event["run_id"]
    record["legacy_unscoped"] = record["run_id"] is None or "schema_version" not in event or event.get("schema_version") != 2
    record["authorizes"] = False
    record["producer_seq"] = None
    return redact(record)
