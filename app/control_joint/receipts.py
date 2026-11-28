"""Trusted operation receipts from one execution authority.

W04 and W05 are not interchangeable. A W04 absence is a missing row in a
consistent snapshot whose persisted repository and application binding match.
A W05 JSON document with ``reconciled`` true, or a missing file, is not that
proof. An unrelated W05 read does not change a W04 result. When the snapshot
cannot be shown to be consistent, the status is ``unknown`` and must not be
used to retry the operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path

from app.control_diagnostics.safe_io import UnsafePath, lstat_at, open_directory, open_root, read_bounded
from app.control_diagnostics.sqlite_fd import verified_sqlite_copy


def query_operation_receipt(
    root: Path,
    *,
    operation_id: str,
    expected_revision: int,
    candidate_digest: str,
    repo_id: str,
    app_id: str,
    authority: str,
) -> dict:
    if authority == "W04":
        return _w04(root, operation_id, expected_revision, candidate_digest, repo_id, app_id)
    if authority == "W05":
        return _w05(root, operation_id, expected_revision, candidate_digest, repo_id, app_id)
    return {"status": "unknown"}


def _w04(root: Path, operation_id: str, expected_revision: int, candidate_digest: str, repo_id: str, app_id: str) -> dict:
    schedule = Path(root) / "scheduler"
    database = schedule / "scheduler.sqlite3"
    try:
        if database.is_symlink() or not database.is_file():
            return {"status": "unknown"}
    except OSError:
        return {"status": "unknown"}
    try:
        with verified_sqlite_copy(schedule, "scheduler.sqlite3") as copied:
            connection = sqlite3.connect(Path(copied).as_uri() + "?mode=ro", uri=True)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                checked = connection.execute("PRAGMA integrity_check").fetchone()
                if checked is None or checked[0] != "ok":
                    return {"status": "unknown"}
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "operation_receipts" not in tables or "repo_binding" not in tables or "tasks" not in tables:
                    return {"status": "unknown"}
                bound = {row["repo_id"] for row in connection.execute("SELECT repo_id FROM repo_binding")}
                if bound != {repo_id}:
                    return {"status": "unknown"}
                app_row = connection.execute("SELECT 1 FROM tasks WHERE app_id=? LIMIT 1", (app_id,)).fetchone()
                if app_row is None:
                    return {"status": "unknown"}
                row = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if row is None:
                    return {"status": "absent"}
                return _applied_row(row, operation_id, expected_revision, candidate_digest, repo_id, app_id)
            finally:
                connection.close()
    except (OSError, UnsafePath, sqlite3.Error):
        return {"status": "unknown"}


def _applied_row(row, operation_id, expected_revision, candidate_digest, repo_id, app_id) -> dict:
    if row["status"] != "applied":
        return {"status": "unknown"}
    try:
        result = json.loads(row["result_json"]) if row["result_json"] else {}
    except (TypeError, ValueError):
        return {"status": "unknown"}
    if not isinstance(result, dict):
        return {"status": "unknown"}
    found = {
        "status": "applied",
        "operation_id": row["operation_id"],
        "expected_revision": int(row["expected_revision"]),
        "candidate_digest": row["candidate_digest"],
        "repo_id": row["repo_id"],
        "app_id": row["app_id"],
        "result": result,
    }
    if not _matches(found, operation_id, expected_revision, candidate_digest, repo_id, app_id):
        return {"status": "unknown"}
    return found


def _w05(root: Path, operation_id: str, expected_revision: int, candidate_digest: str, repo_id: str, app_id: str) -> dict:
    """A complete persisted W05 receipt. ``reconciled`` JSON is not one.

    A missing file is unknown, not absence. The operation journal is not read,
    so a conflict there cannot change this result.
    """

    if not _safe_name(operation_id):
        return {"status": "unknown"}
    root_fd = None
    delivery_fd = None
    receipt_fd = None
    try:
        root_fd = open_root(root)
        try:
            info = lstat_at(root_fd, "delivery")
        except FileNotFoundError:
            return {"status": "unknown"}
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return {"status": "unknown"}
        delivery_fd = open_directory(root_fd, "delivery")
        try:
            folder = lstat_at(delivery_fd, "receipt")
        except FileNotFoundError:
            return {"status": "unknown"}
        if stat.S_ISLNK(folder.st_mode) or not stat.S_ISDIR(folder.st_mode):
            return {"status": "unknown"}
        receipt_fd = open_directory(delivery_fd, "receipt")
        filename = operation_id + ".json"
        try:
            file_info = lstat_at(receipt_fd, filename)
        except FileNotFoundError:
            return {"status": "unknown"}
        if stat.S_ISLNK(file_info.st_mode) or not stat.S_ISREG(file_info.st_mode) or file_info.st_nlink != 1:
            return {"status": "unknown"}
        document = json.loads(read_bounded(receipt_fd, filename, 65536).decode("utf-8"))
    except (OSError, UnicodeError, ValueError, UnsafePath):
        return {"status": "unknown"}
    finally:
        if receipt_fd is not None:
            os.close(receipt_fd)
        if delivery_fd is not None:
            os.close(delivery_fd)
        if root_fd is not None:
            os.close(root_fd)
    if not isinstance(document, dict):
        return {"status": "unknown"}
    if document.get("reconciled") is True and document.get("issued") is not True:
        return {"status": "unknown"}
    if not _issued_w05(document, operation_id, expected_revision, candidate_digest, repo_id, app_id):
        return {"status": "unknown"}
    result = document.get("result")
    return {
        "status": "applied",
        "operation_id": operation_id,
        "expected_revision": expected_revision,
        "candidate_digest": candidate_digest,
        "repo_id": repo_id,
        "app_id": app_id,
        "result": result,
    }


def _issued_w05(document, operation_id, expected_revision, candidate_digest, repo_id, app_id) -> bool:
    result = document.get("result")
    receipt_id = document.get("receipt_id")
    if document.get("authority") != "W05" or document.get("issued") is not True or document.get("persisted") is not True:
        return False
    if document.get("status") != "applied" or not isinstance(receipt_id, str) or not receipt_id:
        return False
    if not isinstance(result, dict):
        return False
    if (
        document.get("operation_id") != operation_id
        or document.get("expected_revision") != expected_revision
        or document.get("candidate_digest") != candidate_digest
        or document.get("repo_id") != repo_id
        or document.get("app_id") != app_id
    ):
        return False
    binding = {
        "app_id": app_id,
        "authority": "W05",
        "candidate_digest": candidate_digest,
        "expected_revision": expected_revision,
        "operation_id": operation_id,
        "receipt_id": receipt_id,
        "repo_id": repo_id,
        "result": result,
    }
    digest = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return document.get("binding_digest") == digest


def _matches(item, operation_id, expected_revision, candidate_digest, repo_id, app_id) -> bool:
    return (
        item.get("operation_id") == operation_id
        and item.get("expected_revision") == expected_revision
        and item.get("candidate_digest") == candidate_digest
        and item.get("repo_id") == repo_id
        and item.get("app_id") == app_id
    )


def _safe_name(value: str) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 80 and all(char.isalnum() or char in "._-" for char in value)
