"""Digest of a scheduler candidate. Revision is included.

The manual-action fence does not use this value as its identity. Compare-and-swap
uses it together with expected_revision so a changed candidate is not a match.
"""

from __future__ import annotations

import hashlib
import json

DIGEST_FIELDS = (
    "repo_id",
    "app_id",
    "task_id",
    "chain_id",
    "candidate_id",
    "revision",
    "head",
    "base",
    "merge",
    "tree",
)


def candidate_digest(body: dict) -> str:
    encoded = {key: body.get(key) for key in DIGEST_FIELDS}
    raw = json.dumps(encoded, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()
