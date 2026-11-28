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
import tempfile
import stat
from contextlib import contextmanager
from pathlib import Path

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
        path = (self.root / kind / (key + ".json")).resolve()
        if path.parent != (self.root / kind).resolve():
            raise ValueError("path escape")
        return path

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
        path.parent.mkdir(parents=True, exist_ok=True)
        self._sync_dir(path.parent.parent if path.parent != self.root else self.root)
        fd, temporary = tempfile.mkstemp(prefix=".journal-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._sync_dir(path.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _recover_pending(self, *, inject=False):
        if not self._pending_path.exists():
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
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def write(self, kind, key, doc):
        path = self._path(kind, key)
        payload = json.dumps(doc, sort_keys=True, indent=2) + "\n"
        with self._locked():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, path)

    def list(self, kind):
        folder = self.root / kind
        with self._locked():
            if not folder.is_dir():
                return []
            docs = []
            for path in sorted(folder.glob("*.json")):
                docs.append(json.loads(path.read_text(encoding="utf-8")))
            return docs

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
