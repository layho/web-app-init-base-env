"""Durable server registry for the isolated engine.

This file stores public principal records and the policy revision. Consumed
nonces are not authoritative here: they commit in the SQLite transaction
with the event or other write. A missing file is a new registry. A present
file that cannot be read as that registry fails closed. This module does
not derive a credential, verify a proof, or authorize a request.
"""

from __future__ import annotations

import json
from pathlib import Path

from .atomic import atomic_write
from .auth_bind import POLICY_REVISION
from .errors import ControlError


def empty_state() -> dict:
    return {
        "policy": {"revision": POLICY_REVISION},
        "principals": {},
        "nonces": {},
        "revoked_signers": [],
    }


def _corrupt(exc: Exception | None = None) -> ControlError:
    error = ControlError("AUTH_STATE_CORRUPT", 503, "auth state is corrupt")
    if exc is not None:
        raise error from exc
    raise error


def load_state(path: Path) -> dict:
    """Load a registry. Absence is empty. Corruption does not become empty."""

    if not path.exists():
        return empty_state()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        _corrupt(exc)
    if not isinstance(loaded, dict):
        _corrupt()
    policy = loaded.get("policy")
    principals = loaded.get("principals")
    nonces = loaded.get("nonces")
    revoked = loaded.get("revoked_signers")
    if not isinstance(policy, dict) or isinstance(policy.get("revision"), bool) or not isinstance(policy.get("revision"), int):
        _corrupt()
    if not isinstance(principals, dict) or not isinstance(nonces, dict) or not isinstance(revoked, list):
        _corrupt()
    if any(not isinstance(item, str) or item == "" for item in revoked):
        _corrupt()
    for principal_id, record in principals.items():
        if not isinstance(principal_id, str) or principal_id == "" or not isinstance(record, dict):
            _corrupt()
    for slot, row in nonces.items():
        if not isinstance(slot, str) or slot == "" or not isinstance(row, dict):
            _corrupt()
        expires = row.get("expires_at")
        principal = row.get("principal_id")
        if isinstance(expires, bool) or not isinstance(expires, int) or not isinstance(principal, str) or principal == "":
            _corrupt()
    return {
        "policy": {"revision": policy["revision"]},
        "principals": principals,
        "nonces": nonces,
        "revoked_signers": list(revoked),
    }


def save_state(path: Path, state: dict) -> None:
    atomic_write(path, json.dumps(state, sort_keys=True).encode())
