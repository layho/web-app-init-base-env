"""Read-only provenance and official foundation version status."""
import base64
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import upgrade_status

REPOSITORY = "layho/web-app-init-base-env"
ROOT = Path(__file__).resolve().parents[1]
API = f"https://api.github.com/repos/{REPOSITORY}"
GIT_URL = f"https://github.com/{REPOSITORY}.git"
RAW = f"https://raw.githubusercontent.com/{REPOSITORY}"
_cache = {"latest": None, "checked_at": None, "stale": True, "expires": 0.0,
          "attempted_at": None, "source": None, "error": None, "last_attempt": None}
_lock = threading.Lock()


def version_info(version, sha):
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version):
        return None
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        return None
    return {"version": version, "sha": sha}


def registered_version():
    try:
        metadata = json.loads((ROOT / ".app-init.json").read_text(encoding="utf-8"))
        foundation = metadata["foundation"]
        if foundation["schema"] != 1 or foundation["repository"] != REPOSITORY:
            return None
        return version_info(foundation["version"], foundation["sha"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def get_json(url):
    request = Request(url, headers={"Accept": "application/vnd.github+json",
                                   "User-Agent": "web-app-foundation-version",
                                   "Cache-Control": "no-cache"})
    with urlopen(request, timeout=5) as response:
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Oversized release metadata")
        return json.loads(raw)


def fetch_latest():
    sha = get_json(f"{API}/branches/main")["commit"]["sha"]
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Invalid main commit")
    content = get_json(f"{API}/contents/app/release_notes.json?ref={sha}")["content"]
    version = json.loads(base64.b64decode(content))[0]["version"]
    result = version_info(version, sha)
    if result is None:
        raise ValueError("Invalid official version")
    return result


def fetch_latest_via_git():
    # Independent of the unauthenticated REST rate limit. Never pull the
    # application checkout, use private credentials, or accept preview.
    result = subprocess.run(
        ["git", "-c", "credential.helper=", "ls-remote", "--refs", GIT_URL, "refs/heads/main"],
        cwd=ROOT, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        capture_output=True, text=True, timeout=6, check=True,
    )
    fields = result.stdout.strip().split()
    if len(fields) != 2 or fields[1] != "refs/heads/main" or not re.fullmatch(r"[0-9a-f]{40}", fields[0]):
        raise ValueError("Invalid Git main reference")
    sha = fields[0]
    notes = get_json(f"{RAW}/{sha}/app/release_notes.json")
    version = version_info(notes[0]["version"], sha)
    if version is None:
        raise ValueError("Invalid official version")
    return version


def failure_code(exc):
    # Only allowlisted diagnostics reach the browser; no raw URL, token,
    # subprocess output, or proxy configuration.
    if isinstance(exc, HTTPError):
        if exc.code in (403, 429):
            return "api_access_or_rate_limit"
        return "http_error"
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return "timeout"
    if isinstance(exc, URLError):
        return "network_error"
    if isinstance(exc, subprocess.SubprocessError):
        return "git_error"
    if isinstance(exc, OSError):
        return "network_error"
    return "invalid_response"


FAILURES = (OSError, ValueError, KeyError, TypeError, IndexError, subprocess.SubprocessError)


def latest_status(force=False):
    with _lock:
        now = time.monotonic()
        last = _cache.get("last_attempt")
        # Manual refresh bypasses five-minute cache, with a short anti-stampede limit.
        if now >= _cache["expires"] or (force and (last is None or now - last >= 10)):
            _cache.update(last_attempt=now, attempted_at=datetime.now(timezone.utc).isoformat())
            try:
                latest = fetch_latest()
                source = "github_api"
            except FAILURES as primary:
                try:
                    latest = fetch_latest_via_git()
                    source = "git_raw"
                except FAILURES as secondary:
                    _cache.update(stale=True, expires=time.monotonic() + 30,
                                  error=failure_code(primary) + "/" + failure_code(secondary))
                    latest = None
            if latest is not None:
                _cache.update(latest=latest, checked_at=datetime.now(timezone.utc).isoformat(),
                              source=source, error=None, stale=False, expires=time.monotonic() + 300)
        return {k: _cache.get(k) for k in ("latest", "checked_at", "stale", "attempted_at", "source", "error")}


def snapshot(force=False):
    root_role = os.getenv("APP_BASE_PREVIEW") == "1"
    current = registered_version()
    if root_role:
        try:
            version = json.loads((ROOT / "app/release_notes.json").read_text(encoding="utf-8"))[0]["version"]
            current = version_info(version, os.getenv("APP_RUNNING_SHA", ""))
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            current = None
    remote = (latest_status(force=True) if force else latest_status()) if current is not None or root_role else {
        "latest": None, "checked_at": None, "stale": True}
    latest = remote["latest"]
    status = ("unregistered" if current is None else "unknown" if remote["stale"] or latest is None
              else "current" if current == latest else "different")
    scheduler = None if root_role else upgrade_status.snapshot(ROOT)
    return {"repository": REPOSITORY, "role": "foundation" if root_role else "application",
            "current": current, **remote, "status": status,
            "scheduler": scheduler,
            "auto_upgrade_enabled": bool(scheduler and scheduler["mode"] == "create" and not scheduler["stale"])}
