"""Redact untrusted diagnostic values before they are stored or returned.

Dictionaries keep only an explicit structural allowlist. Every other key is
omitted, including api_key, access_token, client_secret, Authorization,
Cookie, and names that only contain those words. Nested values are redacted
the same way. A string that still matches a known secret shape is replaced
with ``[REDACTED]`` rather than returned in part.

This module does not detect every unknown secret. A value with no recognized
shape can remain inside an allowlisted field.
"""

import json
import re

_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"ghp_[A-Za-z0-9]+"),
    re.compile(r"glpat-[A-Za-z0-9_\-]+"),
    re.compile(r"postgres(?:ql)?://\S+", re.I),
    re.compile(r"(?i)\b(?:bearer|basic)\s+\S+"),
    re.compile(
        r"""(?ix)
        ["']?
        (?:
            access[_-]?token | refresh[_-]?token | id[_-]?token | client[_-]?secret |
            api[_-]?key | x[_-]?api[_-]?key | app[_a-z0-9]*[_-]?token |
            set[_-]?cookie | authorization | cookie | password | passwd | secret | token
        )
        ["']?
        \s*[:=]\s*
        ["']?
        (?:\\.|[^\s,"'\\}])+
        """
    ),
)
_MARKERS = (
    "github_pat_",
    "ghp_",
    "glpat-",
    "BEGIN PRIVATE KEY",
    "PRIVATE KEY",
    "postgres://",
    "postgresql://",
    "Bearer ",
    "bearer ",
)
_SENSITIVE_PARTS = (
    "secret",
    "password",
    "passwd",
    "token",
    "authorization",
    "cookie",
    "apikey",
    "credential",
    "privatekey",
    "bearer",
)
_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
_ALLOWED = frozenset({
    "active_segment",
    "advisory_ignored",
    "after_seq",
    "app_id",
    "authoritative",
    "authority",
    "automatic_claim",
    "automatic_executor",
    "automatic_repair",
    "body",
    "bytes_read",
    "can_override",
    "candidate",
    "candidate_id",
    "capabilities",
    "category",
    "channel",
    "claim",
    "claimed_state",
    "claimed_state_rejected",
    "classification",
    "code",
    "column",
    "component",
    "container_phase",
    "cpu_evidence",
    "cpu_percent",
    "deployment",
    "deployment_id",
    "diagnostic",
    "diagnostic_class",
    "disk",
    "disk_evidence",
    "dispatch_expansion",
    "dispatch_switch",
    "disposition",
    "event_class",
    "events",
    "evidence_state",
    "executed",
    "expires_at",
    "failures",
    "fetched",
    "file",
    "free_bytes",
    "gap",
    "health",
    "human_intervention",
    "idempotency_scope",
    "interface",
    "intervention",
    "inventory_source",
    "items",
    "journal_epoch",
    "kind",
    "launchctl_invoked",
    "line",
    "local_seq",
    "memory_evidence",
    "memory_percent",
    "message",
    "missing_seq",
    "name",
    "occurred_at",
    "omitted_fields",
    "opened",
    "operation",
    "origin",
    "outcome",
    "payload_hash",
    "phase",
    "port",
    "producer_seq",
    "reason",
    "reason_code",
    "receipt_seq",
    "received_at",
    "record_id",
    "redaction",
    "ref",
    "rejected_capabilities",
    "rejected_channel",
    "rejected_class",
    "rejected_outcome",
    "rejected_state",
    "release_id",
    "repo_id",
    "repair_guidance",
    "required",
    "retained",
    "runtime_evidence",
    "run_id",
    "schema_version",
    "seconds",
    "service_id",
    "services",
    "severity",
    "side_effect",
    "state",
    "status",
    "stream_epoch",
    "stream_id",
    "subject_id",
    "success",
    "summary",
    "supplied_cpu_ignored",
    "supplied_memory_ignored",
    "synthetic",
    "system",
    "system_evidence",
    "task_id",
    "test",
    "timeout_phase",
    "total_bytes",
    "transport",
    "truncated",
    "unparsed_count",
    "used_bytes",
    "value",
    "values",
    "w01",
    "w02",
    "w04",
    "w07",
    "writes_comment",
    "writes_issue",
})


def redact_text(value):
    if not isinstance(value, str):
        return value
    cleaned = value
    for _ in range(6):
        updated = cleaned
        for pattern in _PATTERNS:
            updated = pattern.sub("[REDACTED]", updated)
        if updated == cleaned:
            break
        cleaned = updated
    if _still_sensitive(cleaned):
        return "[REDACTED]"
    return cleaned


def redact_value(value):
    cleaned = _walk(value, 0)
    if _residual_secret(cleaned):
        raise ValueError("redaction failed closed")
    return cleaned


def contains_secret(value):
    text = value if isinstance(value, str) else repr(value)
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _MARKERS)


def _still_sensitive(text):
    if contains_secret(text):
        return True
    return _PATTERNS[-1].search(text) is not None


def _residual_secret(value):
    try:
        rendered = json.dumps(value, ensure_ascii=True, default=str)
    except (TypeError, ValueError):
        return True
    return contains_secret(rendered)


def _walk(value, depth):
    if depth > 12:
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value
    if isinstance(value, list):
        if len(value) > 200:
            return ["[REDACTED]"]
        return [_walk(item, depth + 1) for item in value]
    if isinstance(value, dict):
        cleaned = {}
        omitted = []
        for key, item in value.items():
            name = key if isinstance(key, str) else str(key)
            if _unsafe_key(name):
                omitted.append("[REDACTED]")
                continue
            if name in _ALLOWED and not _sensitive_key(name):
                cleaned[name] = _walk(item, depth + 1)
                continue
            omitted.append("[REDACTED]")
        if omitted:
            existing = cleaned.get("omitted_fields", [])
            if not isinstance(existing, list):
                existing = []
            cleaned["omitted_fields"] = sorted(set(str(item) for item in existing) | set(omitted))
        return cleaned
    return "[REDACTED]"


def _unsafe_key(name):
    if not isinstance(name, str) or _KEY.fullmatch(name) is None:
        return True
    return _embeds_marker(name)


def _embeds_marker(name):
    lowered = name.lower()
    return any(marker.lower() in lowered for marker in _MARKERS)


def _sensitive_key(name):
    folded = re.sub(r"[^a-z0-9]", "", name.lower())
    return any(part in folded for part in _SENSITIVE_PARTS)
