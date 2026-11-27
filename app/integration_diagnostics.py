"""Read-only integration diagnostics; never create or mutate the queue database."""
from pathlib import Path
import os
import re
import sqlite3
import stat
from contextlib import closing

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_STATES = {"QUEUED", "CLAIMED", "PREPARED", "UNKNOWN", "INTEGRATED", "CANCELLED"}
_EVENTS = {"QUEUED", "CLAIMED", "PREPARED", "LEASE_EXPIRED", "PREPARATION_RETRY", "RENEWED",
           "INTEGRATED", "CANCELLED", "PUSH_STARTED", "PUSH_REJECTED"}


def _safe_id(value):
    return isinstance(value,str) and _ID.fullmatch(value) is not None


def _safe_sha(value):
    return value if isinstance(value,str) and _SHA.fullmatch(value) else None


def _timestamp(value):
    return value if type(value) is int and 0<=value<=253402300799 else None


def read_queue(config=None):
    """Report only an explicitly configured, private, existing ledger.

    No caller-provided filename, raw log, lease token or reserved paths are
    returned. A missing or unreadable source never means an empty healthy queue.
    The web process needs separately provisioned read access to the ledger;
    this reader does not grant it or change filesystem permissions.
    """
    config = os.getenv("APP_INTEGRATION_QUEUE_FILE", "") if config is None else config
    empty = {"version":None,"jobs":[],"events":[],"truncated":False}
    if not config:
        return dict(empty,state="not_configured")
    try:
        path=Path(config)
        if not path.is_absolute() or path.resolve(strict=True)!=path:
            raise ValueError("unsafe path")
        parent=path.parent.lstat()
        info=path.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or stat.S_IMODE(parent.st_mode)!=0o700
                or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600
                or parent.st_uid!=info.st_uid or info.st_uid!=os.getuid()):
            raise ValueError("unsafe storage")
        with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=1)) as db:
            db.row_factory=sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            version=db.execute("SELECT version FROM integration_version WHERE singleton=1").fetchone()
            if version is None:
                return dict(empty,state="not_initialized")
            version=version["version"]
            if not _safe_id(version):
                raise ValueError("invalid version")
            rows=db.execute("SELECT id,state,head,base,created,expires FROM integration_jobs WHERE version=? ORDER BY created DESC,id LIMIT 101",(version,)).fetchall()
            events=db.execute("SELECT e.seq,e.job,e.event,e.at FROM integration_events e JOIN integration_jobs j ON j.id=e.job WHERE j.version=? ORDER BY e.seq DESC LIMIT 101",(version,)).fetchall()
            jobs=[]
            for row in rows[:100]:
                if not _safe_id(row["id"]) or row["state"] not in _STATES:
                    raise ValueError("invalid job")
                jobs.append({"id":row["id"],"state":row["state"],"head":_safe_sha(row["head"]),
                             "base":_safe_sha(row["base"]),"created":_timestamp(row["created"]),
                             "expires":_timestamp(row["expires"])})
            safe_events=[]
            for row in events[:100]:
                event=row["event"]
                if isinstance(event,str) and event.startswith("RECONCILED:"):
                    event="RECONCILED"
                if not _safe_id(row["job"]) or event not in _EVENTS|{"RECONCILED"}:
                    raise ValueError("invalid event")
                safe_events.append({"seq":row["seq"],"job":row["job"],"event":event,"at":_timestamp(row["at"])})
            return {"state":"available","version":version,"jobs":jobs,"events":safe_events,
                    "truncated":len(rows)>100 or len(events)>100}
    except (OSError,ValueError,TypeError,sqlite3.Error):
        return dict(empty,state="unavailable")


def read_attempts(config=None):
    """Read actual host push receipts, independently of planning queue state.

    A trusted local administrator may configure a private same-user journal
    or the shared managed queue ledger. No browser-supplied path is accepted.
    """
    config=os.getenv("APP_PREVIEW_JOURNAL_FILE","") if config is None else config
    empty={"attempts":[],"truncated":False}
    if not config:
        return dict(empty,state="not_configured")
    try:
        path=Path(config)
        if not path.is_absolute() or path.resolve(strict=True)!=path:
            raise ValueError("unsafe path")
        parent=path.parent.lstat()
        info=path.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or stat.S_IMODE(parent.st_mode)!=0o700
                or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600
                or parent.st_uid!=info.st_uid or info.st_uid!=os.getuid()):
            raise ValueError("unsafe storage")
        with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=1)) as db:
            db.row_factory=sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            rows=db.execute("SELECT seq,pr,head,base,candidate,state,created,finished FROM preview_attempts ORDER BY seq DESC LIMIT 101").fetchall()
            attempts=[]
            for row in rows[:100]:
                if (type(row["seq"]) is not int or row["seq"]<=0 or type(row["pr"]) is not int or row["pr"]<=0
                        or row["state"] not in {"PENDING","INTEGRATED","REJECTED"}
                        or any(_safe_sha(row[k]) is None for k in ("head","base","candidate"))
                        or _timestamp(row["created"]) is None
                        or (row["state"]=="PENDING" and row["finished"] is not None)
                        or (row["state"]!="PENDING" and (_timestamp(row["finished"]) is None or row["finished"]<row["created"]))):
                    raise ValueError("invalid host receipt")
                attempts.append(dict(row))
            return {"state":"available","attempts":attempts,"truncated":len(rows)>100}
    except (OSError,ValueError,TypeError,sqlite3.Error):
        return dict(empty,state="unavailable")
