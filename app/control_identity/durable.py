"""File-backed state that survives a new process on the same directory.

The directory must be private and must not be a symlink. A short write, a
symlink, or a damaged document fails closed and does not replace the last
good copy with a default document.

Retention drops only expired nonce rows, and only when a new nonce is
inserted. Unreconciled effects, idempotency records, and runs are kept.
When a cap is reached and retention cannot free an expired nonce, the write
is rejected. Capacity limits are server configuration, not request fields.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import stat
import threading
from pathlib import Path

from .errors import ControlError


def fresh_state() -> dict:
    return {
        "principals": {},
        "nonces": {},
        "revoked_signers": [],
        "proxy": {"feedback": 1, "pull": 1, "merge": 1, "publish": 1, "read": 1},
        "runs": {},
        "idempotency": {},
        "policy": {
            "revision": 1,
            "ordinary_allow": ["preview.merge", "deploy.apply", "draft.undraft"],
            "high_risk_allow": [],
            "high_risk_grants": [],
            "stored_outside_candidate": True,
            "evidence": None,
        },
        "limits": {},
        "sessions": {},
        "revoked_sessions": [],
        "candidates": {},
        "effects": [],
        "acceptances": {},
        "statuses": {},
        "merges": [],
        "published": [],
        "writes": [],
    }


def write_all(write, payload: bytes) -> None:
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        raise ControlError("STORE_CORRUPT", 500, "empty state write", reason="short_write")
    view = memoryview(payload)
    while len(view):
        written = write(view)
        if not isinstance(written, int) or written <= 0 or written > len(view):
            raise ControlError("STORE_CORRUPT", 500, "short write", reason="short_write")
        view = view[written:]


class DurableStore:
    def __init__(self, root: str | Path, *, max_nonces: int = 8192, max_idempotency: int = 8192,
                 max_effects: int = 1024, max_bytes: int = 8_000_000):
        self.root = Path(root)
        self.max_nonces = max_nonces
        self.max_idempotency = max_idempotency
        self.max_effects = max_effects
        self.max_bytes = max_bytes
        if self.root.is_symlink():
            raise ControlError("STORE_CORRUPT", 500, "state directory rejected", reason="store_symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.path = self.root / "state.json"
        self._lock_path = self.root / ".lock"
        self._thread = threading.RLock()
        self._assert_private()

    def read(self) -> dict:
        with self._thread:
            handle = self._acquire()
            try:
                return copy.deepcopy(self._read_unlocked())
            finally:
                self._release(handle)

    def update(self, mutator):
        with self._thread:
            handle = self._acquire()
            try:
                current = self._read_unlocked()
                updated, result = mutator(current)
                self._write_unlocked(updated)
                return result
            finally:
                self._release(handle)

    def text(self) -> str:
        self._assert_private()
        if self.path.is_symlink():
            raise ControlError("STORE_CORRUPT", 500, "state path is a symlink", reason="store_symlink")
        if not self.path.exists():
            return ""
        return self.path.read_text(encoding="utf-8")

    def _acquire(self):
        self._assert_private()
        handle = open(self._lock_path, "a+")
        os.chmod(self._lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def _release(self, handle):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    def _assert_private(self) -> None:
        if self.root.is_symlink() or not self.root.is_dir():
            raise ControlError("STORE_CORRUPT", 500, "state directory rejected", reason="store_symlink")
        if stat.S_IMODE(self.root.stat().st_mode) & 0o077:
            raise ControlError("STORE_CORRUPT", 500, "state directory is not private", reason="store_permissions")
        for path in (self.path, self._lock_path):
            if path.is_symlink():
                raise ControlError("STORE_CORRUPT", 500, "state path is a symlink", reason="store_symlink")
            if path.exists() and stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise ControlError("STORE_CORRUPT", 500, "state file is not private", reason="store_permissions")

    def _read_unlocked(self) -> dict:
        self._assert_private()
        if not self.path.exists():
            return fresh_state()
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ControlError("STORE_CORRUPT", 500, "state document is damaged", reason="store_corrupt") from exc
        return self._validated(loaded)

    def _validated(self, loaded: dict) -> dict:
        if not isinstance(loaded, dict):
            raise ControlError("STORE_CORRUPT", 500, "state document is damaged", reason="store_corrupt")
        expected = fresh_state()
        for key, sample in expected.items():
            if key not in loaded:
                raise ControlError("STORE_CORRUPT", 500, "state document is damaged", reason="store_corrupt")
            if sample is not None and not isinstance(loaded[key], type(sample)):
                raise ControlError("STORE_CORRUPT", 500, "state document is damaged", reason="store_corrupt")
        policy = loaded["policy"]
        if "revision" not in policy or "evidence" not in policy or isinstance(policy["revision"], bool) or not isinstance(policy["revision"], int):
            raise ControlError("STORE_CORRUPT", 500, "state document is damaged", reason="store_corrupt")
        return loaded

    def _write_unlocked(self, document: dict) -> None:
        self._validated(document)
        payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(payload) > self.max_bytes:
            raise ControlError("CAPACITY", 429, "state capacity exceeded", reason="capacity")
        temporary = self.path.with_suffix(".json.tmp")
        if temporary.is_symlink() or self.path.is_symlink():
            raise ControlError("STORE_CORRUPT", 500, "state path is a symlink", reason="store_symlink")
        descriptor = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            write_all(lambda buf: os.write(descriptor, buf), payload)
            os.fsync(descriptor)
        except ControlError:
            self._discard_temp(temporary)
            raise
        except OSError as exc:
            self._discard_temp(temporary)
            raise ControlError("STORE_CORRUPT", 500, "state path is a symlink", reason="store_symlink") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _discard_temp(self, temporary: Path) -> None:
        if temporary.exists() and not temporary.is_symlink():
            os.unlink(temporary)
