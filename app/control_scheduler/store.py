"""Private scheduler ledger.

The ledger stores queue, chain, version, and evidence projections. It does
not store lease tokens, event payloads as an outbox, or credential material.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    version TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    baseline TEXT NOT NULL,
    freeze_reason TEXT,
    freeze_evidence TEXT,
    acceptance_ref TEXT,
    publish_ref TEXT,
    published_tree TEXT,
    main_sha TEXT,
    preview_sha TEXT,
    created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    app_id TEXT NOT NULL,
    worktree TEXT NOT NULL,
    acceptance_ref TEXT,
    acceptance_signer TEXT,
    closed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chains (
    chain_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    version TEXT NOT NULL,
    pr_line TEXT NOT NULL,
    worktree TEXT NOT NULL,
    files_json TEXT NOT NULL,
    dependencies_json TEXT NOT NULL,
    priority INTEGER NOT NULL,
    created INTEGER NOT NULL,
    state TEXT NOT NULL,
    queue TEXT NOT NULL,
    candidate_id TEXT NOT NULL DEFAULT '',
    head TEXT NOT NULL DEFAULT '',
    base TEXT NOT NULL DEFAULT '',
    merge_sha TEXT NOT NULL DEFAULT '',
    tree TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    remote_status TEXT NOT NULL DEFAULT 'NONE',
    remote_seq INTEGER NOT NULL DEFAULT 0,
    superseded_by TEXT,
    spec_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rounds (
    round_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    chain_id TEXT,
    task_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    candidate_id TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL,
    body_json TEXT NOT NULL,
    dispatch TEXT NOT NULL,
    run_id TEXT,
    attempt INTEGER,
    generation INTEGER,
    revision INTEGER,
    auth_state TEXT,
    evidence_ref TEXT,
    created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS watermarks (
    stream_id TEXT NOT NULL,
    stream_epoch INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY (stream_id, stream_epoch)
);
CREATE TABLE IF NOT EXISTS applied_events (
    idempotency_key TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS observations (
    chain_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    applied INTEGER NOT NULL,
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (chain_id, seq)
);
"""


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "scheduler.sqlite3"
        self._prepare()
        with self.transaction() as db:
            db.executescript(SCHEMA)

    def _prepare(self) -> None:
        root = self.root
        if not root.is_absolute():
            raise ValueError("absolute scheduler directory required")
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("private scheduler directory required")
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            file_info = os.fstat(fd)
            if not stat.S_ISREG(file_info.st_mode) or file_info.st_uid != os.getuid():
                raise ValueError("private scheduler file required")
            if stat.S_IMODE(file_info.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def transaction(self):
        with contextlib.closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
