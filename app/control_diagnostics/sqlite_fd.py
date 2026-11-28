"""Copy a SQLite database from directory-fd descriptors.

The bytes are read from the descriptors that passed the regular-file check.
SQLite is opened on that private copy, not on the original path. A symlink or
hard link used as a WAL or journal sidecar is refused instead of followed.
"""

from __future__ import annotations

import fcntl
import os
import stat
import tempfile
from contextlib import contextmanager

from app.control_diagnostics.safe_io import UnsafePath, lstat_at, open_root

# SQLite's shared lock covers the 510 bytes after the pending and reserved bytes.
_SHARED_OFFSET = 0x40000000 + 2
_SHARED_LENGTH = 510
_SIDECARS = ("-wal", "-journal")


@contextmanager
def verified_sqlite_copy(directory, name: str):
    root_fd = open_root(directory)
    opened = []
    db_fd = None
    try:
        info = lstat_at(root_fd, name)
        if stat.S_ISLNK(info.st_mode) or stat.S_ISFIFO(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UnsafePath("database is not a private regular file")
        db_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=root_fd)
        opened.append(db_fd)
        current = os.fstat(db_fd)
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or current.st_ino != info.st_ino or current.st_dev != info.st_dev:
            raise UnsafePath("database descriptor changed")
        _lock_shared(db_fd)
        copies = [(name, db_fd)]
        for suffix in _SIDECARS:
            side_name = name + suffix
            try:
                side_info = lstat_at(root_fd, side_name)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(side_info.st_mode) or stat.S_ISFIFO(side_info.st_mode) or not stat.S_ISREG(side_info.st_mode) or side_info.st_nlink != 1:
                raise UnsafePath("sqlite sidecar is not a private regular file")
            side_fd = os.open(side_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=root_fd)
            opened.append(side_fd)
            side_now = os.fstat(side_fd)
            if (
                side_now.st_ino != side_info.st_ino
                or side_now.st_dev != side_info.st_dev
                or side_now.st_nlink != 1
                or not stat.S_ISREG(side_now.st_mode)
            ):
                raise UnsafePath("sqlite sidecar descriptor changed")
            copies.append((side_name, side_fd))
        # Re-check the database inode after the sidecars are open.
        again = os.fstat(db_fd)
        if again.st_ino != current.st_ino or again.st_dev != current.st_dev or again.st_nlink != 1:
            raise UnsafePath("database descriptor changed")
        private = tempfile.TemporaryDirectory(prefix="sqlite-read-")
        try:
            os.chmod(private.name, 0o700)
            for copied_name, copied_fd in copies:
                _copy_fd(copied_fd, os.path.join(private.name, copied_name))
            yield os.path.join(private.name, name)
        finally:
            private.cleanup()
    finally:
        if db_fd is not None:
            try:
                _unlock_shared(db_fd)
            except OSError:
                pass
        for fd in opened:
            os.close(fd)
        os.close(root_fd)


def _lock_shared(fd):
    os.lseek(fd, _SHARED_OFFSET, os.SEEK_SET)
    fcntl.lockf(fd, fcntl.LOCK_SH, _SHARED_LENGTH)


def _unlock_shared(fd):
    os.lseek(fd, _SHARED_OFFSET, os.SEEK_SET)
    fcntl.lockf(fd, fcntl.LOCK_UN, _SHARED_LENGTH)


def _copy_fd(fd, destination):
    os.lseek(fd, 0, os.SEEK_SET)
    out = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            offset = 0
            while offset < len(view):
                wrote = os.write(out, view[offset:])
                if wrote <= 0:
                    raise UnsafePath("short copy")
                offset += wrote
        os.fsync(out)
    finally:
        os.close(out)
