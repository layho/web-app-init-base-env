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
        self._lock_path = self.root / ".journal.lock"
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
        try:
            info = folder.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("unsafe journal directory")
        path = folder / (key + ".json")
        try:
            self._check_record(path.lstat())
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
        root_fd = open_root(self.root)
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
            yield folder_fd
        finally:
            if folder_fd is not None:
                os.close(folder_fd)
            os.close(root_fd)

    @contextmanager
    def _locked(self):
        with _Lock(self._lock_path):
            self._recover_pending()
            yield

    @staticmethod
    def _sync_dir(directory):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _replace_durable(self, path, payload):
        with self._parent_fd(path, create=True) as fd:
            try:
                self._check_record(os.lstat(path.name, dir_fd=fd))
            except FileNotFoundError:
                pass
            replace_bytes(fd, path.name, payload.encode("utf-8"))

    def _recover_pending(self, *, inject=False):
        if not os.path.lexists(self._pending_path):
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
        self._pending_path.unlink()
        self._sync_dir(self.root)


    def read(self, kind, key):
        path = self._path(kind, key)
        with self._locked():
            return self._read_path(path)

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
                trace = self.root / "trace.jsonl"
                with trace.open("a", encoding="utf-8") as handle:
                    handle.writelines(view.trace_lines)
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

    def append_trace(self, entry):
        path = self.root / "trace.jsonl"
        line = json.dumps(entry, sort_keys=True) + "\n"
        with self._locked():
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def traces(self):
        path = self.root / "trace.jsonl"
        with self._locked():
            if not path.exists():
                return []
            rows = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if line:
                    rows.append(json.loads(line))
            return rows


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
        folder = self.journal._path(kind, "snapshot").parent
        keys = {path.stem for path in folder.glob("*.json")}
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
    def __init__(self, path):
        self.path = path
        self.handle = None

    def __enter__(self):
        if fcntl is None:
            raise RuntimeError("journal locking unavailable")
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
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
