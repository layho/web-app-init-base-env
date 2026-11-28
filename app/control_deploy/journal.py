"""Durable deploy log and cross-process fence.

Records are appended and fsynced before the caller is told they exist.
A torn or invalid tail fails closed until an explicit recovery quarantines
only that tail. The fence is an exclusive kernel lock plus a generation.
An in-process table only tracks file descriptors so threads in this process
share the kernel lock; it is not the safety boundary for another process.
Closing a process releases the kernel lock and does not delete the fence
file. A directory sitting at the fence path is not ownership.
"""

import copy
import errno
import fcntl
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path


class JournalCorrupt(Exception):
    def __init__(self, reason, *, line=None, offset=None, torn=False, recoverable=False, prefix_seq=None):
        super().__init__(reason)
        self.reason = reason
        self.line = line
        self.offset = offset
        self.torn = torn
        self.recoverable = recoverable
        self.prefix_seq = prefix_seq


_FENCE_GUARD = threading.Lock()
_JOURNAL_GUARDS = {}
# Path string -> {"engine_id", "fd"}. Threads share a process-wide flock,
# so this table stops a second thread in this process from treating that
# lock as free. Another process is excluded by the kernel lock itself.
_LIVE = {}


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
        "deliveries": {},
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
    elif kind == "delivery":
        record = copy.deepcopy(line["record"])
        store["deliveries"][record["event_id"]] = record
    else:
        raise JournalCorrupt("unknown deploy log record")
    store["seq"] = expected
    return store


def _corrupt(diagnosis):
    return JournalCorrupt(
        diagnosis["reason"],
        line=diagnosis.get("line"),
        offset=diagnosis.get("offset"),
        torn=bool(diagnosis.get("torn")),
        recoverable=bool(diagnosis.get("recoverable")),
        prefix_seq=diagnosis.get("prefix_seq"),
    )


class Journal:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.log_path = self.root / "deploy.log.jsonl"
        self.quarantine_path = self.root / "deploy.log.quarantine"
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

    def _split_durable(self):
        if not self.log_path.exists():
            return b"", b""
        data = self.log_path.read_bytes()
        if not data:
            return b"", b""
        if data.endswith(b"\n"):
            return data, b""
        prefix, sep, tail = data.rpartition(b"\n")
        if not sep:
            return b"", data
        return prefix + b"\n", tail

    def _replay_prefix(self, prefix):
        store = _empty()
        if not prefix:
            return store, None
        parts = prefix.split(b"\n")
        if prefix.endswith(b"\n"):
            parts = parts[:-1]
        offset = 0
        for index, raw_bytes in enumerate(parts, start=1):
            start = offset
            offset += len(raw_bytes) + 1
            raw = raw_bytes.decode("utf-8")
            if not raw.strip():
                continue
            try:
                _apply(store, json.loads(raw))
            except (json.JSONDecodeError, JournalCorrupt, KeyError, TypeError, ValueError) as exc:
                last = index == len(parts)
                return None, {
                    "reason": str(exc),
                    "line": index,
                    "offset": start,
                    "torn": False,
                    "recoverable": last,
                    "prefix_seq": store["seq"],
                    "kept_prefix": prefix[:start],
                    "removed": prefix[start:],
                }
        return store, None

    def _inspect_unlocked(self):
        prefix, torn = self._split_durable()
        store, problem = self._replay_prefix(prefix)
        if problem is not None:
            return None, problem
        if torn:
            return None, {
                "reason": "torn journal tail",
                "line": prefix.count(b"\n") + 1,
                "offset": len(prefix),
                "torn": True,
                "recoverable": True,
                "prefix_seq": store["seq"],
                "kept_prefix": prefix,
                "removed": torn,
            }
        return store, None

    def _replay_unlocked(self):
        store, problem = self._inspect_unlocked()
        if problem is not None:
            raise _corrupt(problem)
        return store

    def diagnose(self):
        with self.exclusive():
            _store, problem = self._inspect_unlocked()
            if problem is None:
                return {"ok": True, "recoverable": False, "torn": False}
            public = {key: value for key, value in problem.items() if key not in {"kept_prefix", "removed"}}
            public["ok"] = False
            public["removed_bytes"] = len(problem.get("removed") or b"")
            return public

    def recover_torn_tail(self):
        """Quarantine a corrupted tail and keep the last valid prefix.

        A damaged record that is not the tail is left in place and the
        call fails closed. Recovery never rewrites a deployment as success.
        """
        with self.exclusive():
            _store, problem = self._inspect_unlocked()
            if problem is None:
                return {"recovered": False, "reason": "journal_is_intact"}
            if not problem.get("recoverable"):
                public = {key: value for key, value in problem.items() if key not in {"kept_prefix", "removed"}}
                public["recovered"] = False
                return public
            removed = problem.get("removed") or b""
            record = {
                "reason": problem["reason"],
                "line": problem.get("line"),
                "offset": problem.get("offset"),
                "torn": bool(problem.get("torn")),
                "prefix_seq": problem.get("prefix_seq"),
                "removed_text": removed.decode("utf-8", errors="replace"),
            }
            with self.quarantine_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary = self.log_path.with_suffix(".jsonl.recovering")
            with temporary.open("wb") as handle:
                handle.write(problem.get("kept_prefix") or b"")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.log_path)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            check, leftover = self._inspect_unlocked()
            if leftover is not None or check is None:
                raise JournalCorrupt("journal tail recovery did not leave a readable prefix")
            return {
                "recovered": True,
                "quarantined": True,
                "reason": problem["reason"],
                "line": problem.get("line"),
                "offset": problem.get("offset"),
                "torn": bool(problem.get("torn")),
                "prefix_seq": problem.get("prefix_seq"),
                "quarantine_path": str(self.quarantine_path),
            }

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
    """Cross-process fence: exclusive kernel lock plus durable generation.

    The in-process table records which engine holds the open descriptor.
    Another process cannot acquire, reclaim, or take over the generation
    while that descriptor is locked. Process death drops the kernel lock;
    the file and generation stay until release or an authorized takeover.
    """

    def __init__(self, root):
        self.root = Path(root) / "fence"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key):
        if not key or "/" in key or key in {".", ".."} or ".." in key:
            raise ValueError("unsafe fence key")
        return self.root / f"{key}.json"

    def _load(self, path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return {"corrupt": True, "path": str(path)}

    def read(self, key):
        path = self._path(key)
        if path.is_dir():
            return {"legacy_directory": True, "path": str(path)}
        if not path.is_file():
            return None
        return self._load(path)

    def _drop(self, name):
        live = _LIVE.pop(name, None)
        if live is not None:
            try:
                os.close(live["fd"])
            except OSError:
                pass

    def _rewrite(self, fd, body):
        payload = json.dumps(body, sort_keys=True).encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        view = payload
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)

    def _lock(self, path):
        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in {errno.EAGAIN, errno.EACCES} or isinstance(exc, BlockingIOError):
                return None
            raise
        return fd

    def acquire(self, key, run_id, deployment_id, engine_id):
        path = self._path(key)
        name = str(path)
        with _FENCE_GUARD:
            if path.is_dir():
                return {"ok": False, "code": "EVIDENCE_MISSING", "record": "lock_unproven"}
            live = _LIVE.get(name)
            if live is not None and live["engine_id"] != engine_id:
                return {"ok": False, "code": "RESERVATION_CONFLICT", "record": "update_locked"}
            if path.exists():
                return {"ok": False, "code": "RESERVATION_CONFLICT", "record": "update_locked"}
            body = {
                "key": key,
                "generation": 1,
                "run_id": run_id,
                "deployment_id": deployment_id,
                "holder_pid": os.getpid(),
            }
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                return {"ok": False, "code": "RESERVATION_CONFLICT", "record": "update_locked"}
            try:
                self._rewrite(fd, body)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                try:
                    path.unlink()
                except OSError:
                    pass
                return {"ok": False, "code": "RESERVATION_CONFLICT", "record": "update_locked"}
            _LIVE[name] = {"engine_id": engine_id, "fd": fd}
            return {"ok": True, "lock": body}

    def reclaim(self, key, run_id, generation, engine_id):
        path = self._path(key)
        name = str(path)
        with _FENCE_GUARD:
            live = _LIVE.get(name)
            if live is not None and live["engine_id"] != engine_id:
                return False
            if not path.is_file():
                return False
            current = self._load(path)
            if current.get("corrupt") or current.get("run_id") != run_id or current.get("generation") != generation:
                return False
            if live is not None and live["engine_id"] == engine_id:
                return True
            fd = self._lock(path)
            if fd is None:
                return False
            _LIVE[name] = {"engine_id": engine_id, "fd": fd}
            return True

    def _load_fd(self, fd):
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            chunks = []
            while True:
                block = os.read(fd, 4096)
                if not block:
                    break
                chunks.append(block)
            if not chunks:
                return {"corrupt": True}
            return json.loads(b"".join(chunks).decode("utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return {"corrupt": True}

    def _generation_exact(self, body, key, generation):
        return (
            isinstance(body, dict)
            and not body.get("corrupt")
            and not body.get("legacy_directory")
            and body.get("key") == key
            and type(body.get("generation")) is int
            and body.get("generation") == generation
        )

    def _proof_path(self, key):
        return self.root / f"{key}.released.json"

    def _proof_matches(self, key, generation):
        path = self._proof_path(key)
        if not path.is_file():
            return False
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return False
        return (
            isinstance(body, dict)
            and body.get("key") == key
            and body.get("released") is True
            and type(body.get("generation")) is int
            and body.get("generation") == generation
        )

    def _write_release_proof(self, key, generation):
        path = self._proof_path(key)
        payload = json.dumps(
            {"generation": generation, "key": key, "released": True},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        temporary = self.root / f".{key}.released.tmp"
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            view = payload
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        except OSError:
            os.close(fd)
            return False
        os.close(fd)
        os.replace(temporary, path)
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return self._proof_matches(key, generation)

    def proven_released(self, key, generation):
        """True only when this generation is gone and the release proof matches it."""

        path = self._path(key)
        name = str(path)
        with _FENCE_GUARD:
            if _LIVE.get(name) is not None:
                return False
            if path.exists():
                return False
            return self._proof_matches(key, generation)

    def release(self, key, generation, engine_id):
        if not isinstance(key, str) or type(generation) is not int:
            return False
        path = self._path(key)
        name = str(path)
        with _FENCE_GUARD:
            live = _LIVE.get(name)
            if live is not None and live["engine_id"] != engine_id:
                return False
            if not path.is_file():
                if live is not None:
                    return False
                return self._proof_matches(key, generation)
            before_lock = self._load(path)
            acquired = False
            if live is not None:
                fd = live["fd"]
            else:
                fd = self._lock(path)
                if fd is None:
                    return False
                _LIVE[name] = {"engine_id": engine_id, "fd": fd}
                acquired = True
            try:
                if not path.is_file():
                    if acquired:
                        self._drop(name)
                    return self._proof_matches(key, generation)
                after_lock = self._load_fd(fd)
                if not self._generation_exact(after_lock, key, generation):
                    if acquired:
                        self._drop(name)
                    return False
                if not isinstance(before_lock, dict):
                    if acquired:
                        self._drop(name)
                    return False
                if not self._write_release_proof(key, generation):
                    if acquired:
                        self._drop(name)
                    return False
                confirmed = self._load_fd(fd)
                if not self._generation_exact(confirmed, key, generation):
                    if acquired:
                        self._drop(name)
                    return False
                try:
                    os.unlink(path)
                except OSError:
                    if acquired:
                        self._drop(name)
                    return False
                self._drop(name)
                return self._proof_matches(key, generation) and not path.exists()
            except OSError:
                if acquired and _LIVE.get(name) is not None:
                    self._drop(name)
                return False

    def takeover(self, key, run_id, deployment_id, expected_generation, engine_id):
        path = self._path(key)
        name = str(path)
        with _FENCE_GUARD:
            live = _LIVE.get(name)
            if live is not None and live["engine_id"] != engine_id:
                return {"ok": False, "code": "RESERVATION_CONFLICT", "record": "owner_alive"}
            if path.is_dir() or not path.is_file():
                return {"ok": False, "code": "EVIDENCE_MISSING", "record": "lock_unproven"}
            current = self._load(path)
            if current.get("corrupt") or current.get("legacy_directory"):
                return {"ok": False, "code": "EVIDENCE_MISSING", "record": "lock_unproven"}
            if current.get("generation") != expected_generation:
                return {"ok": False, "code": "LEASE_FENCE_REJECTED"}
            previous_dead = live is None
            fd = live["fd"] if live is not None else self._lock(path)
            if fd is None:
                return {"ok": False, "code": "RESERVATION_CONFLICT", "record": "owner_alive"}
            body = {
                "key": key,
                "generation": expected_generation + 1,
                "run_id": run_id,
                "deployment_id": deployment_id,
                "holder_pid": os.getpid(),
                "previous_pid": current.get("holder_pid"),
                "previous_run_id": current.get("run_id"),
            }
            self._rewrite(fd, body)
            _LIVE[name] = {"engine_id": engine_id, "fd": fd}
            return {
                "ok": True,
                "lock": body,
                "previous_dead": previous_dead,
                "previous_run_id": current.get("run_id"),
                "previous_pid": current.get("holder_pid"),
            }

    def crash(self, engine_id):
        with _FENCE_GUARD:
            for path, live in list(_LIVE.items()):
                if live["engine_id"] == engine_id:
                    self._drop(path)
