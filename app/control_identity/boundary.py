"""Web response boundary for the isolated W02 app.

CSP always names script-src. HSTS is added only for HTTPS. Error text is
scrubbed of credential-shaped strings before it leaves the process.
"""

from __future__ import annotations

import re

CSP = "default-src 'none'; script-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'"
HSTS = "max-age=31536000"

_SECRET = re.compile(
    r"github_pat_[A-Za-z0-9_]+"
    r"|ghp_[A-Za-z0-9]+"
    r"|gho_[A-Za-z0-9]+"
    r"|AKIA[0-9A-Z]{16}"
    r"|sk-[A-Za-z0-9]{8,}"
    r"|Bearer\s+[A-Za-z0-9._\-+/=]+"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|APP_[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD)[A-Z0-9_]*=\S+",
    re.IGNORECASE | re.DOTALL,
)


def redact(value: str) -> str:
    return _SECRET.sub("[redacted]", value)


def response_acceptable(csp: str, body: str, *, https: bool, hsts: str) -> bool:
    if "script-src" not in (csp or ""):
        return False
    lowered = body or ""
    if "github_pat_" in lowered or "BEGIN PRIVATE KEY" in lowered or "ghp_" in lowered:
        return False
    if https:
        return "max-age" in (hsts or "")
    return not hsts
