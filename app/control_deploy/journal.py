"""Durable deploy log, fence, and idempotency store.

Records are appended and fsynced before the caller is told they exist.
Closing a process releases the in-memory holder and does not delete the
fence file. A directory sitting at the fence path is not ownership.
"""

import copy
import fcntl
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path


class JournalCorrupt(Exception):
    pass


_HOLDERS = {}
_FENCE_GUARD = threading.Lock()
_JOURNAL_GUARDS = {}


class _JournalGuard:
    def __init__(self):
        self.thread = threading.RLock()
        self.depth = threading.local()


def _empty():
    return {
        "seq": 0,
        "deployments": {},
        "locks": {},
        "idempotency": {},
        "holds": {},
        "host_inventory": [],
        "notes": [],
    }


def _apply(store, line):
    expected = store["seq"] + 1
    if line.get("seq") != expected:
        raise JournalCorrupt("deploy log sequence gap")
    kind = line.get("type")
    if kind == "deployment":
        deployment = copy.deepcopy(line["deployment"])
        store["deployments"][deployment["deployment_id"]] = deployment
    elif kind == "lock":
        store["locks"][line["lock"]["key"]] = copy.deepcopy(line["lock"])
    elif kind == "lock_release":
        store["locks"].pop(line["key"], None)
    elif kind == "idempotency":
        record = copy.deepcopy(line["record"])
        store["idempotency"][record["key"]] = record
    elif kind == "hold":
        store["holds"][line["candidate_id"]] = line["reason"]
    elif kind == "inventory":
        store["host_inventory"] = copy.deepcopy(line["items"])
    elif kind == "note":
        store["notes"].append(copy.deepcopy(line))
    else:
        raise JournalCorrupt("unknown deploy log record")
    store["seq"] = expected
    return store


class Journal:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.log_path = self.root / "deploy.log.jsonl"
        self._lock_path = self.root / "journal.lock"
        self._fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        with _FENCE_GUARD:
            guard = _JOURNAL_GUARDS.get(str(self._lock_path))
            if guard is None:
                guard = _JournalGuard()
                _JOURNAL_GUARDS[str(self._lock_path)] = guard
        self._guard = guard

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    @contextmanager
    def exclusive(self):
        self._guard.thread.acquire()
        depth = getattr(self._guard.depth, "n", 0)
        if depth == 0 and self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        self._guard.depth.n = depth + 1
        try:
            yield
        finally:
            self._guard.depth.n -= 1
            if self._guard.depth.n == 0 and self._fd is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._guard.thread.release()

    def _replay_unlocked(self):
        store = _empty()
        if not self.log_path.exists():
            return store
        text = self.log_path.read_text(encoding="utf-8")
        if not text:
            return store
        for raw in text.splitlines():
            if not raw.strip():
                continue
            _apply(store, json.loads(raw))
        return store

    def replay(self):
        with self.exclusive():
            return self._replay_unlocked()

    def commit(self, record):
        with self.exclusive():
            store = self._replay_unlocked()
            line = dict(record)
            line["seq"] = store["seq"] + 1
            _apply(store, line)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return store


class Fence:
    """Process fence backed by an exclusive file plus a generation.

    Existence of a directory is not a held lock. Exit does not remove the
    file; only a terminal release or an authorized takeover changes it.
    """

    def __init__(self, root):
        self.root = Path(root) / "fence"
        self.root.mkdir(parents=True, exist_ok=True)
        self._fds = {}

    def _path(self, key):
        if not key or "/" in key or key in {".", ".."} or ".." in key:
            raise ValueError("unsafe fence key")
        return self.root / f"{key}.json"

    def read(self, key):
        path = self._path(key)
        if path.is_dir():
            return {"legacy_directory": True, "path": str(path)}
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, path, body):
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(body, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def acquire(self, key, run_id, deployment_id, engine_id):
        path = self._path(key)
        with _FENCE_GUARD:
            if path.is_dir():
                return {"ok": False, "code": "EVIDENCE_MISSING", "record": "lock_unproven"}
            holder = _HOLDERS.get(str(path))
            if holder not in (None, engine_id):
                return {"ok": False, "code": "RESERVATION_CONFLICT", "record": "update_locked"}
            if path.exists():
                return {"ok": False, "code": "RESERVATION_CONFLICT", "record": "update_locked"}
            body = {
                "key": key,
                "generation": 1,
                "run_id": run_id,
                "deployment_id": deployment_id,
            }
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                return {"ok": False, "code": "RESERVATION_CONFLICT", "record": "update_locked"}
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(body, sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
            _HOLDERS[str(path)] = engine_id
            return {"ok": True, "lock": body}

    def reclaim(self, key, run_id, generation, engine_id):
        path = self._path(key)
        with _FENCE_GUARD:
            holder = _HOLDERS.get(str(path))
            if holder not in (None, engine_id):
                return False
            if not path.is_file():
                return False
            current = json.loads(path.read_text(encoding="utf-8"))
            if current.get("run_id") != run_id or current.get("generation") != generation:
                return False
            _HOLDERS[str(path)] = engine_id
            return True

    def release(self, key, generation, engine_id):
        path = self._path(key)
        with _FENCE_GUARD:
            if _HOLDERS.get(str(path)) not in (None, engine_id):
                return False
            if not path.is_file():
                _HOLDERS[str(path)] = None
                return False
            current = json.loads(path.read_text(encoding="utf-8"))
            if current.get("generation") != generation:
                return False
            path.unlink()
            _HOLDERS[str(path)] = None
            return True

    def takeover(self, key, run_id, deployment_id, expected_generation, engine_id):
        path = self._path(key)
        with _FENCE_GUARD:
            holder = _HOLDERS.get(str(path))
            if holder not in (None, engine_id):
                return {"ok": False, "code": "RESERVATION_CONFLICT", "record": "owner_alive"}
            if not path.is_file():
                return {"ok": False, "code": "EVIDENCE_MISSING", "record": "lock_unproven"}
            current = json.loads(path.read_text(encoding="utf-8"))
            if current.get("generation") != expected_generation:
                return {"ok": False, "code": "LEASE_FENCE_REJECTED"}
            body = {
                "key": key,
                "generation": expected_generation + 1,
                "run_id": run_id,
                "deployment_id": deployment_id,
            }
            self._write(path, body)
            _HOLDERS[str(path)] = engine_id
            return {"ok": True, "lock": body}

    def crash(self, engine_id):
        with _FENCE_GUARD:
            for path, holder in list(_HOLDERS.items()):
                if holder == engine_id:
                    _HOLDERS[path] = None
