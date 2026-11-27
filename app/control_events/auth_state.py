"""Durable server auth state for the isolated engine.

This file stores public principal records, nonce slots, and the policy
revision. It does not derive a credential, verify a proof, or authorize a
request. Those steps belong to ``app.control_authz``.
"""

from __future__ import annotations

import json
from pathlib import Path

from .atomic import atomic_write
from .auth_bind import POLICY_REVISION


def empty_state() -> dict:
    return {
        "policy": {"revision": POLICY_REVISION},
        "principals": {},
        "nonces": {},
        "revoked_signers": [],
    }


def load_state(path: Path) -> dict:
    if not path.exists():
        return empty_state()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_state()
    if not isinstance(loaded, dict):
        return empty_state()
    state = empty_state()
    policy = loaded.get("policy")
    if isinstance(policy, dict) and isinstance(policy.get("revision"), int) and not isinstance(policy.get("revision"), bool):
        state["policy"] = {"revision": policy["revision"]}
    principals = loaded.get("principals")
    if isinstance(principals, dict):
        state["principals"] = principals
    nonces = loaded.get("nonces")
    if isinstance(nonces, dict):
        state["nonces"] = nonces
    revoked = loaded.get("revoked_signers")
    if isinstance(revoked, list):
        state["revoked_signers"] = revoked
    return state


def save_state(path: Path, state: dict) -> None:
    atomic_write(path, json.dumps(state, sort_keys=True).encode())
