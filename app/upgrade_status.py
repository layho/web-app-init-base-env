"""Read-only, per-application summary of the Mac mini foundation upgrade scheduler.

No REST calls, token reads, arbitrary paths, service mutations or other app data.
The scheduler writes private 0600 JSON under a user-owned 0700 directory.
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import stat

SCHED_ROOT = Path.home() / ".local/share/macmini-foundation-upgrades"
SHA = re.compile(r"[0-9a-f]{40}\Z")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
STATES = {"current", "blocked", "closed_pr", "waiting_adoption", "no_main", "upgrade_pending",
          "draft_created", "error", "upgrade_merged", "upgrade_waiting_verification", "upgrade_review_required"}
MAX_AGE = 15 * 60


def private_json(path, default):
    # Check parent ownership before opening any potentially sensitive file.
    try:
        parent = SCHED_ROOT.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or parent.st_mode & 0o077):
            return default
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > 32768):
                return default
            return json.load(stream)
    except (OSError, ValueError, UnicodeError):
        return default


def safe_sha(value):
    return value if isinstance(value, str) and SHA.fullmatch(value) else None


def checked_time(value):
    if not isinstance(value, str):
        return None, True
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("timezone missing")
        seconds = (datetime.now(timezone.utc) - dt).total_seconds()
        if not -300 <= seconds <= MAX_AGE:
            return value, True
        return value, False
    except ValueError:
        return None, True


def snapshot(app_root):
    result = {"registered": False, "mode": "unregistered", "state": "unavailable",
              "checked_at": None, "stale": True, "pr_url": None,
              "target_sha": None, "app_sha": None, "review_count": None, "blocked_files": []}
    try:
        meta = json.loads((app_root / ".app-init.json").read_text(encoding="utf-8"))
        repo = meta["repository"]
        if not isinstance(repo, str) or not REPO.fullmatch(repo):
            return result
    except (OSError, ValueError, TypeError, KeyError):
        return result
    reg = private_json(SCHED_ROOT / "registry.json", None)
    if not isinstance(reg, dict) or reg.get("schema") != 1 or not isinstance(reg.get("apps"), dict):
        return result
    entry = reg["apps"].get(repo)
    if not isinstance(entry, dict) or entry.get("mode") not in {"create", "observe"}:
        return result
    mode = entry["mode"]
    result.update(registered=True, mode=mode, state="awaiting_first_check")
    digest = hashlib.sha256(repo.encode()).hexdigest()
    status = private_json(SCHED_ROOT / ("status-" + digest + ".json"), None)
    if not isinstance(status, dict) or status.get("schema") != 1 or status.get("repository") != repo:
        return result
    if status.get("mode") != mode or status.get("state") not in STATES:
        return result
    checked, stale = checked_time(status.get("checked_at"))
    raw_url = status.get("pr_url")
    if (isinstance(raw_url, str) and not re.fullmatch(
            r"https://github\.com/" + re.escape(repo) + r"/pull/[1-9]\d*", raw_url)):
        raw_url = None
    review = status.get("review_count")
    files = []
    for entry in status.get("blocked_files", []) if isinstance(status.get("blocked_files"), list) else []:
        if not isinstance(entry, dict) or len(files) >= 100:
            break
        path, action = entry.get("path"), entry.get("action")
        if (isinstance(path, str) and len(path) <= 180 and not path.startswith("/")
                and ".." not in Path(path).parts and action in {"conflict", "review"}):
            files.append({"path": path, "action": action})
    result.update(state=status["state"], checked_at=checked, stale=stale,
                  pr_url=raw_url, target_sha=safe_sha(status.get("target_sha")),
                  app_sha=safe_sha(status.get("app_sha")), blocked_files=files if status["state"] == "blocked" else [],
                  review_count=review if type(review) is int and 0 <= review <= 1000 else None)
    return result
