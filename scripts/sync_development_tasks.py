"""Refresh the development task snapshot independently of feedback writes."""
from datetime import datetime, timezone, timedelta
import fcntl
import time
import json
import os
from tempfile import NamedTemporaryFile
from urllib.error import URLError
from urllib.parse import urlencode

from app import development_tasks, pr_tracking
from scripts.sync_feedback import REPO, github


def sync(*, force=False, repository=None, request=None, incremental=False) -> None:
    repository = repository or REPO
    request = request or github
    cache = development_tasks.CACHE_PATH
    now = datetime.now(timezone.utc)
    previous = development_tasks.snapshot()
    if not force and previous["synced_at"] and now - datetime.fromisoformat(previous["synced_at"]) < development_tasks.REFRESH_INTERVAL:
        return

    cached = {t["number"]: t for t in previous["tasks"]} if incremental and previous.get("repository") == repository else {}
    tasks = []
    page = 1
    page_size = 100
    while True:
        query = urlencode({"state": "all", "per_page": page_size, "page": page, "sort": "created", "direction": "asc"})
        issues = request("GET", f"/repos/{repository}/issues?{query}")
        if not isinstance(issues, list):
            raise ValueError("Unexpected issue page")
        for issue in issues:
            if "pull_request" in issue:
                continue
            # GitHub user/login is shared by multiple AI workers. Structured
            # APP_PROGRESS comments provide an attributable agent claim.
            # Do not fabricate ownership from the GitHub account alone.
            old = cached.get(issue["number"])
            if (old and issue.get("updated_at") and old.get("updated_at") == issue["updated_at"]
                    and old.get("comment_count") == issue.get("comments")):
                tasks.append(old)
                continue
            comments = []
            if type(issue.get("comments")) is int and issue["comments"] > 0:
                comment_page = 1
                while True:
                    batch = request("GET", f"/repos/{repository}/issues/{issue['number']}/comments?per_page=100&page={comment_page}")
                    if not isinstance(batch, list):
                        raise ValueError("Unexpected Issue comments")
                    comments.extend(batch)
                    if len(batch) < 100:
                        break
                    comment_page += 1
            task = development_tasks.parse_issue(issue, repository, comments)
            if task:
                task['comment_count'] = issue.get('comments')
                tasks.append(task)
        if len(issues) < page_size:
            break
        page += 1

    cache.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache.parent, delete=False) as temp:
        try:
            json.dump({"synced_at": datetime.now(timezone.utc).isoformat(), "repository": repository, "tasks": tasks}, temp, ensure_ascii=False)
            temp.close()
            os.replace(temp.name, cache)
        finally:
            if os.path.exists(temp.name):
                os.unlink(temp.name)



# Both the scheduled worker and the manually spawned worker use this lock.
# Only local status is exposed to the browser; no credential or raw API body.
def refresh_status():
    try:
        data = json.loads((development_tasks.CACHE_PATH.parent / "task-sync-status.json").read_text())
        if not isinstance(data, dict):
            return {"state": "unknown"}
        if data.get("state") in {"queued", "running"}:
            stamp = datetime.fromisoformat(data["updated_at"])
            if datetime.now(timezone.utc) - stamp > timedelta(minutes=15):
                return {**data, "state": "interrupted"}
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return {"state": "unknown"}


def write_status(data):
    folder = development_tasks.CACHE_PATH.parent
    folder.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, delete=False) as out:
        try:
            json.dump({**data, "updated_at": datetime.now(timezone.utc).isoformat()}, out)
            out.close()
            os.replace(out.name, folder / "task-sync-status.json")
        finally:
            if os.path.exists(out.name):
                os.unlink(out.name)


def run_refresh(force=False):
    from app import release_acceptance
    folder = development_tasks.CACHE_PATH.parent
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "task-sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        target = pr_tracking.target()
        previous = development_tasks.snapshot()
        tracking = pr_tracking.snapshot() if target else {}
        if not force and not previous.get("stale") and not tracking.get("stale", True):
            if all(datetime.now(timezone.utc) - datetime.fromisoformat(x["synced_at"]) < development_tasks.REFRESH_INTERVAL for x in (previous, tracking)):
                return 0
        start = time.monotonic()
        state = {"state": "running", "phase": "issues", "requests": 0,
                 "started_at": datetime.now(timezone.utc).isoformat()}
        write_status(state)
        def request(method, path):
            if method != "GET":
                raise ValueError("Read-only task sync")
            state["requests"] += 1
            write_status({**state, "elapsed_seconds": round(time.monotonic() - start, 1)})
            return release_acceptance.github(path)
        try:
            if target is None:
                raise ValueError("No verified repository")
            sync(force=True, repository=target[0], request=request, incremental=not force)
            state["phase"] = "pulls"
            write_status(state)
            pr_tracking.sync(request, force=True)
            state["state"] = "success"
        except (release_acceptance.AcceptanceError, OSError, ValueError, TypeError, KeyError) as exc:
            state["state"] = "failed"
            # AcceptanceError messages are already sanitized by the REST client.
            state["reason"] = str(exc) if isinstance(exc, release_acceptance.AcceptanceError) else type(exc).__name__
        write_status({**state, "elapsed_seconds": round(time.monotonic() - start, 1)})
        return int(state["state"] != "success")


if __name__ == "__main__":
    import sys
    raise SystemExit(run_refresh(force="--force" in sys.argv[1:]))
