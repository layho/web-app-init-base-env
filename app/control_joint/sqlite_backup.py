"""SQLite backup for a trusted read snapshot.

The diagnostics package is not allowed to import sqlite3. Callers pass paths
they have already checked, plus a page budget, a byte budget, and a
deadline. A timeout, a budget hit, or a torn snapshot raises. This module
does not decide that a missing row means an operation did not run.
"""

from __future__ import annotations

import sqlite3
import time


class SnapshotBudget(Exception):
    """The snapshot exceeded the page or byte budget."""


class SnapshotTimeout(Exception):
    """The snapshot exceeded its total time limit."""


def backup_consistent(
    source_uri: str,
    destination: str,
    *,
    max_pages: int,
    max_bytes: int,
    deadline: float,
) -> None:
    if time.monotonic() >= deadline:
        raise SnapshotTimeout("sqlite snapshot timed out")
    src = sqlite3.connect(source_uri, uri=True)
    try:
        src.execute("PRAGMA query_only=ON")
        page_count = src.execute("PRAGMA page_count").fetchone()
        page_size = src.execute("PRAGMA page_size").fetchone()
        pages = 0 if page_count is None else int(page_count[0])
        size = 0 if page_size is None else int(page_size[0])
        if pages > max_pages or pages * size > max_bytes:
            raise SnapshotBudget("sqlite snapshot exceeds the page or byte budget")
        dst = sqlite3.connect(destination)
        try:
            copied = 0

            def progress(status, remaining, total):
                del status
                nonlocal copied
                if time.monotonic() >= deadline:
                    raise SnapshotTimeout("sqlite snapshot timed out")
                copied = int(total) - int(remaining)
                if copied > max_pages or int(total) > max_pages:
                    raise SnapshotBudget("sqlite snapshot exceeds the page budget")

            src.backup(dst, pages=max(1, min(32, max_pages)), progress=progress, sleep=0)
            if time.monotonic() >= deadline:
                raise SnapshotTimeout("sqlite snapshot timed out")
            checked = dst.execute("PRAGMA integrity_check").fetchone()
            if checked is None or checked[0] != "ok":
                raise sqlite3.DatabaseError("sqlite snapshot is not consistent")
        finally:
            dst.close()
    finally:
        src.close()
