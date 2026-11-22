"""Plan/create a draft foundation upgrade PR from accepted main, never merge.

Run on a trusted development host, not inside the application web process.
No writes to the installed application, launchd, or business database.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import stat
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
import sys
from tempfile import TemporaryDirectory

import upgrade_plan as planner

FOUNDATION = "layho/web-app-init-base-env"
SOURCE_URL = "https://github.com/" + FOUNDATION + ".git"
SHA = re.compile(r"[0-9a-f]{40}\Z")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,127}\Z")
VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\Z")
PARAMS = ("repository", "domain", "port", "label", "title")
RENDER = """import runpy,sys,json
from pathlib import Path
source,target,params=sys.argv[1:]
m=runpy.run_path(str(Path(source)/'deploy/app-init/app_init.py'))
p=json.loads(params)
m['copy_foundation'](Path(source),Path(target),p['repository'],p['title'],p['domain'],p['port'],p['label'])
"""


def run(args, *, data=None, timeout=120, env=None):
    proc = subprocess.run(args, input=data, capture_output=True, timeout=timeout,
                          env=env, check=True)
    return proc.stdout


def git(root, *args, data=None, env=None):
    return run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],
               data=data, env=env)


def line(root, *args):
    return git(root, *args).decode().strip()


def remote_ref(url, ref):
    output = run(["git", "ls-remote", "--refs", url, ref]).decode().splitlines()
    if not output:
        return None
    if len(output) != 1:
        raise ValueError("Unexpected remote refs")
    sha, actual = output[0].split()
    if not SHA.fullmatch(sha) or actual != ref:
        raise ValueError("Invalid remote ref")
    return sha


def blob(root, sha, path):
    if not SHA.fullmatch(sha):
        raise ValueError("Unpinned commit")
    mode, kind, oid = git(root, "ls-tree", sha, "--", path).decode().split("\t", 1)[0].split()
    if kind != "blob" or mode not in ("100644", "100755"):
        raise ValueError("Not a regular tracked file")
    if int(line(root, "cat-file", "-s", oid)) > planner.MAX_FILE:
        raise ValueError("Oversized file")
    return git(root, "cat-file", "blob", oid)


def metadata(root, sha):
    value = json.loads(blob(root, sha, ".app-init.json"))
    if not isinstance(value, dict):
        raise ValueError("Invalid app metadata")
    return value


def validate_params(meta, repository):
    p = {k: meta[k] for k in PARAMS}
    if (repository == FOUNDATION or not REPO.fullmatch(repository)
            or p["repository"] != repository or not isinstance(p["domain"], str)
            or not DOMAIN.fullmatch(p["domain"])
            or type(p["port"]) is not int or not 1024 <= p["port"] <= 65535
            or not isinstance(p["label"], str) or not LABEL.fullmatch(p["label"])
            or not isinstance(p["title"], str) or not 1 <= len(p["title"]) <= 100
            or any(ord(c) < 32 for c in p["title"])):
        raise ValueError("Invalid or mismatched app generation parameters")
    return p


def release(root, sha):
    version = json.loads(blob(root, sha, "app/release_notes.json"))[0]["version"]
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError("Invalid foundation release")
    return {"schema": 1, "repository": FOUNDATION, "sha": sha, "version": version}


def verify_source(root, old, target):
    if not SHA.fullmatch(old) or not SHA.fullmatch(target):
        raise ValueError("Invalid source SHA")
    # Restrict both versions to the official main first-parent history, not
    # arbitrary preview or feature-branch ancestors merged into main.
    official = line(root, "rev-parse", "refs/remotes/origin/main")
    if target != official:
        raise ValueError("Upgrade target is not the fetched official main")
    history = line(root, "rev-list", "--first-parent", target).splitlines()
    if old not in history:
        raise ValueError("Old foundation is not in the official main history")
    return release(root, old), release(root, target)


def render(root, sha, target, params, folder):
    checkout = folder / ("source-" + sha)
    git(root, "worktree", "add", "--detach", str(checkout), sha)
    try:
        target.mkdir()
        run([sys.executable, "-I", "-B", "-c", RENDER, str(checkout), str(target),
             json.dumps(params, ensure_ascii=False)], timeout=90)
    finally:
        git(root, "worktree", "remove", "--force", str(checkout))


def legacy_check(app, app_sha, old_generated, repository, params):
    """Adopt only a reproducible original initialization, never a guessed SHA."""
    roots = line(app, "rev-list", "--max-parents=0", app_sha).splitlines()
    if len(roots) != 1:
        raise ValueError("Legacy history is ambiguous")
    root = roots[0]
    entries = git(app, "ls-tree", "-rz", root).split(b"\0")
    generated = {p.relative_to(old_generated).as_posix(): p.read_bytes()
                 for p in old_generated.rglob("*") if p.is_file()}
    original = {}
    for entry in entries:
        if not entry:
            continue
        fields, raw_path = entry.split(b"\t", 1)
        mode, kind, oid = fields.decode().split()
        path = raw_path.decode()
        if mode not in ("100644", "100755") or kind != "blob" or path not in generated:
            raise ValueError("Legacy initialization contains unexpected files")
        if int(line(app, "cat-file", "-s", oid)) > planner.MAX_FILE:
            raise ValueError("Oversized legacy blob")
        original[path] = git(app, "cat-file", "blob", oid)
    if original != generated:
        raise ValueError("Legacy initialization does not exactly reproduce")
    initial = json.loads(original[".app-init.json"])
    initial.setdefault("title", json.loads(original["app/project_info.json"])["name"])
    if validate_params(initial, repository) != params:
        raise ValueError("Legacy generation parameters changed")
    return root


def create_commit(app, app_sha, decisions, meta, target):
    if any(d.action not in ("add", "replace", "merge") or d.result is None for d in decisions):
        raise ValueError("Unresolved plan; refusing to create commit")
    # Isolated Git index: no materialization of app code or working tree writes.
    with TemporaryDirectory(prefix="base-upgrade-index-") as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
        git(app, "read-tree", app_sha, env=env)
        files = {d.path: d.result for d in decisions}
        updated = dict(meta, foundation=target)
        files[".app-init.json"] = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode()
        for path, content in files.items():
            oid = git(app, "hash-object", "-w", "--stdin", data=content).decode().strip()
            current = git(app, "ls-tree", app_sha, "--", path).decode()
            mode = current.split()[0] if current else "100644"
            if mode not in ("100644", "100755"):
                raise ValueError("Unsafe app file mode")
            git(app, "update-index", "--add", "--cacheinfo", f"{mode},{oid},{path}", env=env)
        tree = git(app, "write-tree", env=env).decode().strip()
        return git(app, "-c", "user.name=Foundation Upgrade", "-c",
                   "user.email=app-init@localhost", "commit-tree", tree,
                   "-p", app_sha, "-m", "chore: upgrade foundation to " + target["sha"], env=env).decode().strip()


TOKEN_FILE = Path.home() / ".local/share/macmini-foundation-upgrades/github-token"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def valid_token(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_]{20,300}", value) is not None


def read_token():
    fd = os.open(TOKEN_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="ascii") as f:
        info = os.fstat(f.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 512):
            raise ValueError("Unsafe scheduler token file")
        token = f.read().strip()
    if not valid_token(token):
        raise ValueError("Invalid scheduler token")
    return token


def github(path, *, query=None, payload=None, token=None):
    if not (path == "/user" or re.fullmatch(r"/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pulls", path)):
        raise ValueError("Unsupported GitHub API path")
    token = read_token() if token is None else token
    if not valid_token(token):
        raise ValueError("Invalid scheduler token")
    url = "https://api.github.com" + path
    if query:
        url += "?" + urlencode(query)
    req = Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                  headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                           "User-Agent": "foundation-upgrade-scheduler", "Content-Type": "application/json"})
    try:
        with build_opener(NoRedirect()).open(req, timeout=20) as response:
            return json.loads(response.read(8 * 1024 * 1024 + 1))
    except HTTPError as exc:
        raise ValueError("GitHub API returned HTTP " + str(exc.code)) from None
    except (URLError, OSError, ValueError):
        raise ValueError("GitHub API request failed") from None


def pull_rows(repository, *, state="open", branch=None, token=None):
    if not REPO.fullmatch(repository):
        raise ValueError("Invalid PR repository")
    query = {"base": "main", "state": state, "per_page": 100}
    if branch:
        query["head"] = repository.split("/")[0] + ":" + branch
    rows = []
    for page in range(1, 11):
        data = github("/repos/" + repository + "/pulls",
                      query={**query, "page": page}, token=token)
        if not isinstance(data, list) or len(data) > 100:
            raise ValueError("Invalid PR list")
        for item in data:
            head = item.get("head") or {}
            repo = (head.get("repo") or {}).get("full_name")
            if repo != repository or (branch and head.get("ref") != branch):
                continue
            rows.append({"number": item["number"],
                         "state": "MERGED" if item.get("merged_at") else item["state"].upper(),
                         "url": item["html_url"], "headRefName": head["ref"],
                         "baseRefName": item["base"]["ref"]})
        if len(data) < 100:
            return rows
    raise ValueError("PR list incomplete")


def prs(repository, branch):
    return pull_rows(repository, state="all", branch=branch)


def reuse_or_publish(app, repository, app_url, app_sha, target, commit, report):
    branch = "base-upgrade/" + target["sha"]
    full_ref = "refs/heads/" + branch
    existing = prs(repository, branch)
    if existing:
        if len(existing) != 1:
            raise ValueError("Ambiguous previous upgrade PRs")
        return {"state": "existing_pr", "pr": existing[0]}
    if remote_ref(SOURCE_URL, "refs/heads/main") != target["sha"]:
        raise ValueError("Official foundation advanced; replan")
    if remote_ref(app_url, "refs/heads/main") != app_sha:
        raise ValueError("Application main advanced; replan")
    # Avoid stacking different target upgrade PRs, including a new one created during planning.
    open_rows = pull_rows(repository)
    if any(p.get("headRefName", "").startswith("base-upgrade/") for p in open_rows):
        raise ValueError("Another upgrade is pending")
    prior = remote_ref(app_url, full_ref)
    if prior is not None:
        # Recover a previously pushed branch when the PR creation was interrupted.
        git(app, "fetch", "--no-tags", app_url, full_ref)
        if line(app, "rev-parse", "FETCH_HEAD") != prior:
            raise ValueError("Upgrade branch changed; inspect existing branch")
        if (line(app, "show", "-s", "--format=%P", prior) != app_sha
                or line(app, "rev-parse", prior + "^{tree}") != line(app, "rev-parse", commit + "^{tree}")):
            raise ValueError("Existing upgrade branch differs; never overwrite")
    else:
        git(app, "push", app_url, commit + ":" + full_ref)
    body = ("Foundation upgrade from verified official main. Draft only; requires app review and regression.\n\n"
            + json.dumps(report, ensure_ascii=False, indent=2))
    result = github("/repos/" + repository + "/pulls", payload={
        "title": "chore: upgrade foundation v" + target["version"],
        "head": branch, "base": "main", "draft": True, "body": body,
    })
    url = result["html_url"]
    if not re.fullmatch(r"https://github\.com/" + re.escape(repository) + r"/pull/[1-9]\d*", url):
        raise ValueError("Invalid created PR URL")
    return {"state": "draft_created", "url": url}



def coordinate(repository, legacy_base=None, create_pr=False, *, expected_target=None, expected_app=None):
    if not REPO.fullmatch(repository) or repository.lower() == FOUNDATION.lower():
        raise ValueError("Invalid downstream repository")
    app_url = "git@github.com:" + repository + ".git"
    with TemporaryDirectory(prefix="foundation-upgrade-") as tmp:
        folder = Path(tmp)
        source, app = folder / "foundation", folder / "app"
        run(["git", "clone", "--no-checkout", "--single-branch", "--branch", "main", SOURCE_URL, str(source)])
        run(["git", "clone", "--no-checkout", "--single-branch", "--branch", "main", app_url, str(app)])
        app_sha = line(app, "rev-parse", "refs/remotes/origin/main")
        target_sha = line(source, "rev-parse", "refs/remotes/origin/main")
        if (expected_target is not None and target_sha != expected_target) or (expected_app is not None and app_sha != expected_app):
            raise ValueError("Pinned source or application changed; replan")
        meta = metadata(app, app_sha)
        registered = meta.get("foundation")
        if registered:
            if legacy_base is not None or not isinstance(registered, dict):
                raise ValueError("Conflicting registered provenance")
            old_sha = registered["sha"]
            params = validate_params(meta, repository)
        else:
            if not legacy_base:
                raise ValueError("Unregistered foundation; supply verified legacy base")
            old_sha = legacy_base
            roots = line(app, "rev-list", "--max-parents=0", app_sha).splitlines()
            if len(roots) != 1:
                raise ValueError("Legacy history ambiguous")
            initial = json.loads(blob(app, roots[0], "app/project_info.json"))
            meta.setdefault("title", initial["name"])
            params = validate_params(meta, repository)
        old, target = verify_source(source, old_sha, target_sha)
        if registered and registered != old:
            raise ValueError("Registered source version does not match official history")
        if old == target and registered:
            return {"state": "current", "app_sha": app_sha, "foundation": target}
        old_dir, new_dir = folder / "old", folder / "new"
        render(source, old_sha, old_dir, params, folder)
        legacy_root = None if registered else legacy_check(app, app_sha, old_dir, repository, params)
        render(source, target_sha, new_dir, params, folder)
        before, after = planner.generated_snapshot(old_dir), planner.generated_snapshot(new_dir)
        actual, current = planner.tracked_snapshot(app, app_sha, before.keys() | after.keys())
        decisions = planner.plan(before, after, current)
        summary = {"schema": 1, "app_sha": actual, "from": old, "target": target,
                   "legacy_verified_root": legacy_root,
                   "changes": [d.summary() for d in decisions],
                   "blocked": any(d.action in ("conflict", "review") for d in decisions)}
        if summary["blocked"] or not create_pr:
            return {"state": "blocked" if summary["blocked"] else "planned", **summary}
        commit = create_commit(app, app_sha, decisions, meta, target)
        result = reuse_or_publish(app, repository, app_url, app_sha, target, commit, summary)
        return {**summary, **result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--legacy-base", help="Exact official source SHA reproducing initial app")
    parser.add_argument("--create-pr", action="store_true", help="Publish only a draft PR when plan is clean")
    args = parser.parse_args()
    try:
        print(json.dumps(coordinate(args.repo, args.legacy_base, args.create_pr), ensure_ascii=False, indent=2))
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        # Never echo credentials, raw command output, or private file contents.
        print("Upgrade stopped: " + (str(exc) if type(exc) is ValueError else type(exc).__name__), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
