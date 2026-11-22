"""Release ONLY the exact candidate explicitly accepted on the base website."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from tempfile import TemporaryDirectory
from urllib.error import URLError
from urllib.request import Request, build_opener

import merge_preview as m

RECEIPTS = Path.home() / "web-app-init-base-env-preview/logs/release-acceptance"
TITLE = re.compile(r"\[BASE-ACCEPTANCE ([0-9a-f]{40})\]\Z")
MARKER = "[BASE_ACCEPTANCE] "
KEYS = {"schema", "candidate_sha", "expected_main_sha", "version", "test_head",
        "test_base", "tested_merge", "tree"}
SHA_KEYS = KEYS - {"schema", "version"}


def parse(issue):
    match = TITLE.fullmatch(issue.get("title") or "")
    if (not match or issue.get("state") != "open" or "pull_request" in issue
            or issue.get("author_association") != "OWNER"
            or issue.get("user", {}).get("login") != m.OWNER
            or type(issue.get("number")) is not int or issue["number"] <= 0):
        return None
    body = issue.get("body") or ""
    if not body.startswith(MARKER) or "\n" in body:
        return None
    try:
        data = json.loads(body[len(MARKER):])
    except ValueError:
        return None
    if (not isinstance(data, dict) or set(data) != KEYS or type(data["schema"]) is not int
            or data["schema"] != 1 or data["candidate_sha"] != match[1]
            or not all(m.valid(data[k]) for k in SHA_KEYS)
            or not isinstance(data["version"], str)
            or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]{1,25})?", data["version"])):
        return None
    return data


def local_receipt(sha, issue):
    if not m.valid(sha):
        return False
    try:
        fd = os.open(RECEIPTS / (sha + ".json"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                return False
            data = json.loads(stream.read(4096))
        return isinstance(data, dict) and type(data.get("issue")) is int and data["issue"] == issue
    except (OSError, ValueError, TypeError):
        return False


def ref(token, name):
    if name not in {"main", "preview"}:
        raise ValueError("Unexpected branch")
    sha = m.api(token, "/branches/" + name)["commit"]["sha"]
    if not m.valid(sha):
        raise ValueError("Invalid ref")
    return sha


def evidence(token, data):
    candidate = data["candidate_sha"]
    parents = [data["test_base"], data["test_head"]]
    for sha in (candidate, data["tested_merge"]):
        commit = m.api(token, "/git/commits/" + sha)
        if ([p["sha"] for p in commit["parents"]] != parents
                or commit["tree"]["sha"] != data["tree"]):
            return False
    statuses = m.api(token, "/commits/" + data["test_head"] + "/statuses?per_page=100")
    if not m.tested(statuses, data["test_head"], data["test_base"], data["tested_merge"]):
        return False
    prs = m.all_pages(token, "/commits/" + candidate + "/pulls")
    return any(p.get("merged_at") and p.get("merge_commit_sha") == candidate
               and p.get("base", {}).get("ref") == "preview"
               and p.get("base", {}).get("repo", {}).get("full_name") == m.REPO
               and p.get("head", {}).get("sha") == data["test_head"] for p in prs)


def accepted(token, number):
    issue = m.api(token, f"/issues/{number}")
    data = parse(issue)
    if not data or not local_receipt(data["candidate_sha"], number):
        return None
    return data


def ancestor(folder, older, newer):
    try:
        m.git(folder, "merge-base", "--is-ancestor", older, newer)
        return True
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1:
            return False
        raise


def write_api(token, path, method, payload):
    request = Request("https://api.github.com/repos/" + m.REPO + path,
                      method=method, data=json.dumps(payload).encode(),
                      headers={"Authorization": "Bearer " + token,
                               "Accept": "application/vnd.github+json",
                               "Content-Type": "application/json",
                               "User-Agent": "macci-main-publish"})
    with build_opener(m.NoRedirect()).open(request, timeout=20) as stream:
        return json.load(stream)


def acknowledge(token, number, candidate, main):
    marker = "[BASE_PUBLISHED] "
    message = marker + json.dumps({"candidate_sha": candidate, "main_sha": main,
                                   "executor": "macci-main-publish"}, sort_keys=True)
    comments = m.all_pages(token, f"/issues/{number}/comments")
    if not any(c.get("author_association") == "OWNER" and c.get("user", {}).get("login") == m.OWNER
               and c.get("body") == message for c in comments):
        write_api(token, f"/issues/{number}/comments", "POST", {"body": message})
    write_api(token, f"/issues/{number}", "PATCH", {"state": "closed", "state_reason": "completed"})


def publish_one(token, number, dry_run=False):
    data = accepted(token, number)
    if data is None:
        return False
    candidate, expected = data["candidate_sha"], data["expected_main_sha"]
    current, preview = ref(token, "main"), ref(token, "preview")
    with TemporaryDirectory(prefix="macci-main-") as folder:
        m.git(folder, "init", "--bare")
        m.git(folder, "fetch", "--no-tags", m.REMOTE,
              "refs/heads/main:refs/macci/main",
              "refs/heads/preview:refs/macci/preview")
        if (m.git(folder, "rev-parse", "refs/macci/main") != current
                or m.git(folder, "rev-parse", "refs/macci/preview") != preview):
            return False
        # Also recover if the push succeeded earlier but the receipt update failed.
        if not ancestor(folder, candidate, preview):
            return False
        if ancestor(folder, candidate, current):
            if not dry_run and ref(token, "main") == current and accepted(token, number) == data:
                acknowledge(token, number, candidate, current)
                return True
            return False
        if current != expected or not ancestor(folder, expected, candidate):
            print(f"Acceptance #{number}: main baseline changed; no release", flush=True)
            return False
        if not evidence(token, data):
            print(f"Acceptance #{number}: test/PR evidence missing or stale", flush=True)
            return False
        if m.git(folder, "show", "-s", "--format=%T", candidate) != data["tree"]:
            return False
        notes = json.loads(m.git(folder, "show", candidate + ":app/release_notes.json"))
        if notes[0]["version"] != data["version"] or notes[0].get("acceptance_required") is not True:
            return False
        if (accepted(token, number) != data or ref(token, "main") != expected
                or ref(token, "preview") != preview or not evidence(token, data)):
            return False
        if dry_run:
            print(f"Acceptance #{number}: dry run verified {candidate}; no push", flush=True)
            return False
        # Fixed destination, exact accepted commit, never force. GitHub rules apply.
        m.git(folder, "push", "--porcelain", m.REMOTE, candidate + ":refs/heads/main")
        actual = ref(token, "main")
        if actual != candidate:
            print(f"Acceptance #{number}: main changed after push; reconcile next tick", flush=True)
            return True
        print(f"Acceptance #{number}: published main {actual}", flush=True)
        acknowledge(token, number, candidate, actual)
        return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        token = m.read_token()
        with (m.ROOT / "main-publish.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            issues = m.all_pages(token, "/issues?state=open&sort=created&direction=asc")
            for issue in issues:
                if parse(issue) is None:
                    continue
                number = issue["number"]
                try:
                    if publish_one(token, number, args.dry_run):
                        return 0
                except (OSError, URLError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
                    print(f"Acceptance #{number}: blocked: {type(exc).__name__}", flush=True)
            return 0
    except (OSError, URLError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"Main publisher blocked: {type(exc).__name__}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
