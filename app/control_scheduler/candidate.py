"""Candidate identity from the V0.6.0 contract.

The encoding is the W00 canonical form: six UTF-8 lines, no trailing newline,
then ``cand_`` plus the sha256 hex. This is a scheduler identity, not an
authenticator and not a copy of the W02 lease fence.
"""

from __future__ import annotations

import hashlib
import re

_SHA = re.compile(r"^[0-9a-f]{40}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def require_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def require_sha(value: str, label: str = "sha") -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def candidate_id(repo_id: str, task_id: str, head: str, base: str, merge: str, tree: str) -> str:
    raw = "\n".join(["v1", repo_id, task_id, head, base, merge, tree]).encode("utf-8")
    return "cand_" + hashlib.sha256(raw).hexdigest()


def normalize_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or "\\" in value:
        raise ValueError("invalid reserved path")
    if any(ord(char) < 32 for char in value):
        raise ValueError("invalid reserved path")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("invalid reserved path")
    return value.casefold()


def paths_overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")
