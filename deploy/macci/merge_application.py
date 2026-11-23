"""Host-only, fail-closed application main coordinator. No web-exposed mutation.

Separate from the base preview publisher. A repository must be in the trusted
MacCI allowlist and explicitly selected by the trusted host administrator.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory, NamedTemporaryFile
from urllib.request import Request, build_opener
import merge_preview as base
import runner

OWNER = "layho"
CONTEXT = "macmini/isolated-tests-v1"
MARKER = "[APP_MAIN_REVIEW] "
SHA = re.compile(r"[0-9a-f]{40}\Z")
ROOT = Path.home() / ".local/share/macmini-pr-tests"
# Explicit opt-in. A repo being in the MacCI *test* allowlist is not
# enough to authorize publishing it. TI Hunter is legacy and stays excluded.
APP_MAIN_OPT_IN = frozenset({"layho/macmini-office-app-panel"})
ALLOWED = {repo for repo in APP_MAIN_OPT_IN
           if repo != base.REPO and runner.REPOS.get(repo) == {"main"}}


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


def merge_receipt(repo, number, head, old_main, new_main, tree):
    """Durable evidence of GitHub main merge; *never* a deployment receipt."""
    if (repo not in ALLOWED or type(number) is not int or number <= 0
            or not all(valid(x) for x in (head, old_main, new_main, tree))):
        raise ValueError("Invalid merge receipt scope or SHA")
    root = ROOT / "app-main-receipts"
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    import stat
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise ValueError("Unsafe receipt directory")
    name = root / (repo.replace("/", "--") + f"--{number}--" + new_main + ".json")
    record = {
        "schema": 1, "repository": repo, "pr": number,
        "head_sha": head, "previous_main_sha": old_main,
        "merged_main_sha": new_main, "tree_sha": tree,
        "merge_state": "github_main_verified",
        "deployment_state": "unverified",
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    if name.exists() or name.is_symlink():
        raise ValueError("Receipt already exists: refuse destructive overwrite")
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=root,
                            prefix=".app-main-", delete=False) as file:
        temp = Path(file.name)
        try:
            os.fchmod(file.fileno(), 0o600)
            json.dump(record, file, ensure_ascii=False, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
            file.close()
            # hard link is exclusive and never clobbers another receipt
            os.link(temp, name)
        finally:
            temp.unlink(missing_ok=True)
    return name


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
        if actual != candidate:
            return False
        merge_receipt(repo, number, head, current, candidate, tree)
        return True


def reconcile_one(token, repo, number, dry_run=False):
    """Recover a missing receipt only for a historically verified exact merge.

    A closed PR is not sufficient evidence. Verify OWNER approval, exact MacCI
    success, immutable merge parents/tree and main branch ancestry first.
    """
    if repo not in ALLOWED or type(number) is not int or number <= 0:
        return False
    pr = api(token, repo, f"/pulls/{number}")
    if (pr.get("state") != "closed" or pr.get("merged") is not True
            or pr.get("author_association") != "OWNER"
            or (pr.get("user") or {}).get("login") != OWNER
            or (pr.get("base") or {}).get("ref") != "main"
            or ((pr.get("base") or {}).get("repo") or {}).get("full_name") != repo
            or ((pr.get("head") or {}).get("repo") or {}).get("full_name") != repo):
        return False
    head, candidate = (pr.get("head") or {}).get("sha"), pr.get("merge_commit_sha")
    if not valid(head) or not valid(candidate):
        return False
    commit = api(token, repo, "/git/commits/" + candidate)
    parents = [x.get("sha") for x in commit.get("parents", [])]
    tree = (commit.get("tree") or {}).get("sha")
    if len(parents) != 2 or parents[1] != head or not all(valid(x) for x in parents) or not valid(tree):
        return False
    prior = parents[0]
    if not review(pages(token, repo, f"/issues/{number}/comments"), head, prior):
        return False
    if not base.tested(api(token, repo, "/commits/" + head + "/statuses?per_page=100"),
                       head, prior, candidate):
        return False
    main = api(token, repo, "/branches/main")["commit"]["sha"]
    if not valid(main):
        return False
    remote = f"git@github.com:{repo}.git"
    with TemporaryDirectory(prefix="macci-app-reconcile-") as folder:
        base.git(folder, "init", "--bare")
        base.git(folder, "fetch", "--no-tags", remote, "refs/heads/main:refs/macci/main")
        if base.git(folder, "rev-parse", "refs/macci/main") != main:
            return False
        # Require the actual tested commit to belong to published main history;
        # GitHub's 'merged' flag alone is not proof of this exact tree.
        base.git(folder, "merge-base", "--is-ancestor", candidate, main)
        if (base.git(folder, "show", "-s", "--format=%H %P", candidate).split()
                != [candidate, prior, head]
                or base.git(folder, "show", "-s", "--format=%T", candidate) != tree):
            return False
        # Do not write if GitHub's immutable evidence changes during the check.
        latest = api(token, repo, f"/pulls/{number}")
        if (latest.get("merged") is not True
                or latest.get("merge_commit_sha") != candidate
                or (latest.get("head") or {}).get("sha") != head
                or api(token, repo, "/branches/main")["commit"]["sha"] != main):
            return False
        if dry_run:
            return True
        merge_receipt(repo, number, head, prior, candidate, tree)
        return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, choices=sorted(ALLOWED))
    parser.add_argument("--pr", type=int, help="Optional exact PR for a host-triggered one-shot")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reconcile-pr", type=int, help="Rebuild a missing receipt after trusted merged-PR checks")
    args = parser.parse_args()
    if args.pr is not None and args.pr <= 0:
        parser.error("--pr must be positive")
    if args.reconcile_pr is not None and (args.reconcile_pr <= 0 or args.pr is not None):
        parser.error("--reconcile-pr must be positive and exclusive of --pr")
    token = base.read_token()
    lockfile = ROOT / ("app-main-" + args.repo.replace("/", "--") + ".lock")
    with lockfile.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        if args.reconcile_pr is not None:
            return 0 if reconcile_one(token, args.repo, args.reconcile_pr, args.dry_run) else 1
        prs = ([api(token, args.repo, f"/pulls/{args.pr}")] if args.pr is not None
               else pages(token, args.repo, "/pulls?state=open&sort=created&direction=asc"))
        for pr in prs:
            if eligible(args.repo, pr) and merge_one(token, args.repo,
                                                     pr["number"], args.dry_run):
                return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
