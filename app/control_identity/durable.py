"""File-backed state that survives a new process on the same directory."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import threading
from pathlib import Path


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
            "stored_outside_candidate": True,
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


class DurableStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "state.json"
        self._lock_path = self.root / ".lock"
        self._thread = threading.RLock()

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
        if not self.path.exists():
            return ""
        return self.path.read_text(encoding="utf-8")

    def _acquire(self):
        handle = open(self._lock_path, "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def _release(self, handle):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    def _read_unlocked(self) -> dict:
        if not self.path.exists():
            return fresh_state()
        loaded = json.loads(self.path.read_text(encoding="utf-8"))
        base = fresh_state()
        base.update(loaded)
        return base

    def _write_unlocked(self, document: dict) -> None:
        payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        temporary = self.path.with_suffix(".json.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, self.path)
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
