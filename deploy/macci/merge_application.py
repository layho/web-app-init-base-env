"""Host-only, fail-closed application main coordinator. No web-exposed mutation.

Separate from the base preview publisher. A repository must be in the trusted
MacCI allowlist and explicitly selected by the trusted host administrator.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from urllib.request import Request, build_opener
import merge_preview as base
import runner

OWNER = "layho"
CONTEXT = "macmini/isolated-tests-v1"
MARKER = "[APP_MAIN_REVIEW] "
SHA = re.compile(r"[0-9a-f]{40}\Z")
ROOT = Path.home() / ".local/share/macmini-pr-tests"
# The foundation uses preview-only release and must never enter this executor.
ALLOWED = {repo for repo, branches in runner.REPOS.items()
           if repo != base.REPO and branches == {"main"}}


def valid(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def api(token, repo, path):
    if repo not in ALLOWED or not path.startswith("/") or "//" in path:
        raise ValueError("Application GitHub API scope rejected")
    request = Request("https://api.github.com/repos/" + repo + path,
                      headers={"Authorization": "Bearer " + token,
                               "Accept": "application/vnd.github+json",
                               "User-Agent": "macci-app-main-merge"})
    with build_opener(base.NoRedirect()).open(request, timeout=20) as stream:
        return json.load(stream)


def pages(token, repo, path):
    result = []
    for page in range(1, 11):
        response = api(token, repo, path + ("&" if "?" in path else "?")
                       + f"per_page=100&page={page}")
        if not isinstance(response, list):
            raise ValueError("Invalid GitHub page")
        result.extend(response)
        if len(response) < 100:
            return result
    raise ValueError("Review history pagination exceeded; fail closed")


def eligible(repo, pr):
    return bool(repo in ALLOWED and pr.get("state") == "open"
                and pr.get("draft") is False and pr.get("mergeable") is True
                and pr.get("mergeable_state") == "clean"
                and pr.get("author_association") == "OWNER"
                and (pr.get("user") or {}).get("login") == OWNER
                and (pr.get("base") or {}).get("ref") == "main"
                and ((pr.get("base") or {}).get("repo") or {}).get("full_name") == repo
                and ((pr.get("head") or {}).get("repo") or {}).get("full_name") == repo
                and type(pr.get("number")) is int and pr["number"] > 0
                and all(valid((pr.get(side) or {}).get("sha"))
                        for side in ("head", "base"))
                and valid(pr.get("merge_commit_sha")))


def review(comments, head, current_base):
    """Newest owner exact-head decision wins; revocation invalidates approval."""
    for comment in sorted(comments, key=lambda c: c.get("id", 0), reverse=True):
        if (comment.get("author_association") != "OWNER"
                or (comment.get("user") or {}).get("login") != OWNER):
            continue
        body = comment.get("body") or ""
        if not body.startswith(MARKER) or "\n" in body:
            continue
        try:
            data = json.loads(body[len(MARKER):])
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("head") != head:
            continue
        if (set(data) != {"schema", "head", "decision", "base"}
                or type(data["schema"]) is not int or data["schema"] != 1
                or data["decision"] != "approved" or data["base"] != current_base):
            return False
        return True
    return False


def authorized(token, repo, number):
    if repo not in ALLOWED or type(number) is not int or number <= 0:
        return None
    pr = api(token, repo, f"/pulls/{number}")
    if not eligible(repo, pr):
        return None
    head, candidate = pr["head"]["sha"], pr["merge_commit_sha"]
    current = api(token, repo, "/branches/main")["commit"]["sha"]
    if not valid(current):
        return None
    commit = api(token, repo, "/git/commits/" + candidate)
    if ([x.get("sha") for x in commit.get("parents", [])] != [current, head]
            or not valid((commit.get("tree") or {}).get("sha"))):
        return None
    comments = pages(token, repo, f"/issues/{number}/comments")
    if not review(comments, head, current):
        return None
    statuses = api(token, repo, "/commits/" + head + "/statuses?per_page=100")
    # Latest result in context must have owner identity and exact candidate
    if not base.tested(statuses, head, current, candidate):
        return None
    if api(token, repo, "/branches/main")["commit"]["sha"] != current:
        return None
    return head, current, candidate, commit["tree"]["sha"]


def merge_one(token, repo, number, dry_run=False):
    gate = authorized(token, repo, number)
    if gate is None:
        return False
    head, current, candidate, tree = gate
    remote = f"git@github.com:{repo}.git"  # repo is hard-allowlisted
    with TemporaryDirectory(prefix="macci-application-") as folder:
        base.git(folder, "init", "--bare")
        base.git(folder, "fetch", "--no-tags", remote,
                 f"refs/pull/{number}/merge:refs/macci/tested",
                 "refs/heads/main:refs/macci/main")
        if (base.git(folder, "show", "-s", "--format=%H %P",
                     "refs/macci/tested").split() != [candidate, current, head]
                or base.git(folder, "rev-parse", "refs/macci/main") != current
                or base.git(folder, "show", "-s", "--format=%T", candidate) != tree):
            return False
        if authorized(token, repo, number) != gate:
            return False
        if dry_run:
            return True
        # Never force; no arbitrary target, no deployments or credentials copied.
        base.git(folder, "push", "--porcelain", remote,
                 candidate + ":refs/heads/main")
        actual = api(token, repo, "/branches/main")["commit"]["sha"]
        return actual == candidate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, choices=sorted(ALLOWED))
    parser.add_argument("--pr", type=int, help="Optional exact PR for a host-triggered one-shot")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.pr is not None and args.pr <= 0:
        parser.error("--pr must be positive")
    token = base.read_token()
    lockfile = ROOT / ("app-main-" + args.repo.replace("/", "--") + ".lock")
    with lockfile.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        prs = ([api(token, args.repo, f"/pulls/{args.pr}")] if args.pr is not None
               else pages(token, args.repo, "/pulls?state=open&sort=created&direction=asc"))
        for pr in prs:
            if eligible(args.repo, pr) and merge_one(token, args.repo,
                                                     pr["number"], args.dry_run):
                return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
