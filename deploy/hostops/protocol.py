"""Strict read-only job envelope validation; no transport or signing authority."""
import hashlib
import json
import re
from datetime import datetime, timezone

from diagnostics import OPERATIONS, execute

KEYS = frozenset({"schema", "task_id", "host_id", "operation", "parameters",
                  "issued_at", "expires_at", "parameter_sha256"})
IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}\Z")


def parameter_digest(parameters):
    if type(parameters) is not dict or parameters:
        raise ValueError("only empty parameters supported")
    return hashlib.sha256(b"{}").hexdigest()


def timestamp(value):
    if type(value) is not str or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value):
        raise ValueError("invalid UTC timestamp")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def validate(job, host_id, now):
    if type(job) is not dict or set(job) != KEYS:
        raise ValueError("invalid envelope fields")
    if type(job["schema"]) is not int or job["schema"] != 1:
        raise ValueError("unsupported schema")
    for value in (job["task_id"], job["host_id"], host_id):
        if type(value) is not str or not IDENTIFIER.fullmatch(value):
            raise ValueError("invalid identifier")
    if job["host_id"] != host_id:
        raise ValueError("host mismatch")
    operation = job["operation"]
    if type(operation) is not str or operation not in OPERATIONS:
        raise ValueError("unsupported operation")
    if parameter_digest(job["parameters"]) != job["parameter_sha256"]:
        raise ValueError("parameter digest mismatch")
    issued, expires = timestamp(job["issued_at"]), timestamp(job["expires_at"])
    if not issued <= now < expires or (expires-issued).total_seconds() > 300:
        raise ValueError("invalid job validity window")
    return job


def envelope_digest(job):
    return hashlib.sha256(json.dumps(job, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("ascii")).hexdigest()


def run_local_fixture(job, host_id, now):
    """Offline test harness only. Not an authenticated remote execution entrypoint."""
    validate(job, host_id, now)
    return {"task_id": job["task_id"], "envelope_sha256": envelope_digest(job),
            "transport": "local_fixture", "result": execute(job["operation"], job["parameters"])}
