"""Trusted operation receipts from W04 and W05.

A chain that still exists, or a candidate id that still matches, is not a
receipt. ``applied`` requires a stored receipt whose operation id, revision,
and candidate digest match. ``absent`` is returned only after the authority
was read and the operation was not in it. A missing authority, a read error,
or a disagreement is ``unknown``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

from app.control_diagnostics.safe_io import UnsafePath, lstat_at, open_directory, open_root, read_bounded
from app.control_diagnostics.sqlite_fd import verified_sqlite_copy


def query_operation_receipt(root: Path, *, operation_id: str, expected_revision: int, candidate_digest: str, repo_id: str, app_id: str) -> dict:
    w04 = _w04(root, operation_id)
    w05 = _w05(root, operation_id)
    if w04["status"] == "unknown" or w05["status"] == "unknown":
        return {"status": "unknown"}
    applied = [item for item in (w04, w05) if item["status"] == "applied"]
    if applied:
        if any(item["status"] == "absent" for item in (w04, w05)):
            return {"status": "unknown"}
        if not all(_matches(item, operation_id, expected_revision, candidate_digest, repo_id, app_id) for item in applied):
            return {"status": "unknown"}
        chosen = applied[0]
        return {
            "status": "applied",
            "operation_id": operation_id,
            "expected_revision": expected_revision,
            "candidate_digest": candidate_digest,
            "result": chosen.get("result") or {},
        }
    if w04["status"] == "absent" and w05["status"] in {"absent", "missing"}:
        return {"status": "absent"}
    if w05["status"] == "absent" and w04["status"] == "missing":
        return {"status": "absent"}
    return {"status": "unknown"}


def _matches(item, operation_id, expected_revision, candidate_digest, repo_id, app_id) -> bool:
    return (
        item.get("operation_id") == operation_id
        and item.get("expected_revision") == expected_revision
        and item.get("candidate_digest") == candidate_digest
        and item.get("repo_id") == repo_id
        and item.get("app_id") == app_id
    )


def _w04(root: Path, operation_id: str) -> dict:
    schedule = Path(root) / "scheduler"
    database = schedule / "scheduler.sqlite3"
    if not database.is_file():
        return {"status": "missing"}
    try:
        with verified_sqlite_copy(schedule, "scheduler.sqlite3") as copied:
            connection = sqlite3.connect(Path(copied).as_uri() + "?mode=ro", uri=True)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "operation_receipts" not in tables:
                    return {"status": "unknown"}
                row = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
            finally:
                connection.close()
    except (OSError, UnsafePath, sqlite3.Error):
        return {"status": "unknown"}
    if row is None:
        return {"status": "absent"}
    if row["status"] != "applied":
        return {"status": "unknown"}
    try:
        result = json.loads(row["result_json"]) if row["result_json"] else {}
    except (TypeError, ValueError):
        return {"status": "unknown"}
    if not isinstance(result, dict):
        return {"status": "unknown"}
    return {
        "status": "applied",
        "operation_id": row["operation_id"],
        "expected_revision": int(row["expected_revision"]),
        "candidate_digest": row["candidate_digest"],
        "repo_id": row["repo_id"],
        "app_id": row["app_id"],
        "result": result,
    }


def _w05(root: Path, operation_id: str) -> dict:
    directory = Path(root) / "delivery"
    if not directory.exists():
        return {"status": "missing"}
    if not _safe_name(operation_id):
        return {"status": "unknown"}
    root_fd = None
    delivery_fd = None
    operation_fd = None
    try:
        root_fd = open_root(root)
        info = lstat_at(root_fd, "delivery")
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return {"status": "unknown"}
        delivery_fd = open_directory(root_fd, "delivery")
        try:
            folder = lstat_at(delivery_fd, "operation")
        except FileNotFoundError:
            return {"status": "missing"}
        if stat.S_ISLNK(folder.st_mode) or not stat.S_ISDIR(folder.st_mode):
            return {"status": "unknown"}
        operation_fd = open_directory(delivery_fd, "operation")
        filename = operation_id + ".json"
        try:
            file_info = lstat_at(operation_fd, filename)
        except FileNotFoundError:
            return {"status": "absent"}
        if stat.S_ISLNK(file_info.st_mode) or not stat.S_ISREG(file_info.st_mode) or file_info.st_nlink != 1:
            return {"status": "unknown"}
        document = json.loads(read_bounded(operation_fd, filename, 65536).decode("utf-8"))
    except (OSError, UnicodeError, ValueError, UnsafePath):
        return {"status": "unknown"}
    finally:
        if operation_fd is not None:
            os.close(operation_fd)
        if delivery_fd is not None:
            os.close(delivery_fd)
        if root_fd is not None:
            os.close(root_fd)
    if not isinstance(document, dict) or document.get("reconciled") is not True:
        return {"status": "unknown"}
    status = document.get("status")
    if status == "absent":
        return {"status": "absent"}
    if status != "applied":
        return {"status": "unknown"}
    result = document.get("result")
    if result is not None and not isinstance(result, dict):
        return {"status": "unknown"}
    return {
        "status": "applied",
        "operation_id": document.get("operation_id"),
        "expected_revision": document.get("expected_revision"),
        "candidate_digest": document.get("candidate_digest"),
        "repo_id": document.get("repo_id"),
        "app_id": document.get("app_id"),
        "result": result or {},
    }


def _safe_name(value: str) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 80 and all(char.isalnum() or char in "._-" for char in value)
