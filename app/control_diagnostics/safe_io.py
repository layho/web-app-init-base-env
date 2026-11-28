"""Open server-owned directories without following symlinks.

Callers keep a directory file descriptor and read or write through it.
A parent directory symlink is not followed. The bytes returned by a read
come from the same file descriptor that passed the regular-file check, with
a byte cap applied to that descriptor.

This does not detect a byte-for-byte copy that an operator placed inside the
authorized directory. It does not repair a log root the operator pointed at
a production tree. A hard link is refused because the same inode can also
live outside the directory.
"""

import os
import re
import secrets
import stat
import sys
from pathlib import Path


class UnsafePath(OSError):
    def __init__(self, reason, kind="unsafe"):
        super().__init__(reason)
        self.kind = kind


def proc_fd_path(fd, platform_name=None):
    """Path of an open descriptor. Darwin and Linux do not share one string."""
    name = sys.platform if platform_name is None else platform_name
    if name == "darwin":
        return f"/dev/fd/{fd}"
    if isinstance(name, str) and name.startswith("linux"):
        return f"/proc/self/fd/{fd}"
    raise UnsafePath("platform file-descriptor path is not available", "blocked")


def open_root(path):
    """Return a directory fd by walking each component with O_NOFOLLOW.

    The check and the open are the same walk. A symlink swapped into an
    ancestor after a separate lstat cannot redirect this descriptor. Callers
    keep the fd and do not open the path again.
    """
    raw = os.fspath(path)
    if not os.path.isabs(raw):
        raw = os.path.abspath(raw)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    held = fd
    try:
        for part in (item for item in raw.split("/") if item):
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=held)
            except OSError as exc:
                raise UnsafePath("ancestor is a symlink") from exc
            os.close(held)
            held = nxt
            info = os.fstat(held)
            if not stat.S_ISDIR(info.st_mode):
                raise UnsafePath("not a directory")
        fd = held
        held = None
        return fd
    finally:
        if held is not None:
            os.close(held)


def open_directory(dir_fd, name):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        os.close(fd)
        raise UnsafePath("not a directory")
    return fd


def list_names(dir_fd):
    return os.listdir(dir_fd)


def lstat_at(dir_fd, name):
    return os.lstat(name, dir_fd=dir_fd)


def read_bounded(dir_fd, name, max_bytes):
    """Read one regular file under dir_fd from the same file descriptor.

    O_NOFOLLOW is applied to the final component. Symlinks, hard links, and
    reads that exceed max_bytes raise UnsafePath and return no bytes.
    """
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise UnsafePath("invalid bound", "bound")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise UnsafePath("open refused") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafePath("not a regular file")
        if info.st_nlink != 1:
            raise UnsafePath("hard link")
        if info.st_size > max_bytes:
            raise UnsafePath("bounded read exceeded", "bound")
        directory = os.path.realpath(proc_fd_path(dir_fd))
        located = os.path.realpath(proc_fd_path(fd))
        prefix = directory.rstrip("/") + "/"
        if not located.startswith(prefix):
            raise UnsafePath("escaped directory")
        chunks = []
        total = 0
        while total <= max_bytes:
            chunk = os.read(fd, min(65536, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise UnsafePath("bounded read exceeded", "bound")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def append_bytes(dir_fd, name, payload):
    """Append to a regular file. Refuse a symlink or a hard link."""
    try:
        fd = os.open(
            name,
            os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=dir_fd,
        )
    except OSError as exc:
        raise UnsafePath("append refused") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UnsafePath("append target is not a private regular file")
        _write_all(fd, payload)
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    os.fsync(dir_fd)


def replace_bytes(dir_fd, name, payload):
    """Replace name with a new regular file.

    The temporary name is unique, so two writers do not unlink each other.
    The file is fsynced before rename, and the directory is fsynced after.
    The check of the replaced file uses O_NONBLOCK.
    """
    temporary = f".{name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    fd = None
    try:
        try:
            fd = os.open(
                temporary,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=dir_fd,
            )
        except OSError as exc:
            raise UnsafePath("replace refused") from exc
        _write_all(fd, payload)
        os.fchmod(fd, 0o600)
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.rename(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        temporary = None
        os.fsync(dir_fd)
    except Exception:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=dir_fd)
            except OSError:
                pass
        raise
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except OSError as exc:
        raise UnsafePath("replaced file is not regular") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise UnsafePath("replaced file is not a private regular file")
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    os.fsync(dir_fd)


_APP = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_PART = re.compile(r"[A-Za-z0-9._-]{1,80}\Z")


class SafeRoot:
    """Server-authorized directory. Callers supply a relative log name only."""

    def __init__(self, fd):
        self.fd = fd

    @classmethod
    def open(cls, path):
        candidate = Path(path)
        if candidate.is_symlink() or not candidate.is_dir():
            raise UnsafePath("log root is not a real directory")
        return cls(open_root(candidate))

    def read_app_log(self, app_id, relative, max_bytes):
        if not isinstance(app_id, str) or _APP.fullmatch(app_id) is None:
            raise UnsafePath("application is not authorized")
        parts = _relative_parts(relative)
        try:
            app_fd = open_directory(self.fd, app_id)
        except OSError as exc:
            raise UnsafePath("application log directory is not a real directory") from exc
        try:
            app_real = os.path.realpath(proc_fd_path(app_fd))
            data = _read_relative(app_fd, parts, max_bytes, app_real)
        finally:
            os.close(app_fd)
        return data.decode("utf-8", errors="replace")


def _relative_parts(relative):
    if isinstance(relative, os.PathLike):
        relative = os.fspath(relative)
    if not isinstance(relative, str) or not relative or relative.startswith(("/", "\\")):
        raise UnsafePath("absolute path")
    if "\x00" in relative or "\\" in relative or ":" in relative:
        raise UnsafePath("unsafe path")
    parts = relative.split("/")
    if not 1 <= len(parts) <= 4:
        raise UnsafePath("unsafe path")
    for part in parts:
        if part in {"", ".", ".."} or _PART.fullmatch(part) is None:
            raise UnsafePath("unsafe path")
    return parts


def _read_relative(dir_fd, parts, max_bytes, expected_root):
    owned = []
    current = dir_fd
    try:
        for part in parts[:-1]:
            nxt = open_directory(current, part)
            owned.append(nxt)
            current = nxt
        current_real = os.path.realpath(proc_fd_path(current))
        prefix = expected_root.rstrip("/") + "/"
        if current_real != expected_root.rstrip("/") and not current_real.startswith(prefix):
            raise UnsafePath("escaped directory")
        return read_bounded(current, parts[-1], max_bytes)
    finally:
        for fd in reversed(owned):
            os.close(fd)


def _write_all(fd, payload):
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        wrote = os.write(fd, view[offset:])
        if wrote <= 0:
            raise UnsafePath("short write")
        offset += wrote
    os.fsync(fd)
