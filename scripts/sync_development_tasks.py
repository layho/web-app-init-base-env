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


def retry_deadline(reason, now):
    """Only classified rate limits trigger a shared cooldown; no raw API body."""
    if not isinstance(reason, str):
        return None
    if "GitHub API 主额度耗尽" in reason:
        match = re.search(r"恢复时间 ([^；]+)$", reason)
        if match:
            try:
                stamp = datetime.fromisoformat(match[1])
                if stamp.tzinfo is not None:
                    return max(now + timedelta(seconds=60), stamp + timedelta(seconds=5))
            except ValueError:
                pass
        return now + timedelta(minutes=5)
    if "GitHub 请求限流" in reason:
        match = re.search(r"建议等待 ([0-9]{1,6}) 秒", reason)
        return now + timedelta(seconds=max(60, int(match[1]) if match else 60))
    return None


def cooldown(reason=None):
    """Share a durable retry deadline across manual, history and active workers."""
    folder = development_tasks.CACHE_PATH.parent
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "task-sync-cooldown.json"
    now = datetime.now(timezone.utc)
    with (folder / "task-sync-cooldown.lock").open("a") as gate:
        fcntl.flock(gate, fcntl.LOCK_EX)
        try:
            saved = json.loads(path.read_text())
            until = pr_tracking.instant(saved.get("retry_at"))
            if not isinstance(saved.get("reason"), str) or not retry_deadline(saved["reason"], now):
                saved, until = {}, None
        except (OSError, ValueError, TypeError, AttributeError):
            saved, until = {}, None
        # Re-observing our own local hold is not a new server rate limit.
        # The worker must not extend a relative Retry-After on every retry.
        candidate = retry_deadline(reason, now)
        if candidate and (until is None or candidate > until):
            until = candidate
            saved = {"retry_at": until.isoformat(), "reason": reason}
            write_json(path, saved)
        return saved if until and until > now else None


class LocalCooldown(release_acceptance.AcceptanceError):
    """A local hold, not a new GitHub response."""


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
            held = cooldown()
            if held:
                write_status({**refresh_status(), "state": "failed", "phase": "rate_limit", "requests": 0,
                              "reason": held["reason"] + "；下次重试 " + held["retry_at"]})
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
            if isinstance(exc, release_acceptance.AcceptanceError):
                # Stop remaining diagnostic probes when quota is exhausted.
                if retry_deadline(str(exc), datetime.now(timezone.utc)):
                    raise
        result[label]["elapsed_seconds"] = round(time.monotonic() - started, 2)
    check("identity", lambda: request("GET", "/user"), lambda o: {"login": o["login"]})
    check("rate_limit", lambda: request("GET", "/rate_limit"),
          lambda o: {"remaining": o["resources"]["core"]["remaining"], "limit": o["resources"]["core"]["limit"]})
    check("repository", lambda: request("GET", "/repos/" + repository),
          lambda o: {"permissions": {k: v for k, v in o.get("permissions", {}).items()
                                     if k in {"admin", "maintain", "push", "triage", "pull"} and type(v) is bool}})
    def list_fields(obj):
        if not isinstance(obj, list):
            raise ValueError("Invalid list response")
        return {}
    check("issues", lambda: request("GET", "/repos/" + repository + "/issues?state=all&per_page=1"), list_fields)
    check("pulls", lambda: request("GET", "/repos/" + repository + "/pulls?state=open&per_page=1"), list_fields)
    owner, name = repository.split("/")
    # GraphQL POST carries a fixed read query, never a mutation.
    query = 'query($owner:String!,$name:String!){viewer{login} repository(owner:$owner,name:$name){viewerPermission}}'
    def graphql_fields(o):
        if o.get("errors") or not o.get("data", {}).get("repository"):
            raise ValueError("GraphQL read failed")
        return {"permission": o["data"]["repository"]["viewerPermission"]}
    check("graphql", lambda: release_acceptance.github("/graphql", {"query": query, "variables": {"owner": owner, "name": name}}), graphql_fields)
    return result


def publication_history(request, repository, main_sha, tracking, prs):
    """Reuse positive ancestry evidence only across a verified forward move."""
    from app import release_acceptance
    previous = tracking.get("published_main_sha")
    old = tracking.get("published_commits", {})
    prefix = "/repos/" + repository + "/compare/"
    def contains(ancestor, descendant):
        if ancestor == descendant:
            return True
        result = request("GET", prefix + ancestor + "..." + descendant)
        return (result.get("status") in {"ahead", "identical"}
                and (result.get("merge_base_commit") or {}).get("sha") == ancestor)
    if not isinstance(old, dict) or not release_acceptance.valid_sha(previous) or not contains(previous, main_sha):
        old = {}
    published = {}
    for pr in prs:
        candidate = pr.get("merge_sha")
        if pr.get("merge_state") != "merged" or not release_acceptance.valid_sha(candidate):
            continue
        if candidate not in published:
            published[candidate] = candidate == main_sha or old.get(candidate) is True or contains(candidate, main_sha)
    return published


def run_refresh(force=False):
    from app import release_acceptance
    folder = development_tasks.CACHE_PATH.parent
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "task-sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX if force else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        prior_status = refresh_status()
        # Acquiring the exclusive worker lock proves no other worker owns
        # the refresh. A leftover running record is therefore recoverable.
        recovering = prior_status.get("state") in {"failed", "interrupted", "running", "queued"}
        force = force or prior_status.get("state") == "queued"
        held = cooldown()
        if held:
            write_status({**refresh_status(), "state": "failed", "phase": "rate_limit", "requests": 0,
                          "reason": held["reason"] + "；下次重试 " + held["retry_at"]})
            return 1
        target = pr_tracking.target()
        previous = development_tasks.snapshot()
        tracking = pr_tracking.snapshot() if target else {}
        if not force and not recovering and not previous.get("stale") and not tracking.get("stale", True):
            if all(datetime.now(timezone.utc) - datetime.fromisoformat(x["synced_at"]) < development_tasks.REFRESH_INTERVAL for x in (previous, tracking)):
                return 0
        start = time.monotonic()
        state = {"state": "running", "phase": "issues", "requests": 0,
                 "token_health": prior_status.get("token_health", {}),
                 "token_health_checked_at": prior_status.get("token_health_checked_at"),
                 "started_at": datetime.now(timezone.utc).isoformat()}
        if recovering:
            state["recovery_from"] = prior_status.get("state")
            state["previous_failure"] = prior_status.get("reason")
        write_status(state)
        def request(method, path):
            if method != "GET":
                raise ValueError("Read-only task sync")
            held = cooldown()
            if held:
                raise LocalCooldown(held["reason"])
            state["requests"] += 1
            write_status({**state, "elapsed_seconds": round(time.monotonic() - start, 1)})
            try:
                return release_acceptance.github(path)
            except release_acceptance.AcceptanceError as exc:
                if not isinstance(exc, LocalCooldown):
                    cooldown(str(exc))
                raise
        try:
            if target is None:
                raise ValueError("No verified repository")
            if force:
                state["phase"] = "token_health"
                state["token_health"] = token_health(target[0], request)
                state["token_health_checked_at"] = datetime.now(timezone.utc).isoformat()
                held = cooldown()
                if held:
                    raise LocalCooldown(held["reason"])
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
            # Publication is a distinct fact from per-PR user acceptance.
            # Prove historical commits by ancestry of actual official main.
            # Reuse positive results only if previous main is proven an ancestor.
            published = {}
            main_sha = None
            if target == (release_acceptance.REPO, "preview"):
                state["phase"] = "publication_history"
                write_status(state)
                main_sha = request("GET", "/repos/" + target[0] + "/branches/main")["commit"]["sha"]
                if not release_acceptance.valid_sha(main_sha):
                    raise ValueError("Invalid official main")
                published = publication_history(request, target[0], main_sha, tracking, prs)
            pr_data = {"synced_at": stamp, "repository": target[0], "branch": target[1], "prs": prs, "release_evidence": evidence,
                       "published_main_sha":main_sha, "published_commits":published}
            # One atomic publication is authoritative for the unified page.
            write_json(development_tasks.CACHE_PATH, issues)
            write_json(pr_tracking.CACHE, pr_data)
            write_json(folder / "task-sync-combined.json", {"issues": issues, "tracking": pr_data})
            state["state"] = "success"
        except (release_acceptance.AcceptanceError, OSError, ValueError, TypeError, KeyError) as exc:
            state["state"] = "failed"
            # AcceptanceError messages are already sanitized by the REST client.
            state["reason"] = str(exc) if isinstance(exc, release_acceptance.AcceptanceError) else type(exc).__name__
            held = cooldown() if isinstance(exc, LocalCooldown) else cooldown(state["reason"])
            if held:
                state["reason"] = held["reason"] + "；下次重试 " + held["retry_at"]
            if state["phase"] in {"issues", "pulls"} and isinstance(exc, release_acceptance.AcceptanceError):
                state["token_health"] = {**state["token_health"], state["phase"]: {"state": "failed", "reason": state["reason"]}}
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
        if cooldown():
            return 1
        if stamp and (old.get("repository"),old.get("branch"))==target and (datetime.now(timezone.utc)-stamp).total_seconds()<25:
            return 0
        def request(method,path):
            if method!="GET":
                raise ValueError("Read only")
            held = cooldown()
            if held:
                raise LocalCooldown(held["reason"])
            try:
                return release_acceptance.github(path)
            except release_acceptance.AcceptanceError as exc:
                if not isinstance(exc, LocalCooldown):
                    cooldown(str(exc))
                raise
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
