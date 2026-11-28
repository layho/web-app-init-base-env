"""Trusted operation receipts from one execution authority.

W04 and W05 are not interchangeable. A W04 absence is a missing row in a
consistent snapshot whose persisted repository and application binding match.
A W05 file that only repeats its own digest is not that proof. A missing
W05 transaction is unknown, not absence. An unrelated W05 read does not change a W04 result. When the snapshot
cannot be shown to be consistent, the status is ``unknown`` and must not be
used to retry the operation.
"""

from __future__ import annotations

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
    """Fail closed until a real W05 business receipt authority is connected."""

    # Earlier standalone journal records were not tied to an authorized
    # delivery transaction. They cannot be grandfathered in as proof.
    # Enable this port only once the actual business CAS and trusted W01
    # acknowledgement are committed together and independently verified.
    return {"status": "unknown", "reason": "BUSINESS_TRANSACTION_REQUIRED"}


def _delivery_json(root: Path, kind: str, key: str):
    """Read one journal object. Missing and unsafe paths are both unknown."""

    root_fd = None
    delivery_fd = None
    folder_fd = None
    try:
        root_fd = open_root(root)
        try:
            info = lstat_at(root_fd, "delivery")
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return None
        delivery_fd = open_directory(root_fd, "delivery")
        try:
            folder = lstat_at(delivery_fd, kind)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(folder.st_mode) or not stat.S_ISDIR(folder.st_mode):
            return None
        folder_fd = open_directory(delivery_fd, kind)
        filename = key + ".json"
        try:
            file_info = lstat_at(folder_fd, filename)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(file_info.st_mode) or not stat.S_ISREG(file_info.st_mode) or file_info.st_nlink != 1:
            return None
        document = json.loads(read_bounded(folder_fd, filename, 65536).decode("utf-8"))
    except (OSError, UnicodeError, ValueError, UnsafePath):
        return None
    finally:
        if folder_fd is not None:
            os.close(folder_fd)
        if delivery_fd is not None:
            os.close(delivery_fd)
        if root_fd is not None:
            os.close(root_fd)
    return document if isinstance(document, dict) else None


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
