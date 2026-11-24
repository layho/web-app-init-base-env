"""Refresh the development task snapshot independently of feedback writes."""
from datetime import datetime, timezone, timedelta
import fcntl
import time
import subprocess
import sys
import re
import json
import os
from tempfile import NamedTemporaryFile
from urllib.error import URLError
from urllib.parse import urlencode

from app import development_tasks, pr_tracking, release_acceptance
from scripts.sync_feedback import REPO, github


def sync(*, force=False, repository=None, request=None, incremental=False, collect_only=False):
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
                    and old.get("comment_count") == issue.get("comments")
                    and (not issue.get("title","").startswith("[BASE-") or "release_evidence" in old)):
                tasks.append(old)
                continue
            task = fetch_task(issue, repository, request)
            if task:
                tasks.append(task)
        if len(issues) < page_size:
            break
        page += 1

    data = {"synced_at": datetime.now(timezone.utc).isoformat(), "repository": repository, "tasks": tasks}
    if collect_only:
        return data
    write_json(cache, data)


def fetch_task(issue, repository, request):
    comments = []
    if type(issue.get("comments")) is int and issue["comments"] > 0:
        page = 1
        seen = set()
        while True:
            batch = request("GET", f"/repos/{repository}/issues/{issue['number']}/comments?per_page=100&page={page}")
            if not isinstance(batch, list):
                raise ValueError("Unexpected Issue comments")
            ids = {x["id"] for x in batch}
            if len(batch) == 100 and not (ids - seen):
                raise ValueError("Issue comment pagination did not advance")
            seen.update(ids)
            comments.extend(batch)
            if len(batch) < 100:
                break
            page += 1
    task = development_tasks.parse_issue(issue, repository, comments)
    if task and repository == release_acceptance.REPO:
        task["release_evidence"] = release_acceptance.collect_release_evidence(issue, comments)
    if task:
        task["comment_count"] = issue.get("comments")
    return task


def write_json(cache, data):
    cache.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache.parent, delete=False) as temp:
        try:
            json.dump(data, temp, ensure_ascii=False)
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


def enqueue_refresh():
    folder = development_tasks.CACHE_PATH.parent
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "task-sync-request.lock").open("a") as gate:
        fcntl.flock(gate, fcntl.LOCK_EX)
        with (folder / "task-sync.lock").open("a") as worker:
            try:
                fcntl.flock(worker, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return refresh_status()
            if refresh_status().get("state") == "queued":
                return refresh_status()
            write_status({"state": "queued", "requests": 0})
            try:
                with (folder / "task-sync-worker.log").open("ab") as log:
                    subprocess.Popen([sys.executable, "-m", "scripts.sync_development_tasks", "--force"],
                                     cwd=str(development_tasks.CACHE_PATH.parent.parent),
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True)
            except OSError:
                write_status({"state": "failed", "reason": "WorkerStartFailed"})
            return refresh_status()


def token_health(repository, request):
    """Read-only checks using the actual server credential; no secret is returned."""
    from app import release_acceptance
    result = {}
    def check(label, call, extract):
        started = time.monotonic()
        try:
            obj = call()
            result[label] = {"state": "ok", **extract(obj)}
        except (release_acceptance.AcceptanceError, OSError, ValueError, KeyError, TypeError) as exc:
            result[label] = {"state": "failed", "reason": str(exc) if isinstance(exc, release_acceptance.AcceptanceError) else type(exc).__name__}
        result[label]["elapsed_seconds"] = round(time.monotonic() - started, 2)
    check("identity", lambda: request("GET", "/user"), lambda o: {"login": o["login"]})
    check("rate_limit", lambda: request("GET", "/rate_limit"),
          lambda o: {"remaining": o["resources"]["core"]["remaining"], "limit": o["resources"]["core"]["limit"]})
    check("repository", lambda: request("GET", "/repos/" + repository),
          lambda o: {"permissions": {k: v for k, v in o.get("permissions", {}).items()
                                     if k in {"admin", "maintain", "push", "triage", "pull"} and type(v) is bool}})
    owner, name = repository.split("/")
    # GraphQL POST carries a fixed read query, never a mutation.
    query = 'query($owner:String!,$name:String!){viewer{login} repository(owner:$owner,name:$name){viewerPermission}}'
    def graphql_fields(o):
        if o.get("errors") or not o.get("data", {}).get("repository"):
            raise ValueError("GraphQL read failed")
        return {"permission": o["data"]["repository"]["viewerPermission"]}
    check("graphql", lambda: release_acceptance.github("/graphql", {"query": query, "variables": {"owner": owner, "name": name}}), graphql_fields)
    return result


def run_refresh(force=False):
    from app import release_acceptance
    folder = development_tasks.CACHE_PATH.parent
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "task-sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX if force else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        force = force or refresh_status().get("state") == "queued"
        target = pr_tracking.target()
        previous = development_tasks.snapshot()
        tracking = pr_tracking.snapshot() if target else {}
        if not force and not previous.get("stale") and not tracking.get("stale", True):
            if all(datetime.now(timezone.utc) - datetime.fromisoformat(x["synced_at"]) < development_tasks.REFRESH_INTERVAL for x in (previous, tracking)):
                return 0
        start = time.monotonic()
        state = {"state": "running", "phase": "issues", "requests": 0,
                 "token_health": refresh_status().get("token_health", {}),
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
            if force:
                state["phase"] = "token_health"
                state["token_health"] = token_health(target[0], request)
                state["phase"] = "issues"
                write_status(state)
            issues = sync(force=True, repository=target[0], request=request,
                          incremental=not force, collect_only=True)
            state["phase"] = "pulls"
            write_status(state)
            cached = {} if force else {p["number"]: p for p in tracking.get("prs", [])}
            prs = pr_tracking.collect(request, *target, cached=cached)
            state["phase"] = "reconcile"
            write_status(state)
            known = {t["number"] for t in issues["tasks"]}
            missing = {n for p in prs for n in development_tasks.pr_linked_issues(p)} - known
            for n in sorted(missing):
                try:
                    issue = request("GET", f"/repos/{target[0]}/issues/{n}")
                except release_acceptance.AcceptanceError as exc:
                    if str(exc).startswith("GH-REST-404"):
                        continue
                    raise
                if issue.get("number") != n or "pull_request" in issue:
                    continue
                task = fetch_task(issue, target[0], request)
                if task:
                    issues["tasks"].append(task)
            stamp = datetime.now(timezone.utc).isoformat()
            issues["synced_at"] = stamp
            evidence = {}
            for task in issues["tasks"]:
                event = task.get("release_evidence")
                if event:
                    previous = evidence.get(event["candidate_sha"])
                    if previous is None or event["at"] > previous["at"]:
                        evidence[event["candidate_sha"]] = event
            pr_data = {"synced_at": stamp, "repository": target[0], "branch": target[1], "prs": prs, "release_evidence": evidence}
            # One atomic publication is authoritative for the unified page.
            write_json(development_tasks.CACHE_PATH, issues)
            write_json(pr_tracking.CACHE, pr_data)
            write_json(folder / "task-sync-combined.json", {"issues": issues, "tracking": pr_data})
            state["state"] = "success"
        except (release_acceptance.AcceptanceError, OSError, ValueError, TypeError, KeyError) as exc:
            state["state"] = "failed"
            # AcceptanceError messages are already sanitized by the REST client.
            state["reason"] = str(exc) if isinstance(exc, release_acceptance.AcceptanceError) else type(exc).__name__
        write_status({**state, "elapsed_seconds": round(time.monotonic() - start, 1)})
        return int(state["state"] != "success")


def refresh_active():
    """Fast path independent of the full-history lock; no HTTP request waits."""
    target = pr_tracking.target()
    if not target:
        return 0
    folder = development_tasks.CACHE_PATH.parent
    folder.mkdir(parents=True, exist_ok=True)
    with (folder/"pr-active.lock").open("a") as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        path=folder/"pr-active.json"
        try:
            old=json.loads(path.read_text())
        except (OSError,ValueError):
            old={}
        stamp=pr_tracking.instant(old.get("synced_at"))
        if stamp and (old.get("repository"),old.get("branch"))==target and (datetime.now(timezone.utc)-stamp).total_seconds()<25:
            return 0
        def request(method,path):
            if method!="GET":
                raise ValueError("Read only")
            return release_acceptance.github(path)
        try:
            repo,branch=target
            prefix="/repos/"+repo
            listings=[]
            seen=set()
            for page in range(1,21):
                batch=request("GET",prefix+"/pulls?state=open&sort=created&direction=desc&per_page=100&page="+str(page))
                if not isinstance(batch,list):
                    raise ValueError("Invalid open PR page")
                for row in batch:
                    if row["number"] not in seen:
                        listings.append(row)
                        seen.add(row["number"])
                if len(batch)<100:
                    break
            else:
                raise ValueError("Open PR pagination limit")
            # Resolve previously open PRs that just closed/merged, so the fast
            # overlay cannot leave a ghost Draft or pending merge indefinitely.
            previous=pr_tracking.snapshot()
            old_rows={p["number"]:p for p in previous.get("prs",[])}
            if (old.get("repository"),old.get("branch"))==target:
                old_rows.update({p["number"]:p for p in old.get("prs",[])})
            for number,row in old_rows.items():
                if row.get("merge_state") in {"open","draft"} and number not in seen:
                    listings.append(request("GET",prefix+"/pulls/"+str(number)))
            rows=pr_tracking.collect(request,repo,branch,listings=listings)
            # Carry forward recent terminal transitions until history catches up.
            for row in old_rows.values():
                if row["number"] not in {p["number"] for p in rows} and row.get("merge_state") in {"merged","closed"} and row["number"] not in {p["number"] for p in previous.get("prs",[])}:
                    rows.append(row)
            write_json(path,{"repository":repo,"branch":branch,"synced_at":datetime.now(timezone.utc).isoformat(),"prs":rows})
            return 0
        except (release_acceptance.AcceptanceError,OSError,ValueError,KeyError,TypeError):
            return 1


if __name__ == "__main__":
    raise SystemExit(refresh_active() if "--active" in sys.argv[1:] else run_refresh(force="--force" in sys.argv[1:]))
