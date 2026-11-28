"""W05 task read port.

Files are opened from a directory file descriptor. A symlink, hard link, or
FIFO is refused. A missing directory is empty. A corrupt file is partial,
not a silent skip that looks complete.
"""

from __future__ import annotations

import json
import fcntl
import os
import stat
from pathlib import Path

from app.control_diagnostics.safe_io import UnsafePath, lstat_at, open_directory, open_root, read_bounded

_MAX = 65536
_NAME = 80


class DeliveryReadError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def query_tasks(root: Path, *, repo_id: str, app_id: str, task_ids: set[str], limit: int = 50, after: str | None = None) -> dict:
    if type(limit) is not int or isinstance(limit, bool) or limit < 1 or limit > 100:
        raise DeliveryReadError("page")
    directory = Path(root) / "delivery"
    if not directory.exists():
        return {"items": {}, "missing": True, "truncated": False, "bad": 0, "unscoped": 0, "next_cursor": None}
    root_fd = None
    delivery_fd = None
    task_fd = None
    lock_fd = None
    try:
        root_fd = open_root(root)
        info = lstat_at(root_fd, "delivery")
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise DeliveryReadError("special")
        delivery_fd = open_directory(root_fd, "delivery")
        # A read never recovers or creates a journal. A legacy directory
        # without a lock cannot prove a consistent snapshot.
        try:
            lock_fd = os.open(".journal.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=delivery_fd)
        except FileNotFoundError as exc:
            raise DeliveryReadError("journal_lock_missing") from exc
        locked = os.fstat(lock_fd)
        if not stat.S_ISREG(locked.st_mode) or locked.st_nlink != 1 or locked.st_uid != os.getuid() or locked.st_mode & 0o022:
            raise DeliveryReadError("journal_lock_unsafe")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeliveryReadError("journal_busy") from exc
        try:
            lstat_at(delivery_fd, ".journal.pending.json")
        except FileNotFoundError:
            pass
        else:
            raise DeliveryReadError("journal_recovery_pending")
        try:
            folder = lstat_at(delivery_fd, "task")
        except FileNotFoundError:
            return {"items": {}, "missing": True, "truncated": False, "bad": 0, "unscoped": 0, "next_cursor": None}
        if stat.S_ISLNK(folder.st_mode) or not stat.S_ISDIR(folder.st_mode):
            raise DeliveryReadError("special")
        task_fd = open_directory(delivery_fd, "task")
        names = sorted(name for name in os.listdir(task_fd) if name.endswith(".json") and len(name) <= _NAME)
        if after is not None:
            names = [name for name in names if name > after]
        items = {}
        bad = 0
        unscoped = 0
        used = []
        for name in names:
            if len(items) >= limit:
                break
            used.append(name)
            try:
                info = lstat_at(task_fd, name)
            except OSError as exc:
                raise DeliveryReadError("os") from exc
            if stat.S_ISLNK(info.st_mode) or stat.S_ISFIFO(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                bad += 1
                continue
            try:
                raw = read_bounded(task_fd, name, _MAX)
                document = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeError, ValueError, UnsafePath):
                bad += 1
                continue
            if not isinstance(document, dict):
                bad += 1
                continue
            if document.get("repo_id") != repo_id or document.get("app_id") != app_id:
                if not document.get("repo_id") or not document.get("app_id"):
                    unscoped += 1
                continue
            task_id = document.get("task_id")
            if task_id not in task_ids:
                continue
            items[task_id] = {
                "phase": document.get("phase") if isinstance(document.get("phase"), str) else None,
                "revision": document.get("revision") if type(document.get("revision")) is int else None,
                "run_id": document.get("run_id") if isinstance(document.get("run_id"), str) else None,
                "candidate_id": document.get("candidate_id") if isinstance(document.get("candidate_id"), str) else None,
                "authoritative": False,
                "source": "w05",
            }
        truncated = len(names) > len(used)
        return {
            "items": items,
            "missing": False,
            "truncated": truncated,
            "bad": bad,
            "unscoped": unscoped,
            "next_cursor": used[-1] if truncated and used else None,
        }
    except UnsafePath as exc:
        raise DeliveryReadError("unsafe") from exc
    except OSError as exc:
        raise DeliveryReadError("os") from exc
    finally:
        if task_fd is not None:
            os.close(task_fd)
        if lock_fd is not None:
            os.close(lock_fd)
        if delivery_fd is not None:
            os.close(delivery_fd)
        if root_fd is not None:
            os.close(root_fd)
