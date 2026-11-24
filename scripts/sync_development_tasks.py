"""Refresh the development task snapshot independently of feedback writes."""
from datetime import datetime, timezone
import json
import os
from tempfile import NamedTemporaryFile
from urllib.error import URLError
from urllib.parse import urlencode

from app import development_tasks, pr_tracking
from scripts.sync_feedback import REPO, github


def sync() -> None:
    cache = development_tasks.CACHE_PATH
    now = datetime.now(timezone.utc)
    previous = development_tasks.snapshot()
    if previous["synced_at"] and now - datetime.fromisoformat(previous["synced_at"]) < development_tasks.REFRESH_INTERVAL:
        return

    tasks = []
    page = 1
    page_size = 100
    while True:
        query = urlencode({"state": "all", "per_page": page_size, "page": page, "sort": "updated"})
        issues = github("GET", f"/repos/{REPO}/issues?{query}")
        if not isinstance(issues, list):
            raise ValueError("Unexpected issue page")
        for issue in issues:
            if "pull_request" in issue:
                continue
            # GitHub user/login is shared by multiple AI workers. Structured
            # APP_PROGRESS comments provide an attributable agent claim.
            # Do not fabricate ownership from the GitHub account alone.
            comments = []
            if type(issue.get("comments")) is int and issue["comments"] > 0:
                comment_page = 1
                while True:
                    batch = github("GET", f"/repos/{REPO}/issues/{issue['number']}/comments?per_page=100&page={comment_page}")
                    if not isinstance(batch, list):
                        raise ValueError("Unexpected Issue comments")
                    comments.extend(batch)
                    if len(batch) < 100:
                        break
                    comment_page += 1
            task = development_tasks.parse_issue(issue, REPO, comments)
            if task:
                tasks.append(task)
        if len(issues) < page_size:
            break
        page += 1

    cache.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache.parent, delete=False) as temp:
        try:
            json.dump({"synced_at": datetime.now(timezone.utc).isoformat(), "tasks": tasks}, temp, ensure_ascii=False)
            temp.close()
            os.replace(temp.name, cache)
        finally:
            if os.path.exists(temp.name):
                os.unlink(temp.name)


if __name__ == "__main__":
    failed = False
    jobs = [sync]
    if pr_tracking.enabled():
        jobs.append(lambda: pr_tracking.sync(github))
    for job in jobs:
        try:
            job()
        except (OSError, URLError, ValueError, TypeError, KeyError):
            print("Task or PR tracking sync failed; last successful snapshot retained")
            failed = True
    raise SystemExit(int(failed))
