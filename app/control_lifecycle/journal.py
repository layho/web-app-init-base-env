"""Stage journal with cross-process compare-and-swap.

The startup cache is not the source of truth. Each transaction holds a
process lock and a file lock, re-reads the record, validates it, and only
then commits. The commit fsyncs the file and the directory. Symlinks,
hardlinks, and corrupt records fail closed. A failed or unknown receipt is
never replayed as ok or HTTP 200.
"""

import copy
import fcntl
import json
import os
import secrets
import stat
import threading

from app.control_lifecycle.errors import LifecycleError


class _PersistError(Exception):
    def __init__(self, code, diagnosis):
        super().__init__(diagnosis)
        self.code = code
        self.diagnosis = diagnosis


class Journal:
    def __init__(self, path, faults):
        self.path = path
        self.lock_path = path.with_name(path.name + ".lock")
        self.faults = faults
        self._thread = threading.Lock()
        self.data = self._empty()

    def inspect(self):
        with self._thread:
            lock_fd = self._acquire_file_lock()
            try:
                loaded = self._load_locked()
                if loaded["error"]:
                    raise LifecycleError(loaded["error"], 409, loaded["diagnosis"])
                self.data = loaded["data"]
                return self._public(self.data)
            finally:
                self._release_file_lock(lock_fd)

    def transact(self, *, expected_revision, principal, scope, operation, idempotency_key, payload_hash, run_id, handler, result_for):
        del principal, scope
        with self._thread:
            try:
                lock_fd = self._acquire_file_lock()
            except _PersistError as exc:
                return result_for(exc.code, 409, False, exc.diagnosis, {}, False, self.data, original=None)
            try:
                return self._transact_locked(
                    expected_revision=expected_revision,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                    run_id=run_id,
                    handler=handler,
                    result_for=result_for,
                )
            finally:
                self._release_file_lock(lock_fd)

    def _transact_locked(self, *, expected_revision, operation, idempotency_key, payload_hash, run_id, handler, result_for):
        loaded = self._load_locked()
        if loaded["error"]:
            return result_for(loaded["error"], 409, False, loaded["diagnosis"], {}, False, self.data, original=None)
        data = loaded["data"]
        self.data = data
        record = data["idempotency"].get(idempotency_key)
        if record is not None:
            if record.get("hash") != payload_hash:
                return result_for(
                    "IDEMPOTENCY_PAYLOAD_MISMATCH",
                    409,
                    False,
                    "idempotency key was used with a different payload",
                    {},
                    False,
                    data,
                    original=None,
                )
            return self._replay(record, data, result_for)
        if data["revision"] != expected_revision:
            return result_for(
                "CONFLICT_REVISION",
                409,
                False,
                "expected revision does not match the journal",
                {},
                False,
                data,
                original=None,
            )
        if "journal_fsync_fails" in self.faults:
            return result_for("JOURNAL_FAILED", 409, False, "journal write failed closed", {}, False, data, original=None)
        new_data = copy.deepcopy(data)
        outcome = handler(new_data)
        effect = outcome.pop("effect", None)
        intent = outcome.pop("intent", None)
        if effect is None:
            self._bookkeep(new_data, outcome, idempotency_key, payload_hash, operation, run_id, state="confirmed")
            try:
                self._commit(new_data)
            except _PersistError as exc:
                return result_for(exc.code, 409, False, exc.diagnosis, {}, False, data, original=None)
            self.data = new_data
            return result_for(
                outcome["code"],
                outcome["http"],
                outcome["ok"],
                outcome["diagnosis"],
                outcome.get("plan") or {},
                outcome.get("simulated", False),
                new_data,
                original=None,
            )
        staged = copy.deepcopy(new_data)
        if intent is not None:
            staged.setdefault("intents", {})
            stored = dict(intent)
            stored["state"] = "intent"
            staged["intents"][stored["intent_id"]] = stored
        pending = {
            "code": "SIDE_EFFECT_UNKNOWN",
            "ok": False,
            "http": 409,
            "diagnosis": "side effect is not confirmed",
            "plan": outcome.get("plan") or {},
            "simulated": False,
        }
        staged["idempotency"][idempotency_key] = {"hash": payload_hash, "public": pending, "state": "unknown"}
        staged["revision"] = data["revision"] + 1
        try:
            self._commit(staged)
        except _PersistError as exc:
            return result_for(exc.code, 409, False, exc.diagnosis, {}, False, data, original=None)
        self.data = staged
        if "crash_after_intent" in self.faults:
            return result_for(
                "SIDE_EFFECT_UNKNOWN",
                409,
                False,
                "side effect is not confirmed",
                pending["plan"],
                False,
                staged,
                original=None,
            )
        try:
            produced = effect()
        except Exception:
            return result_for(
                "SIDE_EFFECT_UNKNOWN",
                409,
                False,
                "side effect is not confirmed",
                pending["plan"],
                False,
                staged,
                original=None,
            )
        mutate = produced.pop("mutate", None)
        produced.pop("effect", None)
        produced.pop("intent", None)
        final = copy.deepcopy(staged)
        if mutate is not None:
            mutate(final)
        self._bookkeep(final, produced, idempotency_key, payload_hash, operation, run_id, state="confirmed")
        try:
            self._commit(final)
        except _PersistError as exc:
            return result_for(exc.code, 409, False, exc.diagnosis, {}, False, staged, original=None)
        self.data = final
        return result_for(
            produced["code"],
            produced["http"],
            produced["ok"],
            produced["diagnosis"],
            produced.get("plan") or {},
            produced.get("simulated", False),
            final,
            original=None,
        )

    def _replay(self, record, data, result_for):
        public = record.get("public") or {}
        unknown = record.get("state") == "unknown" or public.get("ok") is not True
        if unknown:
            http = public.get("http", 409)
            if http == 200 or record.get("state") == "unknown":
                http = 409
            code = "SIDE_EFFECT_UNKNOWN" if record.get("state") == "unknown" else "IDEMPOTENCY_REPLAY"
            diagnosis = "unknown result is not replayed" if record.get("state") == "unknown" else "idempotent replay of a failed receipt"
            return result_for(code, http, False, diagnosis, public.get("plan") or {}, False, data, original=public)
        return result_for(
            "IDEMPOTENCY_REPLAY",
            200,
            True,
            "idempotent replay",
            public.get("plan") or {},
            public.get("simulated", False),
            data,
            original=public,
        )

    def _bookkeep(self, data, outcome, idem_id, payload_hash, operation, run_id, state):
        if outcome.get("count_seq", True):
            data["seq"] += 1
            epoch = str(data["epoch"])
            data["epochs"].setdefault(epoch, {"gap": False, "closed": False, "applied_seqs": []})
            data["epochs"][epoch]["applied_seqs"].append(data["seq"])
        if outcome.get("record_run", True) and run_id:
            runs = data["runs"].setdefault(run_id, [])
            if operation not in runs:
                runs.append(operation)
        data["revision"] += 1
        public = {
            "code": outcome["code"],
            "ok": outcome["ok"],
            "http": outcome["http"],
            "diagnosis": outcome["diagnosis"],
            "plan": outcome.get("plan") or {},
            "simulated": outcome.get("simulated", False),
        }
        data["idempotency"][idem_id] = {"hash": payload_hash, "public": public, "state": state}

    def _acquire_file_lock(self):
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        fd = os.open(self.lock_path, flags, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(fd)
            raise _PersistError("JOURNAL_UNSAFE", "journal lock is a symlink or hardlink")
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _release_file_lock(self, fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _load_locked(self):
        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            try:
                info = os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return {"error": None, "diagnosis": "", "data": self._empty()}
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return {"error": "JOURNAL_UNSAFE", "diagnosis": "journal path is a symlink or hardlink", "data": self.data}
            fd = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            try:
                current = os.fstat(fd)
                if (
                    current.st_ino != info.st_ino
                    or current.st_dev != info.st_dev
                    or current.st_nlink != 1
                    or not stat.S_ISREG(current.st_mode)
                ):
                    return {
                        "error": "JOURNAL_UNSAFE",
                        "diagnosis": "journal descriptor does not match the directory entry",
                        "data": self.data,
                    }
                raw = _read_all(fd)
            finally:
                os.close(fd)
        finally:
            os.close(directory)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return {"error": "CORRUPT_JOURNAL", "diagnosis": "journal record is corrupt", "data": self.data}
        if not _valid(parsed):
            return {"error": "CORRUPT_JOURNAL", "diagnosis": "journal record is corrupt", "data": self.data}
        parsed.setdefault("intents", {})
        parsed.setdefault("retired_port_generations", {})
        return {"error": None, "diagnosis": "", "data": parsed}

    def _commit(self, data):
        if "journal_fsync_fails" in self.faults:
            raise _PersistError("JOURNAL_FAILED", "journal write failed closed")
        payload = json.dumps(data, sort_keys=True, indent=2).encode("utf-8")
        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        temporary = f".{self.path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
        fd = None
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory,
            )
            view = memoryview(payload)
            offset = 0
            while offset < len(view):
                wrote = os.write(fd, view[offset:])
                if wrote <= 0:
                    raise _PersistError("JOURNAL_FAILED", "short journal write")
                offset += wrote
            os.fsync(fd)
            os.close(fd)
            fd = None
            try:
                info = os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info is not None and (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                raise _PersistError("JOURNAL_UNSAFE", "journal path is a symlink or hardlink")
            os.fsync(directory)
            os.replace(temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
            temporary = None
        except _PersistError:
            raise
        except OSError as exc:
            raise _PersistError("JOURNAL_FAILED", "journal write failed closed") from exc
        finally:
            if fd is not None:
                os.close(fd)
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except OSError:
                    pass
            os.close(directory)

    def _empty(self):
        return {
            "revision": 0,
            "epoch": 1,
            "seq": 0,
            "epochs": {"1": {"gap": False, "closed": False, "applied_seqs": []}},
            "reservations": {},
            "resources": {},
            "runs": {},
            "idempotency": {},
            "intents": {},
            "deployment_state": "ABSENT",
            "service_generation": 0,
            "receipt": None,
            "retired_port_generations": {},
        }

    def _public(self, data):
        return {
            "revision": data["revision"],
            "epoch": data["epoch"],
            "seq": data["seq"],
            "epochs": copy.deepcopy(data["epochs"]),
            "reservations": copy.deepcopy(data["reservations"]),
            "resources": copy.deepcopy(data["resources"]),
            "deployment_state": data["deployment_state"],
            "idempotency_count": len(data["idempotency"]),
            "service_generation": data["service_generation"],
        }


def _read_all(fd):
    chunks = []
    while True:
        block = os.read(fd, 65536)
        if not block:
            break
        chunks.append(block)
    return b"".join(chunks)


def _valid(data):
    if not isinstance(data, dict):
        return False
    required = (
        "revision",
        "epoch",
        "seq",
        "epochs",
        "reservations",
        "resources",
        "runs",
        "idempotency",
        "deployment_state",
        "service_generation",
        "receipt",
    )
    if any(key not in data for key in required):
        return False
    if type(data["revision"]) is not int or data["revision"] < 0:
        return False
    if type(data["epoch"]) is not int or data["epoch"] < 1:
        return False
    if type(data["seq"]) is not int or data["seq"] < 0:
        return False
    if type(data["service_generation"]) is not int or data["service_generation"] < 0:
        return False
    if not isinstance(data["deployment_state"], str):
        return False
    for key in ("epochs", "reservations", "resources", "runs", "idempotency"):
        if not isinstance(data[key], dict):
            return False
    if "intents" in data and not isinstance(data["intents"], dict):
        return False
    if data["receipt"] is not None and not isinstance(data["receipt"], dict):
        return False
    return True
