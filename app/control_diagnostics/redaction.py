"""Redact secrets before a diagnostic record is stored or returned."""

import re

_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"ghp_[A-Za-z0-9]+"),
    re.compile(r"glpat-[A-Za-z0-9_\-]+"),
    re.compile(r"postgres(?:ql)?://\S+", re.I),
    re.compile(r"(?i)\bBearer\s+\S+"),
    re.compile(r"(?i)\b(?:APP_[A-Z0-9_]*TOKEN|password|secret|api_key|token)\b\s*[:=]\s*\S+"),
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
_DROPPED_KEYS = {
    "raw",
    "raw_log",
    "log",
    "stdout",
    "stderr",
    "env",
    "environment",
    "token",
    "lease_token",
    "password",
    "secret",
    "authorization",
    "cookie",
    "database_url",
    "proof_ref",
    "signature",
}


def redact_text(value):
    if not isinstance(value, str):
        return value
    cleaned = value
    for _ in range(4):
        updated = cleaned
        for pattern in _PATTERNS:
            updated = pattern.sub("[REDACTED]", updated)
        if updated == cleaned:
            break
        cleaned = updated
    lowered = cleaned.lower()
    if any(marker.lower() in lowered for marker in _MARKERS):
        return "[REDACTED]"
    return cleaned


def redact_value(value):
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and value != value:
            return None
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        cleaned = {}
        dropped = []
        for key, item in value.items():
            name = str(key)
            if name.lower() in _DROPPED_KEYS:
                dropped.append(name)
                continue
            cleaned[name] = redact_value(item)
        if dropped:
            cleaned["omitted_fields"] = sorted(set(dropped) | set(cleaned.get("omitted_fields", [])))
        return cleaned
    return "[REDACTED]"


def contains_secret(value):
    text = value if isinstance(value, str) else repr(value)
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _MARKERS)
