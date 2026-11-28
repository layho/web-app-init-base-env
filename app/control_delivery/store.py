"""Delivery journal on a caller-supplied directory.

This is the W05 protocol journal. It is not an authentication store and it
is not the W01 event authority. Outbox files are producer delivery state
only; an event is accepted when W01 returns an exact receipt. Callers pass
a temporary directory in tests.
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

from app.control_diagnostics.safe_io import open_root, open_directory, replace_bytes

try:
    import fcntl
except ImportError:  # pragma: no cover - this package is exercised on Linux
    fcntl = None

_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,200}")


class Journal:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        root_fd = open_root(self.root)
        try:
            info = os.fstat(root_fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ValueError("unsafe journal root")
            self._root_identity = (info.st_dev, info.st_ino)
        finally:
            os.close(root_fd)
        self._local = threading.local()
        self.crash_before_commit = False
        self.crash_after_replacements = None
        self._pending_path = self.root / ".journal.pending.json"

    def _path(self, kind, key):
        if not isinstance(kind, str) or not _KEY.fullmatch(kind):
            raise ValueError("unsafe kind")
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise ValueError("unsafe key")
        if kind in {".", ".."} or key in {".", ".."}:
            raise ValueError("unsafe journal name")
        folder = self.root / kind
        path = folder / (key + ".json")
        try:
            with self._parent_fd(path) as fd:
                self._check_record(os.lstat(path.name, dir_fd=fd))
        except FileNotFoundError:
            pass
        return path

    @staticmethod
    def _check_record(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or info.st_mode & 0o022):
            raise ValueError("unsafe journal record")

    @contextmanager
    def _parent_fd(self, path, *, create=False):
        root_fd = self._local.root_fd
        folder_fd = None
        try:
            if path.parent == self.root:
                yield root_fd
                return
            if path.parent.parent != self.root:
                raise ValueError("unsafe journal parent")
            kind = path.parent.name
            if create:
                try:
                    os.mkdir(kind, mode=0o700, dir_fd=root_fd)
                    os.fsync(root_fd)
                except FileExistsError:
                    pass
            folder_fd = open_directory(root_fd, kind)
            info = os.fstat(folder_fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ValueError("unsafe journal directory")
            yield folder_fd
        finally:
            if folder_fd is not None:
                os.close(folder_fd)

    @contextmanager
    def _locked(self):
        if hasattr(self._local, "root_fd"):
            raise RuntimeError("nested journal transaction")
        root_fd = open_root(self.root)
        try:
            info = os.fstat(root_fd)
            if ((info.st_dev, info.st_ino) != self._root_identity
                    or info.st_uid != os.getuid() or info.st_mode & 0o022):
                raise ValueError("journal root changed or unsafe")
            with _Lock(root_fd):
                self._local.root_fd = root_fd
                try:
                    self._recover_pending()
                    yield
                finally:
                    del self._local.root_fd
        finally:
            os.close(root_fd)

    def _replace_durable(self, path, payload):
        with self._parent_fd(path, create=True) as fd:
            try:
                self._check_record(os.lstat(path.name, dir_fd=fd))
            except FileNotFoundError:
                pass
            replace_bytes(fd, path.name, payload.encode("utf-8"))

    def _recover_pending(self, *, inject=False):
        root_fd = self._local.root_fd
        try:
            os.lstat(self._pending_path.name, dir_fd=root_fd)
        except FileNotFoundError:
            return
        pending = self._read_path(self._pending_path)
        if not isinstance(pending, dict) or pending.get("version") != 1 or not isinstance(pending.get("writes"), list):
            raise RuntimeError("invalid delivery transaction; manual recovery required")
        writes = []
        for item in pending["writes"]:
            if not isinstance(item, dict) or set(item) != {"kind", "key", "payload"} or not isinstance(item["payload"], str):
                raise RuntimeError("invalid delivery transaction entry")
            json.loads(item["payload"])
            writes.append((self._path(item["kind"], item["key"]), item["payload"]))
        for index, (path, payload) in enumerate(writes, 1):
            self._replace_durable(path, payload)
            if inject and self.crash_after_replacements == index:
                raise RuntimeError("crash during local commit")
        os.unlink(self._pending_path.name, dir_fd=root_fd)
        os.fsync(root_fd)


    def read(self, kind, key):
        with self._locked():
            return self._read_path(self._path(kind, key))

    def mutate(self, mutator):
        """Run ``mutator(view)`` under the journal lock and commit staged writes.

        The mutator must use the view, not ``read`` or ``write``. A false CAS
        aborts every staged write. ``crash_before_commit`` raises after the
        mutator and before any replacement, so a crash leaves no partial file.
        """
        with self._locked():
            view = _View(self)
            result = mutator(view)
            if view.aborted or (not view.writes and not view.trace_lines):
                return result
            if self.crash_before_commit:
                raise RuntimeError("crash before local commit")
            if view.writes:
                # Persist the complete redo plan before the first replacement.
                # Cooperating readers/writers recover under the same lock.
                pending = {"version": 1, "writes": [
                    {"kind": kind, "key": key, "payload": view.writes[self._path(kind, key)]}
                    for kind, key in view.staged
                ]}
                self._replace_durable(self._pending_path, json.dumps(pending, sort_keys=True))
                self._recover_pending(inject=True)
            if view.trace_lines:
                self._append_trace_locked(view.trace_lines)
            return result

    def _read_path(self, path):
        try:
            with self._parent_fd(path) as parent:
                fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=parent)
                try:
                    self._check_record(os.fstat(fd))
                    with os.fdopen(fd, "r", encoding="utf-8") as handle:
                        fd = None
                        return json.load(handle)
                finally:
                    if fd is not None:
                        os.close(fd)
        except FileNotFoundError:
            return None

    def write(self, kind, key, doc):
        payload = json.dumps(doc, sort_keys=True, indent=2) + "\n"
        with self._locked():
            self._replace_durable(self._path(kind, key), payload)

    def list(self, kind):
        with self._locked():
            return _View(self).list(kind)

    def _append_trace_locked(self, lines):
        root_fd = self._local.root_fd
        fd = os.open("trace.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND |
                     os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=root_fd)
        try:
            self._check_record(os.fstat(fd))
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                fd = None
                handle.writelines(lines)
                handle.flush()
                os.fsync(handle.fileno())
            os.fsync(root_fd)
        finally:
            if fd is not None:
                os.close(fd)

    def append_trace(self, entry):
        line = json.dumps(entry, sort_keys=True) + "\n"
        with self._locked():
            self._append_trace_locked([line])

    def traces(self):
        with self._locked():
            try:
                fd = os.open("trace.jsonl", os.O_RDONLY | os.O_NOFOLLOW |
                             os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=self._local.root_fd)
            except FileNotFoundError:
                return []
            try:
                self._check_record(os.fstat(fd))
                with os.fdopen(fd, "r", encoding="utf-8") as handle:
                    fd = None
                    return [json.loads(line) for line in handle if line.strip()]
            finally:
                if fd is not None:
                    os.close(fd)


class _View:
    def __init__(self, journal):
        self.journal = journal
        self.writes = {}
        self.staged = {}
        self.trace_lines = []
        self.aborted = False

    def read(self, kind, key):
        token = (kind, key)
        if token in self.staged:
            return self.staged[token]
        return self.journal._read_path(self.journal._path(kind, key))

    def list(self, kind):
        """Read a complete kind snapshot under the enclosing journal lock."""
        probe = self.journal._path(kind, "snapshot")
        try:
            with self.journal._parent_fd(probe) as fd:
                keys = {name[:-5] for name in os.listdir(fd) if name.endswith(".json")}
        except FileNotFoundError:
            keys = set()
        keys.update(key for staged_kind, key in self.staged if staged_kind == kind)
        return [self.read(kind, key) for key in sorted(keys)]

    def stage(self, kind, key, doc):
        path = self.journal._path(kind, key)
        payload = json.dumps(doc, sort_keys=True, indent=2) + "\n"
        self.writes[path] = payload
        self.staged[(kind, key)] = doc

    def abort(self):
        self.aborted = True

    def trace(self, entry):
        self.trace_lines.append(json.dumps(entry, sort_keys=True) + "\n")


class _Lock:
    def __init__(self, root_fd):
        self.root_fd = root_fd
        self.handle = None

    def __enter__(self):
        if fcntl is None:
            raise RuntimeError("journal locking unavailable")
        fd = os.open(".journal.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW |
                     os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=self.root_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise RuntimeError("unsafe journal lock")
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.handle = os.fdopen(fd, "a+")
        except BaseException:
            os.close(fd)
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handle is not None:
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
        return False
