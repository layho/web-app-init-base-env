"""Read-only evidence for base PRs; never expose raw MacCI logs or tokens."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
from tempfile import NamedTemporaryFile

from . import operations

CACHE = Path(__file__).resolve().parents[1] / "logs" / "pr-tracking.json"
LOG_ROOT = Path.home() / ".local/share/macmini-pr-tests"
REPO = "layho/web-app-init-base-env"
METADATA = Path(__file__).resolve().parents[1] / ".app-init.json"
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
CONTEXT = "macmini/isolated-tests-v1"
SHA = re.compile(r"[0-9a-f]{40}\Z")
STATUS = re.compile(r"base=([0-9a-f]{40}) merge=([0-9a-f]{40}|unknown) ([a-z-]+)\Z")
PHASES = {"pending", "passed", "setup-failed", "fetch-failed", "context-failed",
          "dependencies-failed", "pytest-failed", "compile-failed", "frontend-failed",
          "test-failed", "ref-changed"}
COUNT = re.compile(r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed|deselected|warnings?)")


def valid_repository(repo):
    """Reject relative/dot GitHub repository segments before using a repo ref."""
    return (isinstance(repo, str) and len(repo) <= 200
            and REPOSITORY.fullmatch(repo) is not None
            and all(segment not in {".", ".."} for segment in repo.split("/")))


def target():
    """Resolve only the current installation's own repository.

    An app cannot query other repositories through a browser-supplied argument.
    Base preview uses its known static repository; generated apps use the
    installer-signed local metadata and their configured public origin.
    """
    if os.getenv("APP_BASE_PREVIEW") == "1":
        return (REPO, "preview") if os.getenv("APP_PUBLIC_ORIGIN") == "https://base.layho.com" else None
    try:
        obj = json.loads(METADATA.read_text(encoding="utf-8"))
        repo, domain = obj["repository"], obj["domain"]
        if (not isinstance(obj, dict) or not isinstance(repo, str)
                or not valid_repository(repo) or not isinstance(domain, str)
                or not re.fullmatch(r"[a-z0-9.-]{3,253}", domain)
                or os.getenv("APP_PUBLIC_ORIGIN") != "https://" + domain):
            return None
        return repo, "main"
    except (OSError, ValueError, TypeError, KeyError):
        return None


def enabled():
    return target() is not None


def instant(value):
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return d if d.tzinfo else None
    except (ValueError, AttributeError, TypeError):
        return None


def valid_sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def evidence(statuses, base):
    matching = []
    for item in statuses:
        if item.get("context") != CONTEXT:
            continue
        match = STATUS.fullmatch(item.get("description") or "")
        if not match or match[1] != base or match[3] not in PHASES:
            continue
        if item.get("state") not in {"pending", "success", "failure", "error"}:
            continue
        if instant(item.get("created_at")) is None:
            continue
        matching.append((item, match))
    if not matching:
        return {"state": "unknown"}
    matching.sort(key=lambda pair: instant(pair[0]["created_at"]), reverse=True)
    item, match = matching[0]
    state = item["state"]
    # Only a pending event directly preceding this terminal attempt is a start.
    start = item["created_at"] if state == "pending" else (
        matching[1][0]["created_at"] if len(matching) > 1 and matching[1][0]["state"] == "pending" else None)
    end = None if state == "pending" else item["created_at"]
    elapsed = (instant(end) - instant(start)).total_seconds() if end and start else None
    return {"state": state, "started_at": start, "finished_at": end,
            "duration_seconds": int(elapsed) if elapsed is not None and elapsed >= 0 else None,
            "phase": match[3], "tested_merge": match[2] if valid_sha(match[2]) else None}


def advisory_progress(statuses, base, candidate, test, owner, now=None):
    """Advisory heartbeat must belong to the current pending attempt."""
    if test.get("state") != "pending" or not valid_sha(candidate):
        return None
    started = instant(test.get("started_at"))
    if not started:
        return None
    now = now or datetime.now(timezone.utc)
    rows = []
    for item in statuses:
        if (item.get("context") != "macmini/progress-v1" or item.get("state")!="pending"
                or (item.get("creator") or {}).get("login")!=owner):
            continue
        match=STATUS.fullmatch(item.get("description") or "")
        at=instant(item.get("created_at"))
        if (match and match[1]==base and match[2]==candidate and
            match[3] in {"setup","fetch","context","dependencies","test","cleanup"}
            and at and started<=at<=now):
            rows.append((at,match[3]))
    if not rows:
        return None
    at,phase=max(rows,key=lambda x:x[0])
    return {"phase":phase,"heartbeat_at":at.isoformat(),
            "heartbeat_stale":(now-at).total_seconds()>120,
            "elapsed_seconds":max(0,int((now-started).total_seconds()))}


def head_status_diagnostic(statuses):
    """Describe latest GitHub MacCI signal separately from exact-base evidence.

    A failure for this HEAD is worth displaying even if its base/merge
    description is malformed or stale. It must NOT authorize a merge or be
    treated as an exact verified test result.
    """
    rows = [(instant(item.get("created_at")), item) for item in statuses
            if isinstance(item, dict) and item.get("context") == CONTEXT
            and item.get("state") in {"pending", "success", "failure", "error"}
            and instant(item.get("created_at")) is not None]
    if not rows:
        return {"state": "unknown", "at": None}
    _, latest = max(rows, key=lambda pair: pair[0])
    return {"state": latest["state"], "at": latest["created_at"]}


def log_summary(number, head, base, test, repo=None):
    # Exact repository and filenames only. Read a bounded tail without following
    # symlinks, return enumerated stages/counts, never arbitrary log text.
    if type(number) is not int or number <= 0 or not valid_sha(head) or not valid_sha(base):
        return None
    if test.get("state") not in {"success", "failure"} or not test.get("started_at") or not test.get("finished_at"):
        return None
    repo = repo or REPO
    if not valid_repository(repo):
        return None
    path = LOG_ROOT / f"{repo.replace('/', '--')}-{number}-{head[:12]}-{base[:12]}.log"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            updated = datetime.fromtimestamp(info.st_mtime, timezone.utc)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or updated < instant(test["started_at"])
                or updated.timestamp() > instant(test["finished_at"]).timestamp() + 5):
                return None
            stream.seek(max(0, info.st_size - 128_000))
            text = stream.read(128_000).decode("utf-8", errors="replace")
    except (OSError, ValueError, TypeError):
        return None
    # Old attempts are appended; inspect only the last recognizable test launch.
    if "+ git init --bare " not in text:
        return None
    text = text.rsplit("+ git init --bare ", 1)[1]
    summaries = []
    for line in text.splitlines():
        # Real pytest -q terminal summary: all tokens must match the grammar.
        clean = line.strip().strip("=").strip()
        if not re.fullmatch(r"\d+ (?:passed|failed|skipped|errors?|xfailed|xpassed|deselected|warnings?)(?:, \d+ (?:passed|failed|skipped|errors?|xfailed|xpassed|deselected|warnings?))* in \d+(?:\.\d+)?s(?: \([0-9:]+\))?", clean):
            continue
        summaries.append({key: int(n) for n, key in COUNT.findall(clean)})
    counts = summaries[-1] if summaries else None
    if counts and test["state"] == "success" and (counts.get("failed", 0) or counts.get("error", 0) or counts.get("errors", 0)):
        counts = None
    if counts and test.get("phase") not in {"passed", "pytest-failed", "compile-failed", "frontend-failed", "test-failed"}:
        counts = None
    return {"pytest": counts, "phase": test.get("phase"), "source": "MacCI local log summary"}


def explicit_issue_refs(body):
    """Recognize explicit Issue references only; not generic unlabelled #numbers."""
    if not isinstance(body, str):
        return []
    # GitHub standard closing keywords or deliberate issue/task labels.
    found = set()
    for match in re.finditer(
            r"(?im)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs?|relates?\s+to|"
            r"related(?:\s+to)?|issue|task|关联(?:任务|需求|issue))\s*:?\s*"
            r"(?:https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/)?#?([1-9][0-9]{0,6})\b",
            body[:40000]):
        found.add(int(match.group(1)))
    return sorted(found)


def list_all_prs(github, prefix):
    """Traverse stable creation order, deduplicate, and fail closed on bad pages."""
    rows = {}
    page = 1
    while True:
        batch = github("GET", prefix + "/pulls?state=all&sort=created&direction=asc&per_page=100&page=" + str(page))
        if not isinstance(batch, list) or len(batch) > 100:
            raise ValueError("Invalid PR page")
        previous = len(rows)
        for row in batch:
            if not isinstance(row, dict) or type(row.get("number")) is not int or row["number"] <= 0:
                raise ValueError("Invalid PR entry")
            rows[row["number"]] = row
        if len(batch) < 100:
            return sorted(rows.values(), key=lambda row: row["number"], reverse=True)
        if len(rows) == previous:
            raise ValueError("PR pagination did not advance")
        page += 1


def failure_report(comments, repo, number, head, base, test):
    """Diagnostic only. Exact candidate and latest terminal attempt required."""
    if test.get("state") not in {"failure", "error"} or not valid_sha(test.get("tested_merge")):
        return None
    end = instant(test.get("finished_at"))
    start = instant(test.get("started_at"))
    if not end:
        return None
    reports = []
    for comment in comments:
        body = comment.get("body") or ""
        if (comment.get("author_association") != "OWNER" or
                (comment.get("user") or {}).get("login") != repo.split("/")[0] or
                comment.get("updated_at") != comment.get("created_at") or
                not isinstance(body,str) or not body.startswith("[MACCI_REPORT] ") or len(body)>12000):
            continue
        try:
            row = json.loads(body[len("[MACCI_REPORT] "):])
            if not isinstance(row,dict) or row.get("schema")!=1:
                continue
            if (row.get("repository"),row.get("pr"),row.get("head"),row.get("base"),row.get("merge"),
                row.get("state"),row.get("phase")) != (repo,number,head,base,test["tested_merge"],test["state"],test.get("phase")):
                continue
            run = row.get("run_id")
            if not isinstance(run,str) or not re.fullmatch(r"[0-9a-f]{32}",run):
                continue
            finished = row.get("finished_at")
            began = row.get("started_at")
            if type(finished) is not int or type(began) is not int or began>finished or abs(finished-end.timestamp())>120 or (start and began<start.timestamp()-5):
                continue
            entries=[]
            if not isinstance(row.get("failures"),list):
                continue
            for item in row["failures"][:12]:
                if not isinstance(item,dict):
                    continue
                file,category=item.get("file"),item.get("category")
                if not isinstance(file,str) or not isinstance(category,str) or ".." in Path(file).parts:
                    continue
                if category=="pytest_failure" and re.fullmatch(r"tests/[A-Za-z0-9_/]{1,120}\.py",file):
                    name=item.get("test")
                    if isinstance(name,str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}(?:::[A-Za-z_][A-Za-z0-9_]{0,79})?",name):
                        entries.append({"file":file,"test":name,"category":category})
                elif re.fullmatch(r"TS\d{4}",category) and re.fullmatch(r"frontend/src/[A-Za-z0-9_/-]{1,120}\.tsx?",file):
                    line,col=item.get("line"),item.get("column")
                    if type(line) is int and type(col) is int and 0<line<=999999 and 0<col<=999999:
                        entries.append({"file":file,"category":category,"line":line,"column":col})
            reports.append((finished,{"run_id":run,"failures":entries,"phase":row["phase"]}))
        except (ValueError,TypeError,KeyError,OverflowError):
            continue
    return max(reports,key=lambda pair:pair[0])[1] if reports else None


def collect(github, repo=None, branch=None, *, cached=None, listings=None):
    current = target() if repo is None else (repo, branch or "main")
    if current is None or not valid_repository(current[0]):
        raise ValueError("No verified application repository")
    repo, branch = current
    prefix = "/repos/" + repo
    prs = list_all_prs(github, prefix) if listings is None else listings
    # The PR's base.sha can be stale after preview/main advances. A pending
    # test is valid only against the actual current target branch SHA.
    latest_sha = github("GET", prefix + "/branches/" + branch)["commit"]["sha"]
    if not valid_sha(latest_sha):
        raise ValueError("Invalid current branch")
    rows = []
    cached = cached or {}
    for listing in prs:
        number = listing["number"]
        # GitHub's list endpoint may omit mergeable/mergeable_state. Refresh
        # open PR details or the UI can silently hide a dirty Draft PR.
        pr = github("GET", prefix + "/pulls/" + str(number)) if listing.get("state") == "open" else listing
        head, base = pr["head"]["sha"], pr["base"]["sha"]
        if type(number) is not int or number <= 0 or not valid_sha(head) or not valid_sha(base):
            continue
        if pr["base"]["ref"] != branch or (pr["head"].get("repo") or {}).get("full_name") != repo:
            continue
        # GitHub's mergeable=dirty is the authoritative conflict gate.
        # /pulls/N/files lists PR-changed files, not necessarily the exact
        # conflicting hunks; label them as candidates, never as proven conflicts.
        blocked = (pr.get("state") == "open" and pr.get("mergeable") is False
                   and pr.get("mergeable_state") == "dirty")
        candidates = []
        file_total = pr.get("changed_files")
        if blocked:
            files = github("GET", prefix + "/pulls/" + str(number) + "/files?per_page=100")
            if isinstance(files, list):
                candidates = [f["filename"] for f in files
                              if isinstance(f, dict) and isinstance(f.get("filename"), str)
                              and len(f["filename"]) <= 180 and not f["filename"].startswith("/")
                              and ".." not in Path(f["filename"]).parts][:100]
        if pr.get("state") == "open":
            base = latest_sha
        merged = bool(pr.get("merged_at"))
        merge = pr.get("merge_commit_sha") if merged else None
        old = cached.get(number, {})
        # The merge object is immutable. Reuse only exact verified parents;
        # status receipts remain live and must be fetched for open PRs.
        if merged and valid_sha(merge) and old.get("merge_sha") == merge and old.get("head") == head and valid_sha(old.get("base")):
            base = old["base"]
        elif merged and valid_sha(merge):
            commit = github("GET", prefix + "/git/commits/" + merge)
            parents = [p["sha"] for p in commit["parents"]]
            # Closed PR base ref may advance. Use the actual merged parents.
            base = parents[0] if len(parents) == 2 and parents[1] == head and valid_sha(parents[0]) else None
        if (merged and old.get("merge_sha") == merge and old.get("head") == head
                and old.get("github_updated_at") == pr.get("updated_at")
                and pr.get("updated_at") and old.get("base") == base
                and old.get("test", {}).get("state") == "success"):
            # Historical success is display evidence, never merge authority.
            # Manual refresh bypasses this cache.
            rows.append({**old, "title": str(pr.get("title", ""))[:300],
                         "linked_issues": explicit_issue_refs(pr.get("body"))})
            continue
        statuses = github("GET", prefix + "/commits/" + head + "/statuses?per_page=100")
        if not isinstance(statuses, list):
            raise ValueError("Invalid statuses")
        test = evidence(statuses, base) if valid_sha(base) else {"state": "unknown"}
        if pr.get("state") == "open":
            test["progress"] = advisory_progress(statuses,base,pr.get("merge_commit_sha"),test,repo.split("/")[0])
        log = log_summary(number, head, base, test, repo)
        if test.get("state") in {"failure","error"} and test.get("tested_merge"):
            comments = github("GET", prefix + "/issues/" + str(number) + "/comments?per_page=100")
            if isinstance(comments,list):
                test["failure_report"] = failure_report(comments,repo,number,head,base,test)
        rows.append({"number": number, "title": str(pr.get("title", ""))[:300],
                     "created_at": pr.get("created_at"), "github_updated_at": pr.get("updated_at"),
                     "url": "https://github.com/" + repo + "/pull/" + str(number),
                     "owner": str((pr.get("user") or {}).get("login") or "未指定")[:100],
                     "linked_issues": explicit_issue_refs(pr.get("body")),
                     "head": head, "base": base, "head_branch": pr["head"].get("ref"),
                     "base_branch": pr["base"].get("ref"), "test": test, "log": log,
                     "head_status": head_status_diagnostic(statuses),
                     "merge_state": "merged" if merged else "closed" if pr.get("state") == "closed" else "draft" if pr.get("draft") else "open",
                     "merge_blocked": blocked,
                     "conflict": {"blocked": blocked, "base_sha": latest_sha,
                                  "candidate_files": candidates,
                                  "file_scope": "PR 修改文件（非已确认的逐文件冲突）",
                                  "reason": "GitHub 确认 PR 与目标分支存在合并冲突" if blocked else None},
                     "mergeability": pr.get("mergeable_state") if isinstance(pr.get("mergeable_state"), str) else "unknown",
                     "merged_at": pr.get("merged_at"), "merge_sha": merge if valid_sha(merge) else None})
    return rows


def sync(github, *, force=False):
    current_target = target()
    if current_target is None:
        return
    current = snapshot()
    previous = instant(current.get("synced_at"))
    if not force and previous and (datetime.now(timezone.utc) - previous).total_seconds() < 300:
        return
    data = {"synced_at": datetime.now(timezone.utc).isoformat(),
            "repository": current_target[0], "branch": current_target[1],
            "prs": collect(github, *current_target)}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=CACHE.parent, delete=False) as out:
        try:
            json.dump(data, out, ensure_ascii=False)
            out.close()
            os.replace(out.name, CACHE)
        finally:
            if os.path.exists(out.name):
                os.unlink(out.name)


def snapshot(data=None):
    current = target()
    try:
        if data is None:
            data = json.loads(CACHE.read_text(encoding="utf-8"))
        dt = instant(data["synced_at"])
        if (current is None or data.get("repository") != current[0]
                or data.get("branch") != current[1]
                or dt is None or not isinstance(data["prs"], list)):
            raise ValueError("invalid snapshot")
    except (OSError, ValueError, KeyError, TypeError):
        return {"synced_at": None, "stale": True, "prs": []}
    events = operations.recent_operations(operations.OPERATIONS_LOG)
    running = os.getenv("APP_RUNNING_SHA", "unknown")
    from app import release_acceptance
    evidence = data.get("release_evidence", {}) if current == (REPO, "preview") else {}
    published = data.get("published_commits", {}) if current == (REPO, "preview") else {}
    for row in data["prs"]:
        sha = row.get("merge_sha")
        row["published_in_main"] = bool(row.get("merge_state") == "merged" and valid_sha(sha) and published.get(sha) is True)
        event = next((e for e in events if sha and e["sha"] == sha and e["code"] in {"install_ok", "update_ok", "rollback", "install_failed"}), None)
        if current == (REPO, "preview") and row.get("merge_state") == "merged" and valid_sha(sha):
            row["release_acceptance_enabled"] = True
            saved = evidence.get(sha, {})
            if saved.get("decision") == "accepted" and saved.get("test_head") != row.get("head"):
                saved = {}
            local = release_acceptance.local_decision(sha, row.get("head"))
            if local and (not saved or saved.get("decision") != local.get("decision")):
                saved = local
            row["acceptance"] = saved
        row["deployment"] = {"state": "not_merged" if row["merge_state"] != "merged" else
                             "running" if sha and sha == running and event and event["code"] in {"install_ok", "update_ok"} else
                             "deployed" if event and event["code"] in {"install_ok", "update_ok"} else
                             "failed" if event else "unverified", "at": event["at"] if event else None}
    return {**data, "running_sha": running if valid_sha(running) else None,
            "can_release_draft": current == (REPO, "preview"),
            "stale": (datetime.now(timezone.utc) - dt).total_seconds() > 600}

def active_snapshot(data):
    """Overlay independently fetched current PRs, never erase history on failure."""
    current = target()
    try:
        raw = json.loads((CACHE.parent / "pr-active.json").read_text())
        stamp = instant(raw["synced_at"])
        if not current or (raw["repository"],raw["branch"]) != current or not stamp:
            return data
        if instant(data.get("synced_at")) and stamp < instant(data["synced_at"]):
            return data
        rows = {p["number"]:p for p in data.get("prs",[])}
        for p in raw["prs"]:
            rows[p["number"]] = p
        result = snapshot({**data, "repository":current[0],"branch":current[1],
                           "synced_at":raw["synced_at"],"prs":sorted(rows.values(),key=lambda p:p["number"],reverse=True)})
        result["active_synced_at"] = raw["synced_at"]
        result["history_synced_at"] = data.get("synced_at")
        result["stale"] = (datetime.now(timezone.utc)-stamp).total_seconds()>120
        return result
    except (OSError, ValueError, TypeError, KeyError):
        return data
