"""File-descriptor root for the isolated adapters.

The directory and its ancestors are opened with ``O_NOFOLLOW``. Writes are
bounded and atomic. A symlink, a hard link, and a FIFO are refused. A path
string that contains ``/tmp`` is not a containment check.
"""

from __future__ import annotations

import os
import stat

from app.control_diagnostics.safe_io import UnsafePath, lstat_at, open_root, replace_bytes

_MAX = 262144


class BoundRoot:
    def __init__(self, fd):
        self.fd = fd

    @classmethod
    def open(cls, path):
        return cls(open_root(path))

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def mkdir(self, name):
        _name(name)
        try:
            os.mkdir(name, mode=0o700, dir_fd=self.fd)
        except FileExistsError:
            info = lstat_at(self.fd, name)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise UnsafePath("directory is not real")
        return open_child(self.fd, name)

    def write_new(self, name, payload: bytes):
        _name(name)
        if not isinstance(payload, bytes) or len(payload) > _MAX:
            raise UnsafePath("bounded write exceeded", "bound")
        _refuse_existing(self.fd, name)
        replace_bytes(self.fd, name, payload)

    def append_line(self, name, line: bytes):
        _name(name)
        if not isinstance(line, bytes) or len(line) > _MAX or b"\n" in line[:-1]:
            raise UnsafePath("bounded write exceeded", "bound")
        _refuse_existing(self.fd, name, allow_absent=True)
        try:
            info = lstat_at(self.fd, name)
        except FileNotFoundError:
            info = None
        if info is not None and info.st_size + len(line) > _MAX:
            raise UnsafePath("bounded write exceeded", "bound")
        fd = os.open(
            name,
            os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            0o600,
            dir_fd=self.fd,
        )
        try:
            current = os.fstat(fd)
            if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or stat.S_ISFIFO(current.st_mode):
                raise UnsafePath("append target is not a private regular file")
            if current.st_size + len(line) > _MAX:
                raise UnsafePath("bounded write exceeded", "bound")
            view = memoryview(line)
            offset = 0
            while offset < len(view):
                wrote = os.write(fd, view[offset:])
                if wrote <= 0:
                    raise UnsafePath("short write")
                offset += wrote
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(self.fd)


def open_child(parent_fd, name):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        os.close(fd)
        raise UnsafePath("not a directory")
    return fd


def _name(name):
    if not isinstance(name, str) or not name or "/" in name or name in {".", ".."} or len(name) > 80:
        raise UnsafePath("unsafe path")


def _refuse_existing(dir_fd, name, allow_absent=False):
    try:
        info = lstat_at(dir_fd, name)
    except FileNotFoundError:
        if allow_absent:
            return
        return
    if stat.S_ISLNK(info.st_mode) or stat.S_ISFIFO(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise UnsafePath("refusing special file")
    if info.st_nlink != 1:
        raise UnsafePath("hard link")
