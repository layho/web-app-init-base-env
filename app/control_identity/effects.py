"""Durable cross-process gate for effect checks.

This proxy serializes local reconciliation with a file lock so two processes
cannot run the critical section together. It is a local durability simulation.
It does not provide GitHub fencing: GitHub has no native lease that this lock
can hold, and a passed check here is not evidence that GitHub accepted or
rejected a write.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from pathlib import Path

from .errors import ControlError


class DurableEffectProxy:
    SIMULATES_GITHUB_FENCING = False
    FENCING_NOTICE = (
        "This proxy is a local durability simulation for isolated reconciliation. "
        "It does not provide GitHub fencing."
    )

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.root / "effect.lock"
        self.journal_path = self.root / "effect-journal.json"
        self._thread = threading.Lock()
        self.spans: list[tuple[str, float, float]] = []

    def check(self, effect_id: str, examiner, hold=None):
        with self._thread:
            handle = self._acquire()
            started_wall = time.time()
            started = time.monotonic()
            try:
                self._journal({
                    "effect_id": effect_id,
                    "phase": "started",
                    "at": started_wall,
                    "github_fencing": False,
                    "notice": self.FENCING_NOTICE,
                })
                if hold is not None:
                    hold()
                try:
                    return examiner()
                finally:
                    finished_wall = time.time()
                    self.spans.append((effect_id, started, time.monotonic()))
                    self._journal({
                        "effect_id": effect_id,
                        "phase": "finished",
                        "start": started_wall,
                        "end": finished_wall,
                        "github_fencing": False,
                        "notice": self.FENCING_NOTICE,
                    })
            finally:
                self._release(handle)

    def reconcile(self, effect_id: str, *, fenced: bool, classification: str, examiner):
        """Record a fenced reconciliation. A rejected call does not journal a success."""

        if not fenced:
            raise ControlError("NOT_AUTHORIZED", 403, "classification requires fencing", reason="fencing_required",
                               extra={"changed": False, "github_fencing": False})
        if self.SIMULATES_GITHUB_FENCING:
            raise ControlError("NOT_AUTHORIZED", 403, self.FENCING_NOTICE, reason="github_fencing")

        def wrapped():
            result = examiner()
            self._journal({
                "effect_id": effect_id,
                "phase": "reconciled",
                "classification": classification,
                "github_fencing": False,
                "notice": self.FENCING_NOTICE,
            })
            return result

        return self.check(effect_id, wrapped)

    def _acquire(self):
        if self.lock_path.is_symlink() or self.journal_path.is_symlink():
            raise ControlError("STORE_CORRUPT", 500, "effect path is a symlink", reason="store_symlink")
        handle = open(self.lock_path, "a+")
        os.chmod(self.lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def _release(self, handle):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    def _journal(self, row: dict) -> None:
        if self.journal_path.is_symlink():
            raise ControlError("STORE_CORRUPT", 500, "effect journal is a symlink", reason="store_symlink")
        rows = []
        if self.journal_path.exists():
            try:
                rows = json.loads(self.journal_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ControlError("STORE_CORRUPT", 500, "effect journal is damaged", reason="store_corrupt") from exc
            if not isinstance(rows, list):
                raise ControlError("STORE_CORRUPT", 500, "effect journal is damaged", reason="store_corrupt")
        rows.append(row)
        payload = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
        temporary = self.journal_path.with_suffix(".json.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            view = memoryview(payload)
            while len(view):
                written = os.write(descriptor, view)
                if written <= 0:
                    raise ControlError("STORE_CORRUPT", 500, "short write", reason="short_write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, self.journal_path)
