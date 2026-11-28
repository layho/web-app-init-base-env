"""Persist a W05 receipt as a delivery-journal transaction.

A receipt file that repeats its own SHA-256 and a pair of booleans is not an
issuance. The journal commit writes the transaction, the receipt projection,
and the issuance head in one locked mutation. A query accepts the receipt
only when that head still names the transaction digest.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.control_delivery.store import Journal

HEAD_KIND = "issuance-head"
HEAD_KEY = "w05"
_NAME = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def issue_w05_receipt(
    root: Path,
    *,
    operation_id: str,
    expected_revision: int,
    candidate_digest: str,
    repo_id: str,
    app_id: str,
    result: dict,
) -> dict:
    """Commit one receipt. A conflict leaves the previous transaction in place."""

    if not _binding_ok(operation_id, expected_revision, candidate_digest, repo_id, app_id, result):
        return {"status": "unknown", "wrote": False}
    journal = Journal(Path(root) / "delivery")

    def mutator(view):
        current = view.read("transaction", operation_id)
        head = view.read(HEAD_KIND, HEAD_KEY) or {"revision": 0, "seq": 0, "head": "", "commits": {}}
        if not isinstance(head, dict) or not isinstance(head.get("commits"), dict):
            view.abort()
            return "unknown"
        if current is not None:
            if _same_transaction(current, operation_id, expected_revision, candidate_digest, repo_id, app_id, result):
                return "replay"
            view.abort()
            return "conflict"
        parent = head.get("head") if isinstance(head.get("head"), str) else ""
        commit_seq = int(head.get("seq") or 0) + 1
        digest = issuance_digest(
            operation_id=operation_id,
            expected_revision=expected_revision,
            candidate_digest=candidate_digest,
            repo_id=repo_id,
            app_id=app_id,
            result=result,
            commit_seq=commit_seq,
            parent=parent,
        )
        receipt_id = "rcpt_" + digest[:24]
        transaction = {
            "authority": "W05",
            "operation_id": operation_id,
            "expected_revision": expected_revision,
            "candidate_digest": candidate_digest,
            "repo_id": repo_id,
            "app_id": app_id,
            "result": result,
            "commit_seq": commit_seq,
            "parent": parent,
            "receipt_id": receipt_id,
            "issuance_digest": digest,
            "status": "applied",
        }
        commits = dict(head["commits"])
        commits[operation_id] = digest
        view.stage("transaction", operation_id, transaction)
        view.stage("receipt", operation_id, {
            "authority": "W05",
            "operation_id": operation_id,
            "expected_revision": expected_revision,
            "candidate_digest": candidate_digest,
            "repo_id": repo_id,
            "app_id": app_id,
            "result": result,
            "receipt_id": receipt_id,
            "status": "applied",
        })
        view.stage(HEAD_KIND, HEAD_KEY, {
            "revision": int(head.get("revision") or 0) + 1,
            "seq": commit_seq,
            "head": digest,
            "commits": commits,
        })
        return "applied"

    try:
        outcome = journal.mutate(mutator)
    except (OSError, ValueError, TypeError):
        return {"status": "unknown", "wrote": False}
    if outcome == "applied":
        return {"status": "applied", "wrote": True}
    if outcome == "replay":
        return {"status": "applied", "wrote": False, "replayed": True}
    if outcome == "conflict":
        return {"status": "conflict", "wrote": False}
    return {"status": "unknown", "wrote": False}


def issuance_digest(
    *,
    operation_id: str,
    expected_revision: int,
    candidate_digest: str,
    repo_id: str,
    app_id: str,
    result: dict,
    commit_seq: int,
    parent: str,
) -> str:
    body = {
        "app_id": app_id,
        "authority": "W05",
        "candidate_digest": candidate_digest,
        "commit_seq": commit_seq,
        "expected_revision": expected_revision,
        "operation_id": operation_id,
        "parent": parent,
        "repo_id": repo_id,
        "result": result,
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _same_transaction(current, operation_id, expected_revision, candidate_digest, repo_id, app_id, result) -> bool:
    return (
        isinstance(current, dict)
        and current.get("operation_id") == operation_id
        and current.get("expected_revision") == expected_revision
        and current.get("candidate_digest") == candidate_digest
        and current.get("repo_id") == repo_id
        and current.get("app_id") == app_id
        and current.get("result") == result
        and current.get("status") == "applied"
    )


def _binding_ok(operation_id, expected_revision, candidate_digest, repo_id, app_id, result) -> bool:
    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 80:
        return False
    if any(char not in _NAME for char in operation_id):
        return False
    if isinstance(expected_revision, bool) or type(expected_revision) is not int or expected_revision < 0:
        return False
    if not isinstance(candidate_digest, str) or len(candidate_digest) != 64:
        return False
    if any(char not in "0123456789abcdef" for char in candidate_digest):
        return False
    if not isinstance(repo_id, str) or not repo_id or len(repo_id) > 200:
        return False
    if not isinstance(app_id, str) or not app_id or len(app_id) > 200:
        return False
    if not isinstance(result, dict):
        return False
    try:
        json.dumps(result, sort_keys=True)
    except (TypeError, ValueError):
        return False
    return True
