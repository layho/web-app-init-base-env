"""SQLite backup for a trusted read snapshot.

The diagnostics package is not allowed to import sqlite3. Callers pass paths
they have already checked. This module does not decide that a missing row
means an operation did not run.
"""

from __future__ import annotations

import sqlite3


def backup_consistent(source_uri: str, destination: str) -> None:
    src = sqlite3.connect(source_uri, uri=True)
    try:
        src.execute("PRAGMA query_only=ON")
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
            checked = dst.execute("PRAGMA integrity_check").fetchone()
            if checked is None or checked[0] != "ok":
                raise sqlite3.DatabaseError("sqlite snapshot is not consistent")
        finally:
            dst.close()
    finally:
        src.close()
