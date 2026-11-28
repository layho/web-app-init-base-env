"""Consistent SQLite snapshots for trusted reads.

A POSIX shared lock plus a byte copy of the database and its WAL is not a
single point in time. Same-process ``fcntl`` locks do not stop a writer in
this process, and a checkpoint can move pages between the two files while
they are copied. A missing row in that pair is not proof the operation did
not commit.

The snapshot is SQLite's own backup. It runs only for a private file inside
a trusted exclusive directory, through a path that cannot leave that
directory. If the backup, the path check, or the size bound fails, the read
is blocked and must not be treated as absence.
"""

from __future__ import annotations

import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from app.control_diagnostics.safe_io import UnsafePath, lstat_at, open_root, proc_fd_path
from app.control_joint.sqlite_backup import backup_consistent

_MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
_SIDECARS = ("-wal", "-journal", "-shm")


@contextmanager
def verified_sqlite_copy(directory, name: str):
    if not isinstance(name, str) or not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise UnsafePath("database name escapes the directory", "blocked")
    root_fd = open_root(directory)
    db_fd = None
    private = None
    try:
        root_info = os.fstat(root_fd)
        if root_info.st_uid != os.getuid() or not stat.S_ISDIR(root_info.st_mode) or stat.S_IMODE(root_info.st_mode) != 0o700:
            raise UnsafePath("database directory is not exclusive", "blocked")
        info = lstat_at(root_fd, name)
        _require_private(info, "database is not a private regular file")
        total = info.st_size
        for suffix in _SIDECARS:
            side_name = name + suffix
            try:
                side = lstat_at(root_fd, side_name)
            except FileNotFoundError:
                continue
            _require_private(side, "sqlite sidecar is not a private regular file")
            total += side.st_size
        if total > _MAX_SNAPSHOT_BYTES:
            raise UnsafePath("database exceeds the snapshot bound", "bound")
        db_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=root_fd)
        current = os.fstat(db_fd)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or current.st_ino != info.st_ino
            or current.st_dev != info.st_dev
        ):
            raise UnsafePath("database descriptor changed", "blocked")
        directory_path = os.path.realpath(proc_fd_path(root_fd))
        source = os.path.join(directory_path, name)
        if os.path.dirname(os.path.realpath(source)) != directory_path or os.path.basename(os.path.realpath(source)) != name:
            raise UnsafePath("database path escapes the directory", "blocked")
        private = tempfile.TemporaryDirectory(prefix="sqlite-read-")
        os.chmod(private.name, 0o700)
        destination = os.path.join(private.name, name)
        _backup(source, destination, inode=current.st_ino, device=current.st_dev, root_fd=root_fd, name=name)
        yield destination
    finally:
        if private is not None:
            private.cleanup()
        if db_fd is not None:
            os.close(db_fd)
        os.close(root_fd)


def _require_private(info, reason: str) -> None:
    if stat.S_ISLNK(info.st_mode) or stat.S_ISFIFO(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise UnsafePath(reason, "blocked")


def _backup(source: str, destination: str, *, inode: int, device: int, root_fd: int, name: str) -> None:
    uri = Path(source).resolve().as_uri() + "?mode=ro"
    again = lstat_at(root_fd, name)
    if again.st_ino != inode or again.st_dev != device or again.st_nlink != 1 or not stat.S_ISREG(again.st_mode):
        raise UnsafePath("database changed during snapshot", "blocked")
    try:
        backup_consistent(uri, destination)
    except Exception as exc:
        raise UnsafePath("sqlite snapshot failed", "blocked") from exc
    final = lstat_at(root_fd, name)
    if final.st_ino != inode or final.st_dev != device or final.st_nlink != 1:
        raise UnsafePath("database changed during snapshot", "blocked")
    if os.path.getsize(destination) > _MAX_SNAPSHOT_BYTES:
        raise UnsafePath("snapshot exceeds the bound", "bound")
