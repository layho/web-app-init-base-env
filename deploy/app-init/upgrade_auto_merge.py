"""Fail-closed auto-merge for explicitly approved Panel foundation upgrade PRs.

No production calls, secrets or deploy actions. Only trusted MacCI exact-candidate
success and clean three-way planner actions can authorize a downstream merge.
"""
import json
import re
from urllib.request import Request, build_opener
from urllib.error import HTTPError, URLError
import upgrade_pr as upgrade

APPROVED_REPOS = {"layho/macmini-office-app-panel"}
CONTEXT = "macmini/isolated-tests-v1"
OWNER = "layho"
MAX_BODY = 180_000


def api(path, *, method="GET", payload=None):
    if not re.fullmatch(r"/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:pulls/[1-9][0-9]*(?:/files|/merge)?|commits/[0-9a-f]{40}/statuses|git/commits/[0-9a-f]{40}|branches/main)", path):
        raise ValueError("Unsupported auto-merge endpoint")
    data = None if payload is None else json.dumps(payload).encode()
    token = upgrade.read_token()
    req = Request("https://api.github.com" + path, data=data, method=method,
                  headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                           "Content-Type": "application/json", "User-Agent": "foundation-upgrade-auto-merge"})
    try:
        with build_opener(upgrade.NoRedirect()).open(req, timeout=12) as response:
            result = response.read(1024 * 1024 + 1)
            if len(result) > 1024 * 1024:
                raise ValueError("Oversized API response")
            return json.loads(result)
    except (HTTPError, URLError, OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("GitHub auto-merge API unavailable") from None


def promotion_gate(repo, number, app_sha, foundation_sha):
    """No writes. Return exact PR head only when all test/scope gates pass."""
    if repo not in APPROVED_REPOS or not upgrade.SHA.fullmatch(app_sha) or not upgrade.SHA.fullmatch(foundation_sha):
        return None
    prefix = "/repos/" + repo
    pr = api(prefix + "/pulls/" + str(number))
    if not isinstance(pr, dict) or pr.get("state") != "open" or pr.get("merged") or pr.get("draft") is not True:
        return None
    head, base = pr.get("head") or {}, pr.get("base") or {}
    if (pr.get("user", {}).get("login") != OWNER or pr.get("author_association") != "OWNER"
            or head.get("repo", {}).get("full_name") != repo or base.get("repo", {}).get("full_name") != repo
            or base.get("ref") != "main" or base.get("sha") != app_sha
            or not isinstance(head.get("sha"), str) or not upgrade.SHA.fullmatch(head["sha"])
            or pr.get("mergeable") is not True or pr.get("mergeable_state") != "clean"):
        return None
    if head.get("ref") != "base-upgrade/" + foundation_sha:
        return None
    body = pr.get("body")
    if not isinstance(body, str) or len(body) > MAX_BODY or "\n\n" not in body:
        return None
    try:
        report = json.loads(body.split("\n\n", 1)[1])
        changes = report["changes"]
        target = report["target"]
        if (report.get("schema") != 1 or report.get("blocked") is not False
                or report.get("app_sha") != app_sha or target["sha"] != foundation_sha
                or target.get("repository") != upgrade.FOUNDATION or not isinstance(changes, list)
                or len(changes) > 100):
            return None
        expected = {".app-init.json"}
        for change in changes:
            if (not isinstance(change, dict) or change.get("action") not in {"add", "replace", "merge"}
                    or not isinstance(change.get("path"), str)):
                return None
            expected.add(change["path"])
        files = api(prefix + "/pulls/" + str(number) + "/files")
        if (not isinstance(files, list) or len(files) > 100 or len(files) != len(expected)
                or any(item.get("status") not in {"added", "modified"} for item in files)
                or {item["filename"] for item in files} != expected):
            return None
    except (TypeError, ValueError, KeyError, IndexError):
        return None
    statuses = api(prefix + "/commits/" + head["sha"] + "/statuses")
    if not isinstance(statuses, list):
        return None
    current = next((s for s in statuses if s.get("context") == CONTEXT), None)
    if (not current or current.get("creator", {}).get("login") != OWNER
            or current.get("state") != "success"):
        return None
    match = re.fullmatch(r"base=([0-9a-f]{40}) merge=([0-9a-f]{40}) passed", current.get("description", ""))
    if not match or match[1] != app_sha:
        return None
    tested = api(prefix + "/git/commits/" + match[2])
    if [p.get("sha") for p in tested.get("parents", [])] != [app_sha, head["sha"]]:
        return None
    return head["sha"]


def promote(repo, row, app_sha, target_sha):
    """Only a fully validated Panel PR may be merged; never force a change."""
    number = row.get("number")
    if type(number) is not int or number <= 0:
        return False
    head = promotion_gate(repo, number, app_sha, target_sha)
    if head is None:
        return False
    prefix = "/repos/" + repo
    # A new main commit or source release invalidates this authorization.
    if (api(prefix + "/branches/main")["commit"]["sha"] != app_sha
            or upgrade.remote_ref(upgrade.SOURCE_URL, "refs/heads/main") != target_sha
            or promotion_gate(repo, number, app_sha, target_sha) != head):
        return False
    # Draft PR requires explicit transition to ready. Fail closed if unavailable.
    # Do not merge blindly: the saved candidate must remain unchanged.
    ready = graphql_ready(api(prefix + "/pulls/" + str(number))["node_id"])
    if not ready:
        return False
    pr = api(prefix + "/pulls/" + str(number))
    if pr.get("head", {}).get("sha") != head or pr.get("base", {}).get("sha") != app_sha or pr.get("draft"):
        return False
    result = api(prefix + "/pulls/" + str(number) + "/merge", method="PUT",
                 payload={"merge_method": "merge", "sha": head})
    return result.get("merged") is True


def graphql_ready(node_id):
    if not isinstance(node_id, str) or not node_id.startswith("PR_"):
        return False
    data = json.dumps({"query": "mutation($id: ID!) { markPullRequestReadyForReview(input:{pullRequestId:$id}) { pullRequest { isDraft } } }",
                       "variables": {"id": node_id}}).encode()
    req = Request("https://api.github.com/graphql", data=data, method="POST",
                  headers={"Authorization": "Bearer " + upgrade.read_token(),
                           "Content-Type": "application/json", "User-Agent": "foundation-upgrade-auto-merge"})
    try:
        with build_opener(upgrade.NoRedirect()).open(req, timeout=12) as resp:
            outcome = json.load(resp)
        return (not outcome.get("errors")
                and outcome["data"]["markPullRequestReadyForReview"]["pullRequest"]["isDraft"] is False)
    except (HTTPError, URLError, OSError, KeyError, TypeError, ValueError):
        return False
