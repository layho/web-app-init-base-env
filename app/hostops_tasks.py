"""Private SQLite task ledger, isolated from the application database.

No execution authority. Only read-only diagnostic task requests are accepted.
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timezone
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

            db.execute("""CREATE TABLE IF NOT EXISTS leases (
                task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
                token_hash TEXT NOT NULL, expires_at INTEGER NOT NULL,
                envelope TEXT NOT NULL)""")

            db.execute("""CREATE TABLE IF NOT EXISTS receipts (
                task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
                body TEXT NOT NULL, received_at INTEGER NOT NULL)""")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    @staticmethod
    def public(row):
        return {k: row[k] for k in ("task_id", "actor", "host_id", "operation",
                                   "reason", "created_at", "expires_at", "state")}

    @staticmethod
    def expire(db, now):
        db.execute("UPDATE tasks SET state='TIMED_OUT' WHERE state='QUEUED' AND expires_at<=?", (now,))

        db.execute("""UPDATE tasks SET state='UNKNOWN' WHERE state='CLAIMED' AND task_id IN
            (SELECT task_id FROM leases WHERE expires_at<=?)""", (now,))

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


    def claim(self, host, signing_key, now=None):
        """Claim once, atomically. Lost or expired leases never re-execute automatically."""
        now = int(time.time()) if now is None else now
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db, now)
            if db.execute("SELECT 1 FROM tasks WHERE host_id=? AND state='CLAIMED'", (host,)).fetchone():
                return None
            row = db.execute("""SELECT * FROM tasks WHERE host_id=? AND state='QUEUED'
                                AND expires_at>? ORDER BY created_at,task_id LIMIT 1""", (host,now)).fetchone()
            if row is None:
                return None
            expires = min(row["expires_at"], now+60)
            token = secrets.token_urlsafe(32)
            def stamp(value):
                return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            envelope = {"schema":2, "task_id":row["task_id"], "host_id":host,
                        "actor":row["actor"], "operation":row["operation"], "parameters":{},
                        "parameter_sha256":hashlib.sha256(b"{}").hexdigest(),
                        "issued_at":stamp(now), "expires_at":stamp(expires),
                        "nonce":secrets.token_hex(16), "policy_version":"readonly-v1",
                        "lease_sha256":hashlib.sha256(token.encode("ascii")).hexdigest()}
            encoded = json.dumps(envelope, sort_keys=True, separators=(",",":"), ensure_ascii=True)
            signature = hmac.new(signing_key, b"hostops/job/v2\0"+encoded.encode("ascii"), hashlib.sha256).hexdigest()
            db.execute("INSERT INTO leases VALUES (?,?,?,?)",
                       (row["task_id"], envelope["lease_sha256"], expires, encoded))
            db.execute("UPDATE tasks SET state='CLAIMED' WHERE task_id=?", (row["task_id"],))
            return {"job":envelope,"signature":signature,"lease_token":token}


    def report(self, host, task_id, token, outcome, result, now=None):
        now = int(time.time()) if now is None else now
        body = json.dumps({"outcome":outcome,"result":result},sort_keys=True,separators=(",",":"),ensure_ascii=True,allow_nan=False)
        if len(body)>16384:
            raise ValueError("receipt exceeds bound")
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT t.*,l.token_hash,l.expires_at AS lease_expires
                                FROM tasks t JOIN leases l USING(task_id)
                                WHERE t.task_id=? AND t.host_id=?""",(task_id,host)).fetchone()
            if row is None or not hmac.compare_digest(digest,row["token_hash"]):
                raise TaskConflict("invalid lease")
            previous = db.execute("SELECT body FROM receipts WHERE task_id=?",(task_id,)).fetchone()
            if previous:
                if previous["body"] != body:
                    raise TaskConflict("receipt conflict")
                return False
            if row["state"] != "CLAIMED" or now >= row["lease_expires"]:
                raise TaskConflict("expired lease")
            if outcome == "SUCCESS" and (type(result) is not dict or result.get("operation") != row["operation"]):
                raise ValueError("operation mismatch")
            db.execute("INSERT INTO receipts VALUES (?,?,?)",(task_id,body,now))
            db.execute("UPDATE tasks SET state=? WHERE task_id=?",(outcome,task_id))
            return True

    def detail(self, host, task_id):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db,int(time.time()))
            row=db.execute("SELECT * FROM tasks WHERE host_id=? AND task_id=?",(host,task_id)).fetchone()
            if not row:
                return None
            receipt=db.execute("SELECT body,received_at FROM receipts WHERE task_id=?",(task_id,)).fetchone()
            return {**self.public(row),"receipt":{**json.loads(receipt["body"]),"received_at":receipt["received_at"]} if receipt else None}
