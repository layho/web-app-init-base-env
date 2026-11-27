"""Durable trusted-host journal for the existing exact-candidate preview push.

No network writes here. Caller holds the coordinator flock throughout.
Unknown outcomes block subsequent pushes; a matching remote SHA confirms only
preview integration, not deployment, acceptance or main publication.
"""
import os
import re
import sqlite3
import stat
import time
from pathlib import Path
from contextlib import contextmanager, closing

_SHA=re.compile(r"[0-9a-f]{40}\Z")

def _sha(value):
    if not isinstance(value,str) or not _SHA.fullmatch(value):
        raise ValueError("invalid SHA")
    return value

class PreviewJournal:
    def __init__(self,path):
        self.path=Path(path)
        if not self.path.is_absolute():
            raise ValueError("absolute journal path required")
        st=self.path.parent.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode&0o077:
            raise ValueError("unsafe journal directory")
        fd=os.open(self.path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
        try:
            st=os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o600:
                raise ValueError("unsafe journal file")
        finally:
            os.close(fd)
        with self.tx() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS preview_attempts(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, pr INTEGER NOT NULL,
                head TEXT NOT NULL, base TEXT NOT NULL, candidate TEXT NOT NULL,
                state TEXT NOT NULL, created INTEGER NOT NULL, finished INTEGER)""")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_pending_preview ON preview_attempts(state) WHERE state='PENDING'")

    @contextmanager
    def tx(self):
        with closing(sqlite3.connect(self.path,timeout=5)) as db:
            db.row_factory=sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def stage(self,number,head,base,changed_files):
        """Atomically register and prepare the already verified candidate.

        Called under the host coordinator lock after all original gates.
        Never alters existing reservations or bypasses outstanding work.
        """
        import json
        if type(number) is not int or number<=0:
            raise ValueError("invalid PR")
        _sha(head); _sha(base)
        if not isinstance(changed_files,list) or not 1<=len(changed_files)<=1000:
            raise ValueError("invalid changed paths")
        paths=[]
        for path in changed_files:
            if (not isinstance(path,str) or not path or len(path)>512 or chr(92) in path
                    or any(ord(c)<32 for c in path) or any(p in ("",".","..") for p in path.split("/"))):
                raise ValueError("invalid changed path")
            paths.append(path.casefold())
        if len(set(paths))!=len(paths):
            raise ValueError("ambiguous changed paths")
        paths=sorted(paths)
        job="pr-"+str(number)
        now=int(time.time())
        with self.tx() as db:
            version=db.execute("SELECT version FROM integration_version WHERE singleton=1").fetchone()
            if version is None:
                raise ValueError("no active integration version")
            version=version["version"]
            if db.execute("SELECT 1 FROM preview_attempts WHERE state='PENDING'").fetchone():
                raise ValueError("previous push unresolved")
            old=db.execute("SELECT * FROM integration_jobs WHERE id=?",(job,)).fetchone()
            if old and (old["version"]!=version or old["head"]!=head):
                raise ValueError("candidate registration changed")
            others=db.execute("SELECT id,files,state FROM integration_jobs WHERE version=? AND id!=? AND state NOT IN ('INTEGRATED','CANCELLED')",(version,job)).fetchall()
            if any(row["state"] in ("CLAIMED","UNKNOWN","PREPARED") for row in others):
                raise ValueError("another integration is active")
            if any(p==r or p.startswith(r+"/") or r.startswith(p+"/") for row in others for r in json.loads(row["files"]) for p in paths):
                raise ValueError("candidate conflicts with reservation")
            if old is None:
                # Do not leapfrog previously queued tasks.
                if others:
                    raise ValueError("registered work takes precedence")
                db.execute("INSERT INTO integration_jobs VALUES(?,?,?,?,?,?,'QUEUED',NULL,NULL,NULL,?)",
                           (job,version,head,"[]",json.dumps(paths),0,now))
                db.execute("INSERT INTO integration_events(job,event,at) VALUES(?,?,?)",(job,"QUEUED",now))
                reserved=paths
            else:
                if old["state"] not in ("QUEUED","PREPARED"):
                    raise ValueError("candidate not eligible for preparation")
                if old["state"]=="PREPARED" and old["base"]!=base:
                    raise ValueError("prepared base changed")
                reserved=json.loads(old["files"])
                for dep in json.loads(old["dependencies"]):
                    row=db.execute("SELECT state FROM integration_jobs WHERE id=?",(dep,)).fetchone()
                    if row is None or row["state"]!="INTEGRATED":
                        raise ValueError("dependency unfinished")
            if any(not any(p==r or p.startswith(r+"/") for r in reserved) for p in paths):
                raise ValueError("candidate exceeds reservation")
            if old is None or old["state"]=="QUEUED":
                db.execute("UPDATE integration_jobs SET state='PREPARED',base=?,lease=NULL,expires=NULL WHERE id=?",(base,job))
                db.execute("INSERT INTO integration_events(job,event,at) VALUES(?,?,?)",(job,"PREPARED",now))
            return job

    def prepare(self,number,head,base,candidate,changed_files=None):
        """In managed mode, consume the exact PREPARED reservation atomically."""

        if type(number) is not int or number<=0:
            raise ValueError("invalid PR")
        for value in (head,base,candidate):
            _sha(value)
        with self.tx() as db:
            if db.execute("SELECT 1 FROM preview_attempts WHERE state='PENDING'").fetchone():
                raise ValueError("previous push unresolved")
            if changed_files is not None:
                import json
                version=db.execute("SELECT version FROM integration_version WHERE singleton=1").fetchone()
                if version is None:
                    raise ValueError("no active integration version")
                rows=db.execute("SELECT * FROM integration_jobs WHERE version=? AND state='PREPARED'",(version["version"],)).fetchall()
                if len(rows)!=1:
                    raise ValueError("no unique prepared reservation")
                row=rows[0]
                if row["id"]!="pr-"+str(number) or row["head"]!=head or row["base"]!=base:
                    raise ValueError("prepared reservation differs from candidate")
                reserved=json.loads(row["files"])
                if not changed_files or any(not isinstance(p,str) or not any(p.casefold()==r or p.casefold().startswith(r+"/") for r in reserved) for p in changed_files):
                    raise ValueError("candidate exceeds prepared reservation")
                db.execute("UPDATE integration_jobs SET state='UNKNOWN' WHERE id=?",(row["id"],))
                db.execute("INSERT INTO integration_events(job,event,at) VALUES(?,?,?)",(row["id"],"PUSH_STARTED",int(time.time())))
            db.execute("INSERT INTO preview_attempts(pr,head,base,candidate,state,created) VALUES(?,?,?,?,'PENDING',?)",
                       (number,head,base,candidate,int(time.time())))

    @staticmethod
    def _finish_reservation(db,number,head,base):
        # Journal-only deployments have no queue table. In managed mode the
        # reservation and receipt share the same transaction and database.
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='integration_jobs'").fetchone():
            return
        job="pr-"+str(number)
        row=db.execute("SELECT state,head,base FROM integration_jobs WHERE id=?",(job,)).fetchone()
        if row is None:
            return
        if (row["state"],row["head"],row["base"])!=("UNKNOWN",head,base):
            raise ValueError("reservation changed during push")
        db.execute("UPDATE integration_jobs SET state='INTEGRATED',lease=NULL,expires=NULL WHERE id=?",(job,))
        db.execute("INSERT INTO integration_events(job,event,at) VALUES(?,?,?)",(job,"INTEGRATED",int(time.time())))

    def confirm(self,number,head,base,candidate,actual):
        if _sha(actual)!=_sha(candidate):
            raise ValueError("remote candidate mismatch")
        with self.tx() as db:
            changed=db.execute("UPDATE preview_attempts SET state='INTEGRATED',finished=? WHERE pr=? AND head=? AND base=? AND candidate=? AND state='PENDING'",
                               (int(time.time()),number,head,base,candidate)).rowcount
            if changed!=1:
                raise ValueError("pending attempt mismatch")
            self._finish_reservation(db,number,head,base)

    def reconcile(self,read_preview):
        with self.tx() as db:
            row=db.execute("SELECT * FROM preview_attempts WHERE state='PENDING'").fetchone()
            if row is None:
                return True
            actual=_sha(read_preview())
            # Even an unchanged base cannot prove the remote push never occurred.
            # Keep pending until a trusted operator reconciles ambiguous outcomes.
            if actual!=row["candidate"]:
                return False
            db.execute("UPDATE preview_attempts SET state='INTEGRATED',finished=? WHERE seq=?",
                       (int(time.time()),row["seq"]))
            self._finish_reservation(db,row["pr"],row["head"],row["base"])
            return True

    def history(self):
        with self.tx() as db:
            return [dict(r) for r in db.execute("SELECT * FROM preview_attempts ORDER BY seq DESC LIMIT 100")]
