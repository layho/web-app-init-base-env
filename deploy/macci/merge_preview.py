"""Trusted base-only preview merge coordinator; separate from Docker tests.

No main publishing, arbitrary repository/branch arguments, or forced push.
Owner review or an explicit host administrator path policy authorizes preview.
"""
import argparse
import base64
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from tempfile import TemporaryDirectory
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import URLError

REPO = "layho/web-app-init-base-env"
OWNER = "layho"
TARGET = "refs/heads/preview"
REMOTE = "git@github.com:layho/web-app-init-base-env.git"
ROOT = Path.home() / ".local/share/macmini-pr-tests"
SHA = re.compile(r"[0-9a-f]{40}\Z")
CONTEXT = "macmini/isolated-tests-v1"
MARKER = "[BASE_PREVIEW_REVIEW] "


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def valid(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def api(token, path):
    request = Request("https://api.github.com/repos/" + REPO + path,
                      headers={"Authorization": "Bearer " + token,
                               "Accept": "application/vnd.github+json", "User-Agent": "macci-preview-merge"})
    with build_opener(NoRedirect()).open(request, timeout=20) as response:
        return json.load(response)


def eligible(pr):
    return (pr.get("state") == "open" and not pr.get("draft")
            and pr.get("mergeable") is True and pr.get("mergeable_state") in {"clean", "unstable"}
            and pr.get("author_association") == "OWNER"
            and pr.get("user", {}).get("login") == OWNER
            and pr.get("base", {}).get("ref") == "preview"
            and pr.get("base", {}).get("repo", {}).get("full_name") == REPO
            and (pr.get("head", {}).get("repo") or {}).get("full_name") == REPO
            and type(pr.get("number")) is int and pr["number"] > 0
            and all(valid(pr.get(side, {}).get("sha")) for side in ("head", "base"))
            and valid(pr.get("merge_commit_sha")))


def review(comments, head):
    """Newest exact-head owner authorization or revocation wins."""
    for comment in sorted(comments, key=lambda c: c["id"], reverse=True):
        if comment.get("author_association") != "OWNER" or comment.get("user", {}).get("login") != OWNER:
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
        if set(data) != {"schema", "head", "decision", "requires_ancestor"} or data.get("schema") != 1:
            return None
        if data.get("decision") != "approved" or not valid(data.get("requires_ancestor")):
            return None
        return data["requires_ancestor"]
    return None


def owner_decision_present(comments, head):
    """A recorded exact-head owner decision takes precedence over delegation."""
    for item in comments:
        if item.get("author_association") != "OWNER" or (item.get("user") or {}).get("login") != OWNER:
            continue
        body = item.get("body") or ""
        if not body.startswith(MARKER):
            continue
        try:
            data = json.loads(body[len(MARKER):])
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("head") == head:
            return True
    return False


AUTO_POLICY = Path("/usr/local/etc/macmini-pr-tests/preview-auto-paths.json")


def automatic_paths():
    """Explicit operator opt-in, outside the candidate and its writable home."""
    for directory in (Path("/usr/local"), AUTO_POLICY.parent.parent, AUTO_POLICY.parent):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            return frozenset()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("Unsafe automatic policy directory")
    try:
        fd = os.open(AUTO_POLICY, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return frozenset()
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o222 or info.st_size > 8192:
            raise ValueError("Unsafe automatic policy")
        data = json.loads(stream.read(8193))
    if not isinstance(data, dict) or set(data) != {"schema", "paths"} or type(data["schema"]) is not int or data["schema"] != 1:
        raise ValueError("Invalid automatic policy")
    paths = data["paths"]
    if not isinstance(paths, list) or len(paths) > 100 or any(not isinstance(p, str) or not safe_auto_path(p) for p in paths):
        raise ValueError("Invalid automatic path list")
    return frozenset(paths)


def safe_auto_path(path):
    # Deliberately narrow first rollout: documentation and presentation-only
    # styles. No scripts, dependencies, tests, authentication, or host runtime.
    return bool(re.fullmatch(r"(?:docs/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\.md|frontend/src/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\.css)", path))


def automatic_diff_allowed(folder, base, merge, allowed):
    if not allowed:
        return False
    # Exact tested merge tree against its first parent, including renames,
    # symlinks, mode changes and deletions. Only ordinary modified blobs.
    raw = git(folder, "diff-tree", "--no-commit-id", "--raw", "-r", "--no-renames",
              "--no-abbrev", base, merge)
    if not raw:
        return False
    for line in raw.splitlines():
        match = re.fullmatch(r":100644 100644 [0-9a-f]{40} [0-9a-f]{40} M\t([^\t]+)", line)
        if not match or match.group(1) not in allowed or not safe_auto_path(match.group(1)):
            return False
    return True


def tested(statuses, head, base, merge):
    if not all(valid(x) for x in (head, base, merge)):
        return False
    latest = next((s for s in statuses if s.get("context") == CONTEXT), None)
    return bool(latest and latest.get("creator", {}).get("login") == OWNER
                and latest.get("state") == "success"
                and latest.get("description") == f"base={base} merge={merge} passed")


def advisory_only(statuses, base, merge):
    latest = {}
    for item in statuses:
        context = item.get("context")
        if not isinstance(context, str) or not context:
            return False
        latest.setdefault(context, item)
    for context, item in latest.items():
        if item.get("state") == "success":
            continue
        if (context != "macmini/progress-v1" or item.get("state") != "pending"
                or (item.get("creator") or {}).get("login") != OWNER
                or not re.fullmatch(r"base=" + base + r" merge=" + merge + r" (setup|fetch|context|dependencies|test|cleanup)", item.get("description") or "")):
            return False
    return True


def all_pages(token, path):
    result = []
    for page in range(1, 11):
        batch = api(token, path + ("&" if "?" in path else "?") + f"per_page=100&page={page}")
        if not isinstance(batch, list):
            raise ValueError("Invalid GitHub list")
        result.extend(batch)
        if len(batch) < 100:
            return result
    # Refuse truncated histories: an unseen newer revocation must not be ignored.
    raise ValueError("GitHub pagination limit exceeded")


def latest_release(token, sha):
    """Read version from the exact immutable commit, never a moving ref."""
    if not valid(sha):
        raise ValueError("Invalid release commit")
    response = api(token, "/contents/app/release_notes.json?ref=" + sha)
    if not isinstance(response, dict) or response.get("encoding") != "base64":
        raise ValueError("Invalid release notes response")
    encoded = response.get("content")
    if not isinstance(encoded, str) or len(encoded) > 700_000:
        raise ValueError("Invalid release notes length")
    notes = json.loads(base64.b64decode(encoded, validate=False))
    if not isinstance(notes, list) or not notes or not isinstance(notes[0], dict):
        raise ValueError("Invalid release notes")
    version = notes[0].get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Invalid release version")
    required = notes[0].get("acceptance_required")
    if type(required) is not bool:
        raise ValueError("Invalid release acceptance flag")
    return tuple(map(int, version.split("."))), required


def published_preview_equivalent(read, preview, main):
    """Recognize an exact, officially merged preview-to-main release.

    Equal trees alone are not publication evidence. Only an actual GitHub
    release merge with the preview as a parent and matching PR is accepted.
    A missing or inconsistent receipt leaves the candidate frozen.
    """
    if not all(isinstance(x, str) and SHA.fullmatch(x) for x in (preview, main)):
        return False
    if preview == main:
        return True
    published = read("/git/commits/" + main)
    candidate = read("/git/commits/" + preview)
    if not isinstance(published, dict) or not isinstance(candidate, dict):
        return False
    if published.get("sha") != main or candidate.get("sha") != preview:
        return False
    tree = (candidate.get("tree") or {}).get("sha")
    parents = published.get("parents")
    if (not isinstance(tree, str) or not SHA.fullmatch(tree)
            or (published.get("tree") or {}).get("sha") != tree
            or not isinstance(parents, list) or len(parents) != 2
            or not all(isinstance(p, dict) for p in parents)
            or parents[1].get("sha") != preview):
        return False
    message = published.get("message")
    match = re.match(r"Merge pull request #([1-9][0-9]{0,8}) from layho/preview(?:\\n|$)",
                     message) if isinstance(message, str) else None
    if not match:
        return False
    release = read("/pulls/" + match.group(1))
    if not isinstance(release, dict):
        return False
    source, target = release.get("head") or {}, release.get("base") or {}
    return (release.get("merged") is True and release.get("state") == "closed"
            and release.get("merge_commit_sha") == main
            and source.get("sha") == preview and source.get("ref") == "preview"
            and target.get("ref") == "main"
            and (source.get("repo") or {}).get("full_name") == REPO
            and (target.get("repo") or {}).get("full_name") == REPO)


def preview_frozen(token):
    """Freeze when preview's release version is newer than official main.

    Fail closed on inconsistent or unreachable refs / release metadata.
    Automatic unfreeze occurs only after main reaches the candidate version.
    """
    preview = api(token, "/branches/preview")["commit"]["sha"]
    main = api(token, "/branches/main")["commit"]["sha"]
    if not valid(preview) or not valid(main):
        raise ValueError("Invalid branch SHA")
    preview_version, requires_acceptance = latest_release(token, preview)
    main_version, _ = latest_release(token, main)
    if preview_version < main_version:
        raise ValueError("Preview release version older than main")
    if preview_version > main_version:
        print(f"Preview frozen for candidate v{'.'.join(map(str, preview_version))}; main is v{'.'.join(map(str, main_version))}", flush=True)
        return True
    # The web gate offers acceptance for the running SHA, including revisions
    # that retain the same release number and release notes.
    if not requires_acceptance or preview == main:
        return False
    if not published_preview_equivalent(lambda path: api(token, path), preview, main):
        return True
    # The caller still verifies the candidate, OWNER review and test receipt.
    return (api(token, "/branches/preview")["commit"]["sha"] != preview
            or api(token, "/branches/main")["commit"]["sha"] != main)


def deployment_replacement_allowed(token, number, base, head, merge, main):
    """Only a real failed deployment with successful rollback may be replaced.
    Never synthesize user rejection or acceptance.
    """
    statuses = api(token, "/commits/" + base + "/statuses?per_page=100")
    receipt = next((s for s in statuses if s.get("context") == "macmini/deployment-v1"), None)
    if not receipt or receipt.get("state") != "failure" or (receipt.get("creator") or {}).get("login") != OWNER:
        return False
    description = receipt.get("description") or ""
    receipt_match = re.fullmatch(
        r"(?:update_test_build_failed|update_restart_failed|update_dependencies_failed|host_test_failed|frontend_build_failed) rollback=([0-9a-f]{40}) at=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
        description,
    )
    if not receipt_match or receipt_match.group(1) != main:
        return False
    # Check the host's actual held candidate and checkout, not GitHub alone.
    checkout = Path.home() / "web-app-init-base-env-preview"
    try:
        fd = os.open(checkout / ".webapp-failed-sha", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="ascii") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022 or info.st_size > 64 or stream.read().strip() != base:
                return False
        if git(checkout, "rev-parse", "HEAD") != main:
            return False
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    comments = all_pages(token, f"/issues/{number}/comments")
    for comment in sorted(comments, key=lambda c:c["id"], reverse=True):
        if comment.get("author_association") != "OWNER" or (comment.get("user") or {}).get("login") != OWNER:
            continue
        body=comment.get("body") or ""
        marker="[BASE_DEPLOY_REPAIR_REVIEW] "
        if not body.startswith(marker):
            continue
        try:
            event=json.loads(body[len(marker):])
        except ValueError:
            continue
        if not isinstance(event,dict) or event.get("head") != head:
            continue
        expected={"schema":1,"head":head,"failed_sha":base,"rollback_sha":main,"decision":"approved"}
        if event != expected:
            return False
        old_version,_=latest_release(token,base)
        new_version,required=latest_release(token,merge)
        return required and new_version>old_version
    return False


def replacement_allowed(token, number, base, head, merge):
    """Narrow exception: an explicitly rejected SHA and a specifically approved repair PR."""
    if api(token, "/branches/preview")["commit"]["sha"] != base:
        return False
    main = api(token, "/branches/main")["commit"]["sha"]
    if not valid(main) or main == base:
        return False
    title = f"[BASE-REJECTION {base}]"
    issues = all_pages(token, "/issues?state=open&sort=created&direction=desc")
    rejected = []
    for issue in issues:
        if (issue.get("title") != title or issue.get("author_association") != "OWNER"
                or issue.get("user", {}).get("login") != OWNER or "pull_request" in issue):
            continue
        body = issue.get("body") or ""
        if not body.startswith("[BASE_REJECTION] "):
            continue
        try:
            data = json.loads(body[len("[BASE_REJECTION] "):])
        except ValueError:
            continue
        if (isinstance(data, dict) and data.get("schema") == 1 and data.get("candidate_sha") == base
                and isinstance(data.get("reason"), str) and len(data["reason"].strip()) >= 5
                and type(issue.get("number")) is int):
            rejected.append(issue["number"])
    if not rejected:
        return deployment_replacement_allowed(token,number,base,head,merge,main)
    if len(rejected) != 1:
        return False
    comments = all_pages(token, f"/issues/{number}/comments")
    for comment in sorted(comments, key=lambda c: c["id"], reverse=True):
        if comment.get("author_association") != "OWNER" or comment.get("user", {}).get("login") != OWNER:
            continue
        body = comment.get("body") or ""
        if not body.startswith("[BASE_REPAIR_REVIEW] "):
            continue
        try:
            event = json.loads(body[len("[BASE_REPAIR_REVIEW] "):])
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("head") != head:
            continue
        expected = {"schema": 1, "head": head, "rejected_sha": base,
                    "rejection_issue": rejected[0], "decision": "approved"}
        if event != expected:
            return False
        old_version, _ = latest_release(token, base)
        new_version, required = latest_release(token, merge)
        return required and new_version > old_version
    return False


def gate_block(number, reason):
    # Fixed reason codes only: never print API payloads, credentials or git output.
    print(f"PR #{number}: preview gate blocked: {reason}", flush=True)
    return None


def authorization(token, number):
    pr = api(token, f"/pulls/{number}")
    if not eligible(pr):
        return gate_block(number, "github_merge_state_unstable" if pr.get("mergeable") is True and pr.get("mergeable_state") == "unstable" and not pr.get("draft") else "pr_ineligible")
    head, merge = pr["head"]["sha"], pr["merge_commit_sha"]
    base = api(token, "/branches/preview")["commit"]["sha"]
    if not valid(base):
        return gate_block(number, "invalid_preview_sha")
    commit = api(token, "/git/commits/" + merge)
    if [p["sha"] for p in commit["parents"]] != [base, head]:
        return gate_block(number, "candidate_parents_mismatch")
    comments = all_pages(token, f"/issues/{number}/comments")
    required = review(comments, head)
    if not required:
        # An explicit owner denial must never fall through to delegated review.
        if owner_decision_present(comments, head):
            return gate_block(number, "owner_review_missing_or_revoked")
        if not automatic_paths():
            return gate_block(number, "owner_review_missing_or_revoked")
        required = "automatic-path-policy"
    statuses = api(token, f"/commits/{head}/statuses?per_page=100")
    if not tested(statuses, head, base, merge):
        return gate_block(number, "exact_test_receipt_missing_or_invalid")
    if pr.get("mergeable_state") == "unstable":
        if len(statuses) >= 100 or not advisory_only(statuses, base, merge):
            return gate_block(number, "github_non_advisory_check_blocked")
        # Statuses are not the entire check suite. Refuse unknown or failed
        # check runs rather than treating all unstable states as advisory.
        checks = api(token, "/commits/" + head + "/check-runs?per_page=100")
        if (not isinstance(checks.get("check_runs"), list)
                or checks.get("total_count") != len(checks["check_runs"])
                or any(x.get("status") != "completed" or x.get("conclusion") != "success" for x in checks["check_runs"])):
            return gate_block(number, "github_check_run_blocked")
        if not any(x.get("context") == "macmini/progress-v1" and x.get("state") == "pending" for x in statuses[:1] + [next((x for x in statuses if x.get("context") == "macmini/progress-v1"), {})]):
            return gate_block(number, "github_unstable_unexplained")
    if api(token, "/branches/preview")["commit"]["sha"] != base:
        return gate_block(number, "preview_changed")
    return head, base, merge, required


def git(folder, *args):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
           "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
           "GIT_CONFIG_NOSYSTEM": "1"}
    # Never pass a GitHub token as a command argument.
    return subprocess.check_output(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(folder), *args],
        env=env, text=True, stderr=subprocess.STDOUT, timeout=90).strip()


def verify_candidate(folder, base, head, candidate, required):
    fields = git(folder, "show", "-s", "--format=%H %P", "refs/macci/tested").split()
    if fields != [candidate, base, head]:
        raise ValueError("Fetched merge candidate differs from tested parents")
    if git(folder, "rev-parse", "refs/macci/base") != base:
        raise ValueError("Preview moved")
    if required == "automatic-path-policy":
        if not automatic_diff_allowed(folder, base, candidate, automatic_paths()):
            raise ValueError("Candidate outside operator automatic path policy")
    else:
        git(folder, "merge-base", "--is-ancestor", required, base)


def merge_one(token, number, dry_run=False):
    frozen = preview_frozen(token)
    gate = authorization(token, number)
    if gate is None:
        return False
    head, base, merge, required = gate
    if frozen and (required == "automatic-path-policy" or not replacement_allowed(token, number, base, head, merge)):
        gate_block(number, "acceptance_freeze")
        return False
    with TemporaryDirectory(prefix="macci-preview-") as folder:
        git(folder, "init", "--bare")
        git(folder, "fetch", "--no-tags", REMOTE,
            f"refs/pull/{number}/merge:refs/macci/tested",
            "refs/heads/preview:refs/macci/base")
        try:
            verify_candidate(folder, base, head, merge, required)
        except (ValueError, subprocess.CalledProcessError):
            print(f"PR #{number}: candidate or dependency changed; no push", flush=True)
            return False
        if authorization(token, number) != gate:
            print(f"PR #{number}: gate changed; no push", flush=True)
            return False
        if required == "automatic-path-policy" and not automatic_diff_allowed(folder, base, merge, automatic_paths()):
            gate_block(number, "automatic_path_policy_changed")
            return False
        if preview_frozen(token) and (required == "automatic-path-policy" or not replacement_allowed(token, number, base, head, merge)):
            print(f"PR #{number}: preview acceptance freeze; no authorized replacement", flush=True)
            return False
        if dry_run:
            print(f"PR #{number}: dry run verified {merge}; no push", flush=True)
            return False
        # Push the exact tested commit, not PR head or a moving merge ref.
        # Non-force push protects concurrent divergent preview updates; branch rules
        # still apply. A failed push must never be retried with force.
        git(folder, "push", "--porcelain", REMOTE, merge + ":" + TARGET)
        actual = api(token, "/branches/preview")["commit"]["sha"]
        if actual != merge:
            print(f"PR #{number}: preview changed after push; reconcile before further release", flush=True)
            return True
        pr = api(token, f"/pulls/{number}")
        print(f"PR #{number}: preview advanced to tested SHA {merge}; GitHub merged={pr.get('merged') is True}", flush=True)
        return True


def shared_token_path():
    """Explicit opt-in: a private, same-user host credential; never use a symlink."""
    root = Path.home() / ".local/share/macmini-github"
    try:
        info = root.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Unsafe shared GitHub credential directory")
    path = root / "github-token"
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    return path


def read_token():
    path = shared_token_path() or ROOT / "github-token"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Unsafe credential file")
        token = stream.read(4096).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
            raise ValueError("Invalid credential")
        return token


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        token = read_token()
        with (ROOT / "preview-merge.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            prs = all_pages(token, "/pulls?state=open&base=preview&sort=created&direction=asc")
            for pr in prs:
                number = pr.get("number")
                if type(number) is not int or number <= 0 or pr.get("draft"):
                    continue
                try:
                    if merge_one(token, number, args.dry_run):
                        return 0  # New base requires fresh tests for following PRs.
                except (ValueError, KeyError, TypeError, OSError, URLError, subprocess.SubprocessError) as exc:
                    print(f"PR #{number}: gate/push blocked: {type(exc).__name__}", flush=True)
            return 0
    except (ValueError, KeyError, TypeError, OSError, URLError, subprocess.SubprocessError) as exc:
        print(f"Preview coordinator blocked: {type(exc).__name__}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
