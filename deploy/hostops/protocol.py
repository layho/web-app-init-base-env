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


def verify_claim(claim, host_id, key, now):
    import hmac
    if type(claim) is not dict or set(claim)!={"job","signature","lease_token"}:
        raise ValueError("invalid claim")
    job=claim["job"]
    extra={"actor","nonce","policy_version","lease_sha256"}
    if type(job) is not dict or set(job)!=KEYS|extra or type(job["schema"]) is not int or job["schema"]!=2:
        raise ValueError("invalid signed envelope")
    if type(claim["signature"]) is not str or not re.fullmatch(r"[0-9a-f]{64}",claim["signature"]):
        raise ValueError("invalid signature")
    encoded=json.dumps(job,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode("ascii")
    expected=hmac.new(key,b"hostops/job/v2\0"+encoded,hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,claim["signature"]):
        raise ValueError("signature mismatch")
    token=claim["lease_token"]
    if type(token) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{43}",token):
        raise ValueError("invalid lease")
    if job["lease_sha256"]!=hashlib.sha256(token.encode("ascii")).hexdigest():
        raise ValueError("lease mismatch")
    if job["policy_version"]!="readonly-v1" or type(job["actor"]) is not str or not 1<=len(job["actor"])<=256:
        raise ValueError("policy mismatch")
    if type(job["nonce"]) is not str or not re.fullmatch(r"[0-9a-f]{32}",job["nonce"]):
        raise ValueError("invalid nonce")
    local={k:job[k] for k in KEYS}
    local["schema"]=1
    validate(local,host_id,now)
    if (timestamp(job["expires_at"])-timestamp(job["issued_at"])).total_seconds()>60:
        raise ValueError("lease too long")
    return job
