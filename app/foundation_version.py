"""Read-only provenance and official foundation version status."""
import base64
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

REPOSITORY = "layho/web-app-init-base-env"
ROOT = Path(__file__).resolve().parents[1]
API = f"https://api.github.com/repos/{REPOSITORY}"
_cache = {"latest": None, "checked_at": None, "stale": True, "expires": 0.0}
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
    request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "web-app-foundation-version"})
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read(1024 * 1024))


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


def latest_status():
    with _lock:
        if time.monotonic() >= _cache["expires"]:
            try:
                latest = fetch_latest()
            except (OSError, ValueError, KeyError, TypeError, IndexError):
                _cache.update(stale=True, expires=time.monotonic() + 30)
            else:
                _cache.update(latest=latest, checked_at=datetime.now(timezone.utc).isoformat(),
                              stale=False, expires=time.monotonic() + 300)
        return {k: _cache[k] for k in ("latest", "checked_at", "stale")}


def snapshot():
    root_role = os.getenv("APP_BASE_PREVIEW") == "1"
    current = registered_version()
    if root_role:
        try:
            version = json.loads((ROOT / "app/release_notes.json").read_text(encoding="utf-8"))[0]["version"]
            current = version_info(version, os.getenv("APP_RUNNING_SHA", ""))
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            current = None
    remote = latest_status() if current is not None or root_role else {
        "latest": None, "checked_at": None, "stale": True}
    latest = remote["latest"]
    status = ("unregistered" if current is None else "unknown" if remote["stale"] or latest is None
              else "current" if current == latest else "different")
    return {"repository": REPOSITORY, "role": "foundation" if root_role else "application",
            "current": current, **remote, "status": status, "auto_upgrade_enabled": False}
