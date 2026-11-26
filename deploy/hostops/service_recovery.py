"""Local operator-authorized recovery of existing, same-user launchd jobs.

This is not a remote broker. Never stops a running job, changes launchd
configuration, installs a service, or triggers merge/publish coordinators.
"""
import argparse
import fcntl
import json
import os
import platform
import stat
import sqlite3
from contextlib import closing
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from diagnostics import SERVICES, services

RECOVERABLE = frozenset({"macci.runner", "macci.receipts"})


@contextmanager
def recovery_lock(root):
    root.mkdir(mode=0o700,exist_ok=True)
    info=root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
        raise ValueError("unsafe recovery state directory")
    fd=os.open(root/"recovery.lock",os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600:
            raise ValueError("unsafe recovery lock")
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("recovery already active") from None
        yield
    finally:
        os.close(fd)


def observe(service_id, inventory):
    rows=[row for row in inventory() if row.get("service_id")==service_id]
    if len(rows)!=1:
        return "unknown"
    return rows[0].get("state","unknown")


def launch(target):
    # No -k: never terminate a running process to trigger another run.
    result=subprocess.run(["/bin/launchctl","kickstart",target],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        env={"PATH":"/usr/bin:/bin","HOME":str(Path.home())},
        timeout=10,check=False)
    return result.returncode


def reserve_recovery(root,service_id,now):
    """Persist an attempt before launching. A crash cannot reset its cooldown."""
    path=root/"attempts.sqlite3"
    fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600:
            raise ValueError("unsafe recovery ledger")
    finally:
        os.close(fd)
    with closing(sqlite3.connect(path,timeout=3)) as db,db:
        db.execute("CREATE TABLE IF NOT EXISTS attempts(service_id TEXT PRIMARY KEY, attempted_at INTEGER NOT NULL)")
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT attempted_at FROM attempts WHERE service_id=?",(service_id,)).fetchone()
        if row and now-row[0]<300:
            return False
        db.execute("INSERT INTO attempts VALUES (?,?) ON CONFLICT(service_id) DO UPDATE SET attempted_at=excluded.attempted_at",(service_id,now))
        return True


def recover(service_id, *, execute=False, root=None, inventory=services, runner=launch,
            system=platform.system, uid=os.getuid, sleeper=time.sleep, clock=time.time):
    if service_id not in RECOVERABLE:
        raise ValueError("service is not approved for recovery")
    if system()!="Darwin" or uid()==0:
        raise ValueError("same-user non-root macOS login required")
    if execute and root is None:
        raise ValueError("private recovery state required")
    def operation():
        before=observe(service_id,inventory)
        result={"service_id":service_id,"before":before,"after":before,
                "action":"none","state":"not_eligible"}
        if before!="last_exit_failed":
            return result
        if not execute:
            return {**result,"state":"eligible"}
        target="gui/"+str(uid())+"/"+SERVICES[service_id]
        # Recheck immediately before the fixed launchctl action.
        if observe(service_id,inventory)!="last_exit_failed":
            return {**result,"state":"state_changed"}
        if not reserve_recovery(root,service_id,int(clock())):
            return {**result,"state":"cooldown"}
        try:
            code=runner(target)
        except (OSError,subprocess.SubprocessError):
            return {**result,"action":"kickstart","state":"outcome_unknown","after":"unknown"}
        if code!=0:
            return {**result,"action":"kickstart","state":"rejected",
                    "after":observe(service_id,inventory)}
        for _ in range(3):
            after=observe(service_id,inventory)
            if after=="running":
                return {**result,"action":"kickstart","state":"running_observed","after":after}
            if after=="loaded_idle":
                return {**result,"action":"kickstart","state":"idle_observed","after":after}
            sleeper(1)
        return {**result,"action":"kickstart","state":"outcome_unknown","after":after}
    if not execute:
        return operation()
    with recovery_lock(root):
        return operation()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service",choices=sorted(RECOVERABLE))
    parser.add_argument("--execute",action="store_true",help="Explicitly authorize one local recovery attempt")
    args=parser.parse_args()
    try:
        result=recover(args.service,execute=args.execute,root=Path.home()/".hostops-recovery")
    except (OSError,ValueError,sqlite3.Error):
        print(json.dumps({"state":"refused"}))
        return 1
    print(json.dumps(result))
    return 0 if result["state"] in ("eligible","not_eligible","running_observed","idle_observed","state_changed","cooldown") else 1


if __name__=="__main__":
    raise SystemExit(main())
