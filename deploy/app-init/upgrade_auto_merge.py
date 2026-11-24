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
# Process-local, allowlisted diagnostics; scheduler is single-threaded under its tick lock.
last_reason = 'not_checked'

def deny(reason):
    global last_reason
    last_reason = reason
    return None



def api(path, *, method="GET", payload=None):
    if not re.fullmatch(r"/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:pulls/[1-9][0-9]*(?:/files|/merge)?|commits/[0-9a-f]{40}/statuses|git/commits/[0-9a-f]{40}|branches/main|compare/[0-9a-f]{40}\.\.\.[0-9a-f]{40})", path):
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
    except HTTPError as exc:
        deny("rest_http_" + str(exc.code))
        raise ValueError("GitHub auto-merge API unavailable") from None
    except (URLError, OSError, UnicodeError, json.JSONDecodeError):
        deny("rest_transport_or_response")
        raise ValueError("GitHub auto-merge API unavailable") from None



def official_foundation_target(target_sha):
    """A pending upgrade may safely complete after foundation main advances.

    Only trusted immutable ancestors of current official main are eligible.
    This prevents an older in-flight PR from blocking all later releases.
    """
    if not isinstance(target_sha, str) or not upgrade.SHA.fullmatch(target_sha):
        return False
    latest = upgrade.remote_ref(upgrade.SOURCE_URL, "refs/heads/main")
    if latest == target_sha:
        return True
    if not isinstance(latest, str) or not upgrade.SHA.fullmatch(latest):
        return False
    comparison = api("/repos/" + upgrade.FOUNDATION + "/compare/" + target_sha + "..." + latest)
    return (isinstance(comparison, dict)
            and comparison.get("status") == "ahead"
            and (comparison.get("merge_base_commit") or {}).get("sha") == target_sha)



def promotion_gate(repo, number, app_sha, foundation_sha):
    """No writes. Return exact PR head only when all test/scope gates pass."""
    if repo not in APPROVED_REPOS or not upgrade.SHA.fullmatch(app_sha) or not upgrade.SHA.fullmatch(foundation_sha):
        return deny("scope_or_sha_invalid")
    prefix = "/repos/" + repo
    pr = api(prefix + "/pulls/" + str(number))
    if (not isinstance(pr, dict) or pr.get("state") != "open" or pr.get("merged")
            or pr.get("draft") not in {True, False}):
        return deny("pr_not_open")
    head, base = pr.get("head") or {}, pr.get("base") or {}
    if (pr.get("user", {}).get("login") != OWNER or pr.get("author_association") != "OWNER"
            or head.get("repo", {}).get("full_name") != repo or base.get("repo", {}).get("full_name") != repo
            or base.get("ref") != "main" or base.get("sha") != app_sha
            or not isinstance(head.get("sha"), str) or not upgrade.SHA.fullmatch(head["sha"])
            or pr.get("mergeable") is not True or pr.get("mergeable_state") != "clean"):
        return deny("pr_identity_or_mergeability")
    if head.get("ref") != "base-upgrade/" + foundation_sha:
        return deny("branch_target_mismatch")
    # The scheduler creates exactly one commit on the observed app main.
    # Any manual follow-up commit (even if tests pass) requires human review.
    source = api(prefix + "/git/commits/" + head["sha"])
    if [parent.get("sha") for parent in source.get("parents", [])] != [app_sha]:
        return deny("source_parent_mismatch")
    body = pr.get("body")
    if not isinstance(body, str) or len(body) > MAX_BODY or "\n\n" not in body:
        return deny("upgrade_report_missing")
    try:
        report = json.loads(body.split("\n\n", 1)[1])
        changes = report["changes"]
        target = report["target"]
        if (report.get("schema") != 1 or report.get("blocked") is not False
                or report.get("app_sha") != app_sha or target["sha"] != foundation_sha
                or target.get("repository") != upgrade.FOUNDATION or not isinstance(changes, list)
                or len(changes) > 100):
            return deny("upgrade_report_mismatch")
        expected = {".app-init.json"}
        for change in changes:
            if (not isinstance(change, dict) or change.get("action") not in {"add", "replace", "merge"}
                    or not isinstance(change.get("path"), str)):
                return deny("upgrade_change_invalid")
            expected.add(change["path"])
        files = api(prefix + "/pulls/" + str(number) + "/files")
        if (not isinstance(files, list) or len(files) > 100 or not len(expected) <= len(files) <= len(expected) + 1
                or any(item.get("status") not in {"added", "modified"} for item in files)
                or not expected.issubset({item["filename"] for item in files})
                or not {item["filename"] for item in files}.issubset(expected | {"app/project_info.json"})):
            return deny("file_scope_mismatch")
    except (TypeError, ValueError, KeyError, IndexError):
        return deny("upgrade_report_invalid")
    statuses = api(prefix + "/commits/" + head["sha"] + "/statuses")
    if not isinstance(statuses, list):
        return deny("status_response_invalid")
    current = next((s for s in statuses if s.get("context") == CONTEXT), None)
    if (not current or current.get("creator", {}).get("login") != OWNER
            or current.get("state") != "success"):
        return deny("macci_not_success_or_untrusted")
    match = re.fullmatch(r"base=([0-9a-f]{40}) merge=([0-9a-f]{40}) passed", current.get("description", ""))
    if not match or match[1] != app_sha:
        return deny("macci_base_mismatch")
    tested = api(prefix + "/git/commits/" + match[2])
    if [p.get("sha") for p in tested.get("parents", [])] != [app_sha, head["sha"]]:
        return deny("macci_merge_parents_mismatch")
    return head["sha"]


def promote(repo, row, app_sha, target_sha):
    """Only a fully validated Panel PR may be merged; never force a change."""
    deny("not_checked")
    number = row.get("number")
    if type(number) is not int or number <= 0:
        return bool(deny("invalid_pr_number"))
    if not official_foundation_target(target_sha):
        return bool(deny("foundation_target_unverified"))
    head = promotion_gate(repo, number, app_sha, target_sha)
    if head is None:
        return False
    prefix = "/repos/" + repo
    # A new main commit or source release invalidates this authorization.
    if (api(prefix + "/branches/main")["commit"]["sha"] != app_sha
            or not official_foundation_target(target_sha)
            or promotion_gate(repo, number, app_sha, target_sha) != head):
        if last_reason == "not_checked":
            deny("preflight_changed")
        return False
    # Recover if a previous attempt already removed Draft but was interrupted
    # before the expected-head merge. All exact provenance and MacCI checks
    # above still apply to the ready PR on every tick.
    preflight = api(prefix + "/pulls/" + str(number))
    if preflight.get("draft") is True:
        if not graphql_ready(preflight.get("node_id")):
            if last_reason == "not_checked":
                deny("graphql_ready_failed")
            return False
    elif preflight.get("draft") is not False:
        return bool(deny("invalid_draft_state"))
    pr = api(prefix + "/pulls/" + str(number))
    if pr.get("head", {}).get("sha") != head or pr.get("base", {}).get("sha") != app_sha or pr.get("draft"):
        return bool(deny("ready_ref_changed"))
    result = api(prefix + "/pulls/" + str(number) + "/merge", method="PUT",
                 payload={"merge_method": "merge", "sha": head})
    if result.get("merged") is True:
        deny("merged")
        return True
    return bool(deny("merge_not_completed"))


def graphql_ready(node_id):
    if not isinstance(node_id, str) or not node_id.startswith("PR_"):
        return bool(deny("graphql_invalid_node"))
    data = json.dumps({"query": "mutation($id: ID!) { markPullRequestReadyForReview(input:{pullRequestId:$id}) { pullRequest { isDraft } } }",
                       "variables": {"id": node_id}}).encode()
    req = Request("https://api.github.com/graphql", data=data, method="POST",
                  headers={"Authorization": "Bearer " + upgrade.read_token(),
                           "Content-Type": "application/json", "User-Agent": "foundation-upgrade-auto-merge"})
    try:
        with build_opener(upgrade.NoRedirect()).open(req, timeout=12) as resp:
            outcome = json.load(resp)
        if outcome.get("errors"):
            # Only recognized error categories, never raw GraphQL messages.
            categories = {str(e.get("type","")) for e in outcome["errors"] if isinstance(e,dict)}
            return bool(deny("graphql_forbidden" if categories & {"FORBIDDEN","UNAUTHORIZED"} else "graphql_errors"))
        if outcome["data"]["markPullRequestReadyForReview"]["pullRequest"]["isDraft"] is False:
            return True
        return bool(deny("graphql_still_draft"))
    except HTTPError as exc:
        return bool(deny("graphql_http_" + str(exc.code)))
    except (URLError, OSError, KeyError, TypeError, ValueError):
        return bool(deny("graphql_transport_or_response"))
