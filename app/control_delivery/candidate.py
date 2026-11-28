"""Candidate identity from the W00 contract.

``candidate_id`` is ``cand_`` plus the sha256 of six UTF-8 lines and no
trailing newline: v1, repo_id, task_id, head, base, merge, tree.
"""

from __future__ import annotations

import hashlib
import re

_SHA = re.compile(r"[0-9a-f]{40}")


def valid_sha(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def candidate_id(repo_id, task_id, head, base, merge, tree):
    for label, value in (
        ("head", head),
        ("base", base),
        ("merge", merge),
        ("tree", tree),
    ):
        if not valid_sha(value):
            raise ValueError("invalid " + label)
    if not isinstance(repo_id, str) or not repo_id or not isinstance(task_id, str) or not task_id:
        raise ValueError("repo_id and task_id are required")
    raw = "\n".join(["v1", repo_id, task_id, head, base, merge, tree]).encode("utf-8")
    return "cand_" + hashlib.sha256(raw).hexdigest()
