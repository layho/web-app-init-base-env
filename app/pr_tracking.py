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
CONTEXT = "macmini/isolated-tests-v1"
SHA = re.compile(r"[0-9a-f]{40}\Z")
STATUS = re.compile(r"base=([0-9a-f]{40}) merge=([0-9a-f]{40}|unknown) ([a-z-]+)\Z")
PHASES = {"pending", "passed", "setup-failed", "fetch-failed", "context-failed",
          "dependencies-failed", "pytest-failed", "compile-failed", "frontend-failed",
          "test-failed", "ref-changed"}
COUNT = re.compile(r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed|deselected|warnings?)")


def enabled():
    return os.getenv("APP_BASE_PREVIEW") == "1" and os.getenv("APP_PUBLIC_ORIGIN") == "https://base.layho.com"


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


def log_summary(number, head, base, test):
    # Exact repository and filenames only. Read a bounded tail without following
    # symlinks, return enumerated stages/counts, never arbitrary log text.
    if type(number) is not int or number <= 0 or not valid_sha(head) or not valid_sha(base):
        return None
    if test.get("state") not in {"success", "failure"} or not test.get("started_at") or not test.get("finished_at"):
        return None
    path = LOG_ROOT / f"layho--web-app-init-base-env-{number}-{head[:12]}-{base[:12]}.log"
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


def collect(github):
    prefix = "/repos/" + REPO
    prs = github("GET", prefix + "/pulls?state=all&sort=updated&direction=desc&per_page=50")
    if not isinstance(prs, list):
        raise ValueError("Invalid PR list")
    rows = []
    for pr in prs:
        number = pr["number"]
        head, base = pr["head"]["sha"], pr["base"]["sha"]
        if type(number) is not int or number <= 0 or not valid_sha(head) or not valid_sha(base):
            continue
        if pr["base"]["ref"] != "preview" or (pr["head"].get("repo") or {}).get("full_name") != REPO:
            continue
        merged = bool(pr.get("merged_at"))
        merge = pr.get("merge_commit_sha") if merged else None
        if merged and valid_sha(merge):
            commit = github("GET", prefix + "/git/commits/" + merge)
            parents = [p["sha"] for p in commit["parents"]]
            # Closed PR base ref may advance. Use the actual merged parents.
            base = parents[0] if len(parents) == 2 and parents[1] == head and valid_sha(parents[0]) else None
        statuses = github("GET", prefix + "/commits/" + head + "/statuses?per_page=100")
        if not isinstance(statuses, list):
            raise ValueError("Invalid statuses")
        test = evidence(statuses, base) if valid_sha(base) else {"state": "unknown"}
        log = log_summary(number, head, base, test)
        rows.append({"number": number, "title": str(pr.get("title", ""))[:300],
                     "url": "https://github.com/" + REPO + "/pull/" + str(number),
                     "head": head, "base": base, "test": test, "log": log,
                     "merge_state": "merged" if merged else "closed" if pr.get("state") == "closed" else "draft" if pr.get("draft") else "open",
                     "merge_blocked": not merged and not pr.get("draft") and pr.get("mergeable") is False,
                     "mergeability": pr.get("mergeable_state") if isinstance(pr.get("mergeable_state"), str) else "unknown",
                     "merged_at": pr.get("merged_at"), "merge_sha": merge if valid_sha(merge) else None})
    return rows


def sync(github):
    current = snapshot()
    previous = instant(current.get("synced_at"))
    if previous and (datetime.now(timezone.utc) - previous).total_seconds() < 300:
        return
    data = {"synced_at": datetime.now(timezone.utc).isoformat(), "prs": collect(github)}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=CACHE.parent, delete=False) as out:
        try:
            json.dump(data, out, ensure_ascii=False)
            out.close()
            os.replace(out.name, CACHE)
        finally:
            if os.path.exists(out.name):
                os.unlink(out.name)


def snapshot():
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        dt = instant(data["synced_at"])
        if dt is None or not isinstance(data["prs"], list):
            raise ValueError("invalid snapshot")
    except (OSError, ValueError, KeyError, TypeError):
        return {"synced_at": None, "stale": True, "prs": []}
    events = operations.recent_operations(operations.OPERATIONS_LOG)
    running = os.getenv("APP_RUNNING_SHA", "unknown")
    for row in data["prs"]:
        sha = row.get("merge_sha")
        event = next((e for e in events if sha and e["sha"] == sha and e["code"] in {"install_ok", "update_ok", "rollback", "install_failed"}), None)
        row["deployment"] = {"state": "not_merged" if row["merge_state"] != "merged" else
                             "running" if sha and sha == running and event and event["code"] in {"install_ok", "update_ok"} else
                             "deployed" if event and event["code"] in {"install_ok", "update_ok"} else
                             "failed" if event else "unverified", "at": event["at"] if event else None}
    return {**data, "stale": (datetime.now(timezone.utc) - dt).total_seconds() > 600}
