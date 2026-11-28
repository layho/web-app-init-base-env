"""W01 read port for the joint board.

Rows are read through the event store. A ``read_receipt`` inside the stored
payload is not authority. The receipt returned here is minted from the
``payload_hash`` column, which is the digest W01 recorded at append time.
A missing file is empty. A database error is ``blocked``, not an empty page.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

from app.control_diagnostics.safe_io import UnsafePath, lstat_at, open_root
from app.control_events.engine import _identity
from app.control_events.redact import redact

_PAGE = 50
_SCAN = 2000


class TrustedReadError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def query_applied(
    root: Path,
    *,
    repo_id: str,
    app_id: str,
    task_ids: set[str],
    stream_id: str | None,
    limit: int = _PAGE,
    after_rowid: int = 0,
) -> dict:
    if type(limit) is not int or isinstance(limit, bool) or limit < 1 or limit > 100:
        raise TrustedReadError("page")
    if type(after_rowid) is not int or isinstance(after_rowid, bool) or after_rowid < 0:
        raise TrustedReadError("page")
    path = Path(root) / "events.sqlite3"
    if not path.exists():
        return _page([], truncated=False, next_rowid=None, bad=0, missing=True)
    root_fd = None
    file_fd = None
    connection = None
    try:
        root_fd = open_root(root)
        info = lstat_at(root_fd, "events.sqlite3")
        if stat.S_ISLNK(info.st_mode) or stat.S_ISFIFO(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise TrustedReadError("special")
        if info.st_nlink != 1:
            raise TrustedReadError("hardlink")
        file_fd = os.open("events.sqlite3", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=root_fd)
        checked = os.fstat(file_fd)
        if not stat.S_ISREG(checked.st_mode) or checked.st_nlink != 1:
            raise TrustedReadError("special")
        # SQLite WAL sidecars live beside this path. The descriptor above already
        # refused a symlink, FIFO, or hard link, and this inode must still match.
        current = path.lstat()
        if current.st_ino != checked.st_ino or current.st_dev != checked.st_dev or stat.S_ISLNK(current.st_mode):
            raise TrustedReadError("special")
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        return _collect(
            connection,
            repo_id=repo_id,
            app_id=app_id,
            task_ids=task_ids,
            stream_id=stream_id,
            limit=limit,
            after_rowid=after_rowid,
        )
    except TrustedReadError:
        raise
    except UnsafePath as exc:
        raise TrustedReadError(exc.kind if exc.kind else "unsafe") from exc
    except sqlite3.Error as exc:
        raise TrustedReadError("sqlite") from exc
    except OSError as exc:
        raise TrustedReadError("os") from exc
    finally:
        if connection is not None:
            connection.close()
        if file_fd is not None:
            os.close(file_fd)
        if root_fd is not None:
            os.close(root_fd)


def _collect(connection, *, repo_id, app_id, task_ids, stream_id, limit, after_rowid):
    items = []
    bad = 0
    cursor = after_rowid
    scanned = 0
    truncated = False
    while len(items) < limit and scanned < _SCAN:
        rows = connection.execute(
            """
            SELECT rowid, payload_hash, payload_json, received_at, applied
            FROM events
            WHERE applied=1 AND rowid>?
            ORDER BY rowid
            """,
            (cursor,),
        ).fetchmany(limit)
        if not rows:
            break
        scanned += len(rows)
        for row in rows:
            cursor = int(row["rowid"])
            minted = _mint(row)
            if minted is None:
                bad += 1
                continue
            if minted.get("repo_id") != repo_id or minted.get("app_id") != app_id:
                continue
            task_id = minted.get("task_id") or minted.get("subject_id")
            if task_id not in task_ids:
                continue
            if stream_id is not None and minted.get("stream_id") != stream_id:
                continue
            items.append(minted)
            if len(items) >= limit:
                break
        if len(items) >= limit:
            extra = connection.execute(
                "SELECT 1 FROM events WHERE applied=1 AND rowid>? LIMIT 1",
                (cursor,),
            ).fetchone()
            truncated = extra is not None
            break
        if len(rows) < limit:
            break
    else:
        if scanned >= _SCAN:
            extra = connection.execute(
                "SELECT 1 FROM events WHERE applied=1 AND rowid>? LIMIT 1",
                (cursor,),
            ).fetchone()
            truncated = extra is not None
    next_rowid = cursor if truncated else None
    return _page(items, truncated=truncated, next_rowid=next_rowid, bad=bad, missing=False)


def _mint(row):
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    authority = row["payload_hash"]
    if not isinstance(authority, str) or not authority:
        return None
    # W01 hashes the event before it stamps received_at. A receipt that was
    # not part of that hash cannot be substituted for the column digest.
    checked = {key: value for key, value in payload.items() if key != "received_at"}
    if _identity(checked) != authority:
        return None
    event = redact(dict(payload))
    event.pop("read_receipt", None)
    event.pop("submit_receipt", None)
    event["payload_digest"] = authority
    received = row["received_at"] if isinstance(row["received_at"], str) and row["received_at"] else None
    if received is None:
        event.pop("received_at", None)
    else:
        event["received_at"] = received
    source = event.get("trusted_source")
    if not isinstance(source, str) or not source:
        source = ""
    event["read_receipt"] = {
        "kind": "read",
        "authority": "W01",
        "authority_digest": authority,
        "event_id": event.get("event_id"),
        "payload_digest": authority,
        "stream_id": event.get("stream_id"),
        "stream_epoch": event.get("stream_epoch"),
        "producer_seq": event.get("producer_seq"),
        "server_source": source,
        "fact_state": "ACCEPTED" if row["applied"] else None,
        "evidence": "verified",
        "received_at": received,
    }
    if event["read_receipt"]["authority_digest"] != authority:
        return None
    return event


def _page(items, *, truncated, next_rowid, bad, missing):
    return {
        "items": items,
        "truncated": truncated,
        "next_rowid": next_rowid,
        "bad": bad,
        "missing": missing,
        "blocked": False,
    }
