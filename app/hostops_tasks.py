"""Private SQLite task ledger, isolated from the application database.

No execution authority. Only read-only diagnostic task requests are accepted.
"""
import hashlib
import json
import os
import sqlite3
import stat
import time
import uuid
from contextlib import closing
from pathlib import Path


class TaskConflict(ValueError):
    pass


class TaskCapacity(ValueError):
    pass


class TaskStore:
    def __init__(self, directory):
        root = Path(directory)
        if not root.is_absolute():
            raise ValueError("absolute private state directory required")
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("unsafe state directory")
        path = root / "tasks.sqlite3"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("unsafe state database")
        finally:
            os.close(fd)
        self.path = path
        with closing(self.connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, actor TEXT NOT NULL, host_id TEXT NOT NULL,
                idem TEXT NOT NULL, digest TEXT NOT NULL, operation TEXT NOT NULL,
                reason TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                state TEXT NOT NULL, UNIQUE(actor,host_id,idem))""")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def public(row):
        return {k: row[k] for k in ("task_id", "actor", "host_id", "operation",
                                   "reason", "created_at", "expires_at", "state")}

    @staticmethod
    def expire(db, now):
        db.execute("UPDATE tasks SET state='TIMED_OUT' WHERE state='QUEUED' AND expires_at<=?", (now,))

    def enqueue(self, actor, host, operation, key, reason, timeout, now=None):
        now = int(time.time()) if now is None else now
        if operation not in ("host.health", "service.inventory", "agent.snapshot"):
            raise ValueError("unsupported operation")
        digest = hashlib.sha256(json.dumps([operation, reason, timeout], ensure_ascii=True,
                                           separators=(",", ":")).encode()).hexdigest()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db, now)
            row = db.execute("SELECT * FROM tasks WHERE actor=? AND host_id=? AND idem=?",
                             (actor,host,key)).fetchone()
            if row:
                if row["digest"] != digest:
                    raise TaskConflict("idempotency key conflict")
                return self.public(row), False
            # Do not silently discard audit records or accept unlimited requests.
            if db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] >= 10000:
                raise TaskCapacity("task storage capacity reached")
            if db.execute("SELECT COUNT(*) FROM tasks WHERE host_id=? AND state='QUEUED'", (host,)).fetchone()[0] >= 50:
                raise TaskCapacity("task queue full")
            task_id = uuid.uuid4().hex
            db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (task_id,actor,host,key,digest,operation,reason,now,now+timeout,"QUEUED"))
            return self.public(db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()), True

    def list(self, host, now=None):
        now = int(time.time()) if now is None else now
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db, now)
            return [self.public(row) for row in db.execute(
                "SELECT * FROM tasks WHERE host_id=? ORDER BY created_at DESC, task_id DESC LIMIT 100", (host,))]
