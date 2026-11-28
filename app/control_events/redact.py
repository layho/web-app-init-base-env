"""Strip secrets from isolated event responses and stored payloads."""

SECRET_KEYS = {
    "lease_token",
    "token",
    "secret",
    "password",
    "authorization",
    "github_token",
    "signature",
    "proof_ref",
}
SECRET_MARKERS = ("github_pat_", "ghp_", "sk-", "AKIA", "BEGIN PRIVATE KEY")


def redact(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if key in SECRET_KEYS:
                cleaned[key] = None
            else:
                cleaned[key] = redact(item)
        return cleaned
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and any(marker in value for marker in SECRET_MARKERS):
        return "[redacted]"
    return value
