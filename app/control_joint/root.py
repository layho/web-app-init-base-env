"""Server-side control root checks.

A missing root is not configured. A root that fails these checks is blocked
and is not read. The request body cannot choose the directory.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def inspect_root(raw: str | None = None) -> dict:
    if raw is None:
        raw = os.environ.get("V060_CONTROL_ROOT", "")
    if not isinstance(raw, str) or raw.strip() == "":
        return {
            "ok": False,
            "state": "not_configured",
            "reason": "No control root is configured. This read does not load demo rows, write a journal, or touch a host.",
        }
    if raw != raw.strip() or "\x00" in raw or not raw.startswith("/"):
        return _blocked("The control root must be an absolute path.")
    path = Path(raw)
    if path != path.resolve() or any(part in {"", ".", ".."} for part in path.parts):
        return _blocked("The control root must be a resolved directory.")
    try:
        info = path.lstat()
    except OSError:
        return _blocked("The control root is not a directory this process can stat.")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return _blocked("The control root must be a real directory.")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        return _blocked("The control root must be a private directory owned by this user.")
    if path == Path("/") or path == Path(tempfile.gettempdir()) or path == _REPO or _REPO in path.parents:
        return _blocked("The control root is too broad or points at this repository.")
    database = os.environ.get("DATABASE_URL", "")
    if database.startswith("sqlite:///") and path.as_posix() in database:
        return _blocked("The control root overlaps a configured database URL.")
    return {"ok": True, "path": path, "state": "ready", "reason": None}


def temporary_root(raw: str | None = None) -> dict:
    """Writable adapter capability. Default deny stays outside this function."""

    checked = inspect_root(raw)
    if not checked["ok"]:
        return checked
    path = checked["path"]
    prefixes = [Path(tempfile.gettempdir()).resolve()]
    extra = os.environ.get("TMPDIR", "")
    if extra.startswith("/"):
        prefixes.append(Path(extra).resolve())
    prefixes.extend([Path("/tmp"), Path("/private/tmp"), Path("/var/folders")])
    if not any(path == prefix or prefix in path.parents for prefix in prefixes if prefix.is_absolute()):
        return _blocked("An isolated adapter only accepts a temporary directory.")
    return checked


def _blocked(reason: str) -> dict:
    return {"ok": False, "state": "blocked", "reason": reason}
