"""SQLite event log. Transactions commit only after the caller says so."""

from contextlib import contextmanager
import sqlite3
from pathlib import Path
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    stream_id TEXT NOT NULL,
    stream_epoch INTEGER NOT NULL,
    producer_seq INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0,
    hold_reason TEXT,
    epoch_closed INTEGER NOT NULL DEFAULT 0,
    UNIQUE (stream_id, stream_epoch, producer_seq)
);
CREATE TABLE IF NOT EXISTS idempotency (
    principal_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY (principal_id, scope, operation, idempotency_key)
);
CREATE TABLE IF NOT EXISTS facts (
    object_key TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    event_class TEXT NOT NULL,
    authoritative INTEGER NOT NULL,
    producer_id TEXT NOT NULL,
    owner_producer TEXT NOT NULL,
    successor_producer TEXT,
    event_id TEXT NOT NULL,
    state TEXT,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_history (
    object_key TEXT NOT NULL,
    revision INTEGER NOT NULL,
    authoritative INTEGER NOT NULL,
    producer_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    state TEXT,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rounds (
    task_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT,
    stage_times_json TEXT NOT NULL,
    blocked INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    PRIMARY KEY (task_id, candidate_id, run_id)
);
CREATE TABLE IF NOT EXISTS cursors (
    principal_id TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    stream_epoch INTEGER NOT NULL,
    after_seq INTEGER NOT NULL,
    PRIMARY KEY (principal_id, stream_id, stream_epoch)
);
CREATE TABLE IF NOT EXISTS seals (
    stream_id TEXT NOT NULL,
    stream_epoch INTEGER NOT NULL,
    PRIMARY KEY (stream_id, stream_epoch)
);
CREATE TABLE IF NOT EXISTS deliveries (
    event_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    state TEXT NOT NULL,
    detail TEXT,
    updated_at TEXT NOT NULL,
    pending_since TEXT NOT NULL,
    run_id TEXT,
    claim_token TEXT,
    claim_pid INTEGER,
    claim_expires_at INTEGER,
    PRIMARY KEY (event_id, channel)
);
CREATE TABLE IF NOT EXISTS reports (
    run_id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    channels_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT
);
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    run_id TEXT,
    event_id TEXT,
    state TEXT NOT NULL,
    body_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS authorizations (
    task_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    revision INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    PRIMARY KEY (task_id, candidate_id, operation)
);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS consumed_nonces (
    slot TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    operation TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_outbox (
    event_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    stream_epoch INTEGER NOT NULL,
    producer_seq INTEGER NOT NULL,
    state TEXT NOT NULL,
    detail TEXT,
    updated_at TEXT NOT NULL,
    pending_since TEXT NOT NULL,
    claim_token TEXT,
    claim_pid INTEGER,
    claim_expires_at INTEGER,
    payload_bytes INTEGER NOT NULL,
    PRIMARY KEY (event_id, channel)
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(reports)")}
            if columns and "payload_json" not in columns:
                conn.execute("ALTER TABLE reports ADD COLUMN payload_json TEXT")
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def transaction(self):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()

    def count(self, table: str) -> int:
        if table not in {"events", "idempotency", "authorizations", "facts", "deliveries", "receipts", "consumed_nonces", "event_outbox", "reports"}:
            raise ValueError("unknown table")
        with self.transaction() as conn:
            return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
