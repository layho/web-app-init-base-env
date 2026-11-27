"""Persistent integration planning ledger. No GitHub writes or publication authority.

The caller must supply a trusted private SQLite location and independently
verified candidate evidence. A lease reserves work; it never authorizes merge.
"""
import contextlib
import json
import re
import secrets
import sqlite3
import time
import os
import stat
from pathlib import Path


class QueueConflict(ValueError):
    pass


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("invalid commit SHA")
    return value


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value):
        raise ValueError("invalid identifier")
    return value


def _path(value):
    if not isinstance(value, str) or not value or len(value) > 512 or "\\" in value or any(ord(c)<32 for c in value):
        raise ValueError("invalid reserved path")
    if any(p in ("", ".", "..") for p in value.split("/")):
        raise ValueError("invalid reserved path")
    return value.casefold()


def _overlap(a, b):
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


class IntegrationQueue:
    def __init__(self, db_path):
        path = Path(db_path)
        if not path.is_absolute():
            raise ValueError("absolute queue path required")
        parent = path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ValueError("private queue directory required")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("private queue file required")
        finally:
            os.close(fd)
        self.db_path = path
        with self.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS integration_jobs(
                id TEXT PRIMARY KEY, version TEXT NOT NULL, head TEXT NOT NULL,
                dependencies TEXT NOT NULL, files TEXT NOT NULL, priority INTEGER NOT NULL,
                state TEXT NOT NULL, base TEXT, lease TEXT, expires INTEGER,
                created INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS integration_events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL,
                event TEXT NOT NULL, at INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS integration_version(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), version TEXT NOT NULL)""")

    @contextlib.contextmanager
    def transaction(self):
        with contextlib.closing(sqlite3.connect(self.db_path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _event(db, job, event, now):
        db.execute("INSERT INTO integration_events(job,event,at) VALUES(?,?,?)",(job,event,now))

    def activate(self, version):
        _id(version)
        with self.transaction() as db:
            row = db.execute("SELECT version FROM integration_version WHERE singleton=1").fetchone()
            if row and row["version"] != version:
                if db.execute("SELECT 1 FROM integration_jobs WHERE version=? AND state NOT IN ('INTEGRATED','CANCELLED') LIMIT 1",(row["version"],)).fetchone():
                    raise QueueConflict("previous version has unfinished jobs")
            db.execute("INSERT INTO integration_version VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET version=excluded.version",(version,))

    @staticmethod
    def _active(db, version):
        row = db.execute("SELECT version FROM integration_version WHERE singleton=1").fetchone()
        if not row or row["version"] != version:
            raise QueueConflict("version is not active")

    def register(self, job, version, head, dependencies, files, priority=0, now=None):
        _id(job); _id(version); _sha(head)
        if type(priority) is not int or not -100 <= priority <= 100:
            raise ValueError("invalid priority")
        if not isinstance(dependencies, (list,tuple)) or not isinstance(files,(list,tuple)) or len(dependencies)>100 or not 1<=len(files)<=1000:
            raise ValueError("invalid reservations")
        deps = sorted({_id(d) for d in dependencies})
        paths = sorted({_path(p) for p in files})
        if job in deps or len(paths)!=len(files):
            raise ValueError("invalid dependency or duplicate path")
        now = int(time.time()) if now is None else int(now)
        with self.transaction() as db:
            self._active(db,version)
            old = db.execute("SELECT * FROM integration_jobs WHERE id=?",(job,)).fetchone()
            spec = (version,head,json.dumps(deps),json.dumps(paths),priority)
            if old:
                if tuple(old[k] for k in ("version","head","dependencies","files","priority"))!=spec:
                    raise QueueConflict("job specification changed; use a new job identity")
                return False
            for row in db.execute("SELECT id,files FROM integration_jobs WHERE version=? AND state NOT IN ('INTEGRATED','CANCELLED')",(version,)):
                if any(_overlap(p,r) for p in paths for r in json.loads(row["files"])):
                    raise QueueConflict("file reservation conflicts with "+row["id"])
            # Dependencies must already exist. This makes cycles impossible.
            for dep in deps:
                row = db.execute("SELECT version,state FROM integration_jobs WHERE id=?",(dep,)).fetchone()
                if not row or row["version"]!=version or row["state"]=="CANCELLED":
                    raise QueueConflict("missing or invalid dependency")
            db.execute("INSERT INTO integration_jobs VALUES(?,?,?,?,?,?,'QUEUED',NULL,NULL,NULL,?)",(job,*spec,now))
            self._event(db,job,"QUEUED",now)
            return True

    def claim(self, version, base, ttl=120, now=None):
        _sha(base)
        if type(ttl) is not int or not 15<=ttl<=900:
            raise ValueError("invalid lease duration")
        now = int(time.time()) if now is None else int(now)
        with self.transaction() as db:
            self._active(db,version)
            for row in db.execute("SELECT id FROM integration_jobs WHERE version=? AND state='CLAIMED' AND expires<=?",(version,now)).fetchall():
                db.execute("UPDATE integration_jobs SET state='UNKNOWN',lease=NULL,expires=NULL WHERE id=?",(row["id"],))
                self._event(db,row["id"],"LEASE_EXPIRED",now)
            # UNKNOWN blocks the queue until an operator reconciles the real outcome.
            if db.execute("SELECT 1 FROM integration_jobs WHERE version=? AND state IN ('CLAIMED','UNKNOWN','PREPARED')",(version,)).fetchone():
                return None
            rows = db.execute("SELECT * FROM integration_jobs WHERE version=? AND state='QUEUED' ORDER BY priority DESC,created,id",(version,)).fetchall()
            for row in rows:
                deps=json.loads(row["dependencies"])
                if any(db.execute("SELECT state FROM integration_jobs WHERE id=?",(d,)).fetchone()["state"]!="INTEGRATED" for d in deps):
                    continue
                token=secrets.token_hex(32)
                db.execute("UPDATE integration_jobs SET state='CLAIMED',base=?,lease=?,expires=? WHERE id=?",(base,token,now+ttl,row["id"]))
                self._event(db,row["id"],"CLAIMED",now)
                return {"id":row["id"],"head":row["head"],"base":base,"lease":token,"expires":now+ttl,"files":json.loads(row["files"])}
            return None

    def finish(self, job, lease, current_base, changed_files, *, now=None):
        """Record completion of preparation only, never a merge or test success."""
        _sha(current_base)
        if not isinstance(changed_files,(list,tuple)) or not 1<=len(changed_files)<=1000:
            raise ValueError("invalid changed files")
        actual=[_path(p) for p in changed_files]
        now=int(time.time()) if now is None else int(now)
        with self.transaction() as db:
            row=db.execute("SELECT * FROM integration_jobs WHERE id=?",(job,)).fetchone()
            if not row or row["state"]!="CLAIMED" or not isinstance(lease,str) or not secrets.compare_digest(row["lease"],lease) or now>=row["expires"]:
                raise QueueConflict("invalid or expired lease")
            if row["base"]!=current_base:
                raise QueueConflict("baseline changed")
            reserved=json.loads(row["files"])
            if any(not any(p==r or p.startswith(r+"/") for r in reserved) for p in actual):
                raise QueueConflict("diff exceeds reservation")
            db.execute("UPDATE integration_jobs SET state='PREPARED',lease=NULL,expires=NULL WHERE id=?",(job,))
            self._event(db,job,"PREPARED",now)

    def retry_preparation(self, job, expected_head, expected_base, *, now=None):
        """Only a trusted local coordinator may call after reconciling evidence.
        PREPARED has no publication side effect; UNKNOWN is deliberately excluded.
        """
        _sha(expected_head); _sha(expected_base)
        now=int(time.time()) if now is None else int(now)
        with self.transaction() as db:
            row=db.execute("SELECT * FROM integration_jobs WHERE id=?",(job,)).fetchone()
            if not row or row["state"]!="PREPARED" or row["head"]!=expected_head or row["base"]!=expected_base:
                raise QueueConflict("prepared candidate changed")
            db.execute("UPDATE integration_jobs SET state='QUEUED',base=NULL WHERE id=?",(job,))
            self._event(db,job,"PREPARATION_RETRY",now)

    def renew(self, job, lease, base, ttl=120, *, now=None):
        _sha(base)
        if type(ttl) is not int or not 15<=ttl<=900:
            raise ValueError("invalid lease duration")
        now=int(time.time()) if now is None else int(now)
        with self.transaction() as db:
            row=db.execute("SELECT * FROM integration_jobs WHERE id=?",(job,)).fetchone()
            if not row or row["state"]!="CLAIMED" or row["base"]!=base or not isinstance(lease,str) or not secrets.compare_digest(row["lease"],lease) or now>=row["expires"]:
                raise QueueConflict("invalid or expired lease")
            expires=max(row["expires"],now+ttl)
            db.execute("UPDATE integration_jobs SET expires=? WHERE id=?",(expires,job))
            self._event(db,job,"RENEWED",now)
            return expires

    def events(self, version, after=0):
        if type(after) is not int or not 0<=after<=9223372036854775807:
            raise ValueError("invalid cursor")
        with self.transaction() as db:
            return [dict(r) for r in db.execute("SELECT e.seq,e.job,e.event,e.at FROM integration_events e JOIN integration_jobs j ON j.id=e.job WHERE j.version=? AND e.seq>? ORDER BY e.seq LIMIT 100",(version,after))]

    def snapshot(self, version):
        with self.transaction() as db:
            rows=db.execute("SELECT id,version,head,dependencies,files,priority,state,base,expires,created FROM integration_jobs WHERE version=? ORDER BY priority DESC,created,id",(version,)).fetchall()
            return [dict(row, dependencies=json.loads(row["dependencies"]),files=json.loads(row["files"])) for row in rows]

    def reservation_conflicts(self, version, files):
        paths=[_path(p) for p in files]
        return [row["id"] for row in self.snapshot(version)
                if row["state"] not in ("INTEGRATED","CANCELLED")
                and any(_overlap(p,r) for p in paths for r in row["files"])]
