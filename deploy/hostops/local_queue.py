"""Offline read-only job ledger. No network transport or authorization authority."""
import json
import sqlite3
from contextlib import closing

from protocol import envelope_digest, validate, run_local_fixture


class LocalQueue:
    """Use a caller-selected private local database; never accepts remote paths."""

    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                task_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                state TEXT NOT NULL, result TEXT)""")

    def run_fixture(self, job, host_id, now):
        validate(job, host_id, now)
        digest = envelope_digest(job)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT digest,state,result FROM jobs WHERE task_id=?",
                             (job["task_id"],)).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError("task identity conflict")
                if row[1] != "success":
                    raise ValueError("task outcome unknown")
                return json.loads(row[2])
            db.execute("INSERT INTO jobs VALUES (?,?,?,NULL)",
                       (job["task_id"], digest, "running"))
        # A crash leaves a durable unknown outcome, not a fabricated success.
        result = run_local_fixture(job, host_id, now)
        encoded = json.dumps(result, sort_keys=True, ensure_ascii=True)
        if len(encoded) > 262144:
            raise ValueError("result exceeds bound")
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            cursor = db.execute(
                "UPDATE jobs SET state='success',result=? WHERE task_id=? AND digest=? AND state='running'",
                (encoded, job["task_id"], digest))
            if cursor.rowcount != 1:
                raise ValueError("job state changed")
        return result
