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

            db.execute("""CREATE TABLE IF NOT EXISTS task_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(task_id),
                actor TEXT NOT NULL, state TEXT NOT NULL, at INTEGER NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS task_events_task ON task_events(task_id,seq)")
            db.execute("CREATE INDEX IF NOT EXISTS tasks_host_state ON tasks(host_id,state,created_at)")

            db.execute("""CREATE TABLE IF NOT EXISTS host_snapshots (
                host_id TEXT PRIMARY KEY, received_at INTEGER NOT NULL, body TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS integration_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, host_id TEXT NOT NULL,
                actor TEXT NOT NULL, operation TEXT NOT NULL, evidence_state TEXT NOT NULL,
                at INTEGER NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS integration_host_seq ON integration_events(host_id,seq)")

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
    def event(db, task_id, actor, state, now):
        db.execute("INSERT INTO task_events(task_id,actor,state,at) VALUES (?,?,?,?)",
                   (task_id,actor,state,now))

    @staticmethod
    def expire(db, now):
        for state, query in (
            ("TIMED_OUT","SELECT task_id FROM tasks WHERE state='QUEUED' AND expires_at<=?"),
            ("UNKNOWN","SELECT tasks.task_id FROM tasks JOIN leases USING(task_id) WHERE tasks.state='CLAIMED' AND leases.expires_at<=?"),
        ):
            for row in db.execute(query,(now,)).fetchall():
                db.execute("UPDATE tasks SET state=? WHERE task_id=?",(state,row["task_id"]))
                TaskStore.event(db,row["task_id"],"system",state,now)

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
            self.event(db,task_id,actor,"QUEUED",now)
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
            self.event(db,row["task_id"],"agent:"+host,"CLAIMED",now)
            return {"job":envelope,"signature":signature,"lease_token":token}


    def report(self, host, task_id, token, outcome, result, now=None):
        now = int(time.time()) if now is None else now
        if outcome not in ("SUCCESS","FAILED") or (outcome=="SUCCESS") != (result is not None):
            raise ValueError("invalid receipt outcome")
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
            self.event(db,task_id,"agent:"+host,outcome,now)
            return True

    def detail(self, host, task_id):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db,int(time.time()))
            row=db.execute("SELECT * FROM tasks WHERE host_id=? AND task_id=?",(host,task_id)).fetchone()
            if not row:
                return None
            receipt=db.execute("SELECT body,received_at FROM receipts WHERE task_id=?",(task_id,)).fetchone()
            events=[dict(e) for e in db.execute("SELECT seq,actor,state,at FROM task_events WHERE task_id=? ORDER BY seq",(task_id,))]
            return {**self.public(row),"events":events,"receipt":{**json.loads(receipt["body"]),"received_at":receipt["received_at"]} if receipt else None}


    def cancel(self, host, task_id, actor, now=None):
        now=int(time.time()) if now is None else now
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db,now)
            row=db.execute("SELECT * FROM tasks WHERE host_id=? AND task_id=?",(host,task_id)).fetchone()
            if row is None:
                return None
            if row["state"]=="CANCELLED":
                return self.public(row)
            if row["state"]!="QUEUED":
                raise TaskConflict("only queued tasks can be cancelled")
            db.execute("UPDATE tasks SET state='CANCELLED' WHERE task_id=?",(task_id,))
            self.event(db,task_id,actor,"CANCELLED",now)
            return self.public(db.execute("SELECT * FROM tasks WHERE task_id=?",(task_id,)).fetchone())

    def audit(self, host, after=0):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db,int(time.time()))
            return [dict(row) for row in db.execute("""SELECT e.seq,e.task_id,e.actor,e.state,e.at,t.operation
                FROM task_events e JOIN tasks t USING(task_id) WHERE t.host_id=? AND e.seq>?
                ORDER BY e.seq LIMIT 100""",(host,after))]


    def save_snapshot(self, host, snapshot, now=None):
        now=int(time.time()) if now is None else now
        body=json.dumps(snapshot,sort_keys=True,separators=(",",":"),ensure_ascii=True,allow_nan=False)
        if len(body)>16384 or snapshot.get("host_id")!=host:
            raise ValueError("invalid host snapshot")
        with closing(self.connect()) as db,db:
            db.execute("""INSERT INTO host_snapshots VALUES (?,?,?)
                ON CONFLICT(host_id) DO UPDATE SET received_at=excluded.received_at,body=excluded.body""",
                (host,now,body))

    def snapshot(self, host, now=None):
        now=int(time.time()) if now is None else now
        with closing(self.connect()) as db:
            row=db.execute("SELECT received_at,body FROM host_snapshots WHERE host_id=?",(host,)).fetchone()
        if row is None:
            return None
        elapsed=now-row["received_at"]
        # A wall-clock rollback cannot make old evidence appear current.
        age=elapsed if elapsed>=0 else 91
        return age,json.loads(row["body"])

    def integration_event(self, host, operation, evidence_state):
        if operation not in ("hostops_get_health","hostops_list_services","hostops_get_module_versions"):
            raise ValueError("unsupported integration operation")
        if evidence_state not in ("never_seen","online","stale"):
            raise ValueError("invalid evidence state")
        with closing(self.connect()) as db,db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM integration_events").fetchone()[0]>=100000:
                raise TaskCapacity("integration audit capacity reached")
            cursor=db.execute("INSERT INTO integration_events(host_id,actor,operation,evidence_state,at) VALUES (?,?,?,?,?)",
                              (host,"mcp:readonly",operation,evidence_state,int(time.time())))
            return cursor.lastrowid

    def integration_audit(self,host,after=0):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute("""SELECT seq,actor,operation,evidence_state,at
                FROM integration_events WHERE host_id=? AND seq>? ORDER BY seq LIMIT 100""",(host,after))]
