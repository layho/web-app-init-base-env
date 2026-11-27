"""Outbound heartbeat and signed read-only diagnostic agent; no inbound listener."""
import argparse
import hashlib
import json
import os
import re
import stat
import time
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from contextlib import closing
from protocol import verify_claim, envelope_digest
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import URLError, HTTPError

from diagnostics import execute


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def config():
    host = os.getenv("HOSTOPS_HOST_ID", "")
    origin = os.getenv("HOSTOPS_GATEWAY_ORIGIN", "")
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or any(c.isspace() for c in origin)):
        raise ValueError("HTTPS gateway origin required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", host):
        raise ValueError("invalid host identity")
    path = os.getenv("HOSTOPS_AGENT_TOKEN_FILE", "")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="ascii") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 512):
            raise ValueError("unsafe credential file")
        token = stream.read(513).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise ValueError("invalid credential")
    return host, origin.rstrip("/"), token


def runtime_digest():
    root=Path(__file__).resolve().parent
    manifest={}
    for name in ("agent.py","diagnostics.py","protocol.py"):
        data=(root/name).read_bytes()
        if len(data)>262144:
            raise ValueError("runtime file exceeds bound")
        manifest[name]=hashlib.sha256(data).hexdigest()
    return hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def preview_receipt_snapshot():
    """Optional fixed local journal evidence; no remote command or path input."""
    if os.getenv("HOSTOPS_PREVIEW_EVIDENCE")!="true":
        return None
    empty={"state":"unavailable","attempts":[],"truncated":False}
    try:
        root=Path.home()/".local/share/macmini-pr-tests"
        path=root/("integration-queue.sqlite3" if os.getenv("MACCI_MANAGED_QUEUE")=="1" else "preview-integration.sqlite3")
        for p,mode,kind in ((root,0o700,stat.S_ISDIR),(path,0o600,stat.S_ISREG)):
            info=p.lstat()
            if not kind(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=mode:
                return empty
        if path.resolve(strict=True)!=path:
            return empty
        with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=1)) as db:
            db.row_factory=sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            rows=db.execute("SELECT seq,pr,head,base,candidate,state,created,finished FROM preview_attempts ORDER BY seq DESC LIMIT 21").fetchall()
            records=[]
            for row in rows[:20]:
                if (type(row["seq"]) is not int or row["seq"]<=0 or type(row["pr"]) is not int or row["pr"]<=0
                    or any(not isinstance(row[k],str) or not re.fullmatch(r"[0-9a-f]{40}",row[k]) for k in ("head","base","candidate"))
                    or row["state"] not in ("PENDING","INTEGRATED","REJECTED")
                    or type(row["created"]) is not int or not 0<=row["created"]<=253402300799
                    or (row["state"]=="PENDING" and row["finished"] is not None)
                    or (row["state"]!="PENDING" and (type(row["finished"]) is not int or not row["created"]<=row["finished"]<=253402300799))):
                    return empty
                records.append(dict(row))
            return {"state":"available","attempts":records,"truncated":len(rows)>20}
    except (OSError,ValueError,TypeError,sqlite3.Error):
        return empty


def heartbeat_payload(host):
    snap = execute("agent.snapshot", {})["result"]
    health = snap["health"]
    payload={"host_id": host, "system": health["system"],
            "architecture": health["architecture"], "disk": health["disk"],
            "services": snap["services"], "capabilities": snap["capabilities"],
            "runtime_digest":runtime_digest(),
            "dispatch_active":os.getenv("HOSTOPS_AGENT_DISPATCH")=="true"}
    evidence=preview_receipt_snapshot()
    if evidence is not None:
        payload["preview_evidence"]=evidence
    return payload


def send_heartbeat(host, origin, token, opener=None):
    body = json.dumps(heartbeat_payload(host), ensure_ascii=True).encode("ascii")
    if len(body) > 16384:
        raise ValueError("heartbeat exceeds bound")
    request = Request(origin + "/api/hostops/v1/agent/heartbeat", data=body,
                      headers={"Authorization": "Bearer " + token,
                               "Content-Type": "application/json"}, method="POST")
    with (opener or build_opener(NoRedirect())).open(request, timeout=10) as response:
        raw = response.read(4097)
        if len(raw) > 4096:
            raise ValueError("response exceeds bound")
        result = json.loads(raw)
    if type(result) is not dict or result.get("accepted") is not True or result.get("schema") != 1:
        raise ValueError("heartbeat not acknowledged")


def post_json(origin, token, route, payload):
    if route not in ("claim","report"):
        raise ValueError("unsupported route")
    body=json.dumps(payload,ensure_ascii=True,allow_nan=False).encode("ascii")
    if len(body)>32768:
        raise ValueError("request too large")
    request=Request(origin+"/api/hostops/v1/agent/"+route,data=body,method="POST",
                    headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"})
    with build_opener(NoRedirect()).open(request,timeout=10) as response:
        raw=response.read(32769)
    if len(raw)>32768:
        raise ValueError("response too large")
    result=json.loads(raw)
    if type(result) is not dict:
        raise ValueError("invalid response")
    return result


def read_job_config():
    root=Path(os.environ["HOSTOPS_AGENT_STATE_DIR"])
    if not root.is_absolute():
        raise ValueError("absolute state directory required")
    info=root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
        raise ValueError("unsafe state directory")
    fd=os.open(os.environ["HOSTOPS_JOB_KEY_FILE"],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,"r",encoding="ascii") as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>128:
            raise ValueError("unsafe signing key")
        key=stream.read(129).strip()
    if not re.fullmatch(r"[0-9a-f]{64}",key):
        raise ValueError("invalid signing key")
    path=root/"execution.sqlite3"
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600:
            raise ValueError("unsafe execution ledger")
    finally:
        os.close(fd)
    return bytes.fromhex(key),path


def initialize_ledger(db):
    db.execute("CREATE TABLE IF NOT EXISTS executions(task_id TEXT PRIMARY KEY,nonce TEXT UNIQUE,digest TEXT NOT NULL)")
    db.execute("""CREATE TABLE IF NOT EXISTS outbox(
        task_id TEXT PRIMARY KEY, host_id TEXT NOT NULL, receipt TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'PENDING')""")


def run_claim(claim,host,key,path):
    job=verify_claim(claim,host,key,datetime.now(timezone.utc))
    digest=envelope_digest(job)
    with closing(sqlite3.connect(path,timeout=3)) as db,db:
        initialize_ledger(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("INSERT INTO executions VALUES (?,?,?)",(job["task_id"],job["nonce"],digest))
        except sqlite3.IntegrityError:
            raise ValueError("replayed task") from None
    try:
        result=execute(job["operation"],{})
        outcome="SUCCESS"
    except (OSError,ValueError):
        result=None
        outcome="FAILED"
    receipt={"host_id":host,"task_id":job["task_id"],"lease_token":claim["lease_token"],
             "outcome":outcome,"result":result}
    encoded=json.dumps(receipt,sort_keys=True,separators=(",",":"),ensure_ascii=True,allow_nan=False)
    if len(encoded)>16384:
        raise ValueError("receipt exceeds bound")
    with closing(sqlite3.connect(path,timeout=3)) as db,db:
        db.execute("INSERT INTO outbox(task_id,host_id,receipt) VALUES (?,?,?)",
                   (job["task_id"],host,encoded))
    return receipt


def flush_receipts(host,origin,token,path):
    # Never re-run completed diagnostics merely because receipt delivery failed.
    with closing(sqlite3.connect(path,timeout=3)) as db,db:
        initialize_ledger(db)
        pending=db.execute("SELECT task_id,receipt FROM outbox WHERE host_id=? AND state='PENDING' ORDER BY rowid LIMIT 20",(host,)).fetchall()
    for task_id,encoded in pending:
        receipt=json.loads(encoded)
        try:
            if post_json(origin,token,"report",receipt).get("accepted") is not True:
                raise ValueError("receipt not acknowledged")
            state="ACKNOWLEDGED"
        except HTTPError as exc:
            # A stale/conflicting lease is not successful execution evidence.
            if exc.code!=409:
                raise
            state="REJECTED"
        with closing(sqlite3.connect(path,timeout=3)) as db,db:
            db.execute("UPDATE outbox SET state=?,receipt='' WHERE task_id=? AND state='PENDING'",(state,task_id))


def dispatch_once(host,origin,token,key,path):
    flush_receipts(host,origin,token,path)
    claim=post_json(origin,token,"claim",{"host_id":host}).get("claim")
    if claim is None:
        return
    run_claim(claim,host,key,path)
    flush_receipts(host,origin,token,path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        host, origin, token = config()
        job_config = read_job_config() if os.getenv('HOSTOPS_AGENT_DISPATCH') == 'true' else None
        if job_config and job_config[0].hex() == token:
            raise ValueError('separate signing key required')
    except (OSError, ValueError, KeyError):
        print("HostOps agent configuration unavailable", flush=True)
        return 2
    delay = 15
    while True:
        try:
            send_heartbeat(host, origin, token)
            print("HostOps heartbeat accepted", flush=True)
            if job_config:
                dispatch_once(host,origin,token,*job_config)
            delay = 15
            if args.once:
                return 0
        except (OSError, URLError, ValueError, sqlite3.Error):
            print("HostOps heartbeat unavailable; retry deferred", flush=True)
            delay = min(delay * 2, 60)
            if args.once:
                return 1
        time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(main())
