"""Owner-confirmed Draft release for reviewed base preview PRs.

This intentionally never merges or deploys. Existing MacCI and preview merge
coordinator remain sole authorities for tested merging.
"""
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from . import release_acceptance

REPO = "layho/web-app-init-base-env"
OWNER = "layho"
SHA = re.compile(r"[0-9a-f]{40}\Z")
REVIEW_MARKER = "[BASE_PREVIEW_REVIEW] "


class DraftReleaseError(Exception):
    pass


def github(path, payload=None):
    # Reuse the bounded GitHub helper without exposing credentials.
    try:
        return release_acceptance.api(path, payload)
    except (release_acceptance.AcceptanceError, KeyError, TypeError, ValueError):
        raise DraftReleaseError("无法核实 GitHub 状态或权限，未解除 Draft") from None


def mark_ready(node_id):
    if not isinstance(node_id, str) or not re.fullmatch(r"PR_[A-Za-z0-9_]+", node_id):
        raise DraftReleaseError("PR 标识无效，不能解除 Draft")
    # Feedback tokens commonly grant Issues write but not Pull requests write.
    # A dedicated owner PR token can be supplied independently.
    token = os.getenv("APP_GITHUB_PR_TOKEN") or os.getenv("APP_GITHUB_FEEDBACK_TOKEN", "")
    if not token:
        raise DraftReleaseError("未配置 GitHub PR 写入凭据，无法解除 Draft")
    data = json.dumps({
        "query": "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){pullRequest{isDraft}}}",
        "variables": {"id": node_id},
    }).encode("utf-8")
    request = Request("https://api.github.com/graphql", data=data, method="POST",
                      headers={"Authorization": "Bearer " + token,
                               "Accept": "application/vnd.github+json",
                               "Content-Type": "application/json",
                               "User-Agent": "base-owner-draft-release"})
    try:
        with build_opener(release_acceptance.NoRedirect()).open(request, timeout=10) as response:
            result = json.load(response)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise DraftReleaseError("解除 Draft 的 GitHub 凭据未获授权，请配置具有 Pull requests 写入权限的 OWNER Token") from None
        raise DraftReleaseError("GitHub 返回 HTTP 错误，未解除 Draft") from None
    except (URLError, OSError, ValueError):
        raise DraftReleaseError("连接 GitHub GraphQL 失败，未解除 Draft") from None
    if result.get("errors"):
        # GitHub GraphQL returns errors as untrusted strings, never echo them.
        raise DraftReleaseError("GitHub GraphQL 拒绝解除 Draft：请检查 OWNER Token 的 Pull requests 写入权限")
    if result.get("data", {}).get("markPullRequestReadyForReview", {}).get("pullRequest", {}).get("isDraft") is not False:
        raise DraftReleaseError("GitHub 未确认 Draft 状态变化，已安全停止")
    return True


def validate(number, expected_head):
    if not release_acceptance.enabled():
        raise DraftReleaseError("仅基座验收站管理员可操作 Draft")
    if type(number) is not int or not 1 <= number <= 1000000 or not isinstance(expected_head, str) or SHA.fullmatch(expected_head) is None:
        raise DraftReleaseError("PR 编号或 HEAD SHA 无效")
    # Both release refs must agree before accepting changes for the next batch.
    main, preview = github("/branches/main")["commit"]["sha"], github("/branches/preview")["commit"]["sha"]
    if not all(isinstance(x, str) and SHA.fullmatch(x) for x in (main, preview)) or main != preview:
        raise DraftReleaseError("存在待验收发布候选，main 与 preview 不一致，禁止解除 Draft")
    # Don't unlock a PR until the current accepted preview was actually deployed.
    if os.getenv("APP_RUNNING_SHA") != preview:
        raise DraftReleaseError("网页运行 SHA 与最新 preview 不一致，请等待部署后操作")
    pr = github(f"/pulls/{number}")
    head, base = pr.get("head") or {}, pr.get("base") or {}
    if (pr.get("state") != "open" or pr.get("draft") is not True
            or pr.get("author_association") != "OWNER" or (pr.get("user") or {}).get("login") != OWNER
            or base.get("ref") != "preview" or (base.get("repo") or {}).get("full_name") != REPO
            or (head.get("repo") or {}).get("full_name") != REPO or head.get("sha") != expected_head):
        raise DraftReleaseError("PR 已变化、不是 OWNER 的基座 preview 草稿或已解除 Draft")
    if pr.get("mergeable") is not True or pr.get("mergeable_state") != "clean":
        raise DraftReleaseError("PR 尚不可安全合并，请先解决冲突或等待 GitHub 计算")
    # A deliberate owner gesture establishes reviewed HEAD + ancestor. On a new
    # preview base MacCI must rerun: stale statuses cannot be reused.
    if github("/branches/main")["commit"]["sha"] != main or github("/branches/preview")["commit"]["sha"] != preview:
        raise DraftReleaseError("发布基线在确认期间变化，未解除 Draft")
    return pr, preview


def release(number, expected_head):
    pr, ancestor = validate(number, expected_head)
    # Confirm that the GitHub credentials act as the repo owner, rather than
    # letting a low-privilege integration silently produce ineffectual approval.
    # The merge coordinator will independently check owner authorship of the
    # review comment and exact current HEAD/base/MacCI before any merge.
    marker = REVIEW_MARKER + json.dumps({
        "schema": 1, "head": expected_head, "decision": "approved",
        "requires_ancestor": ancestor,
    }, separators=(",", ":"), ensure_ascii=False)
    comment = github(f"/issues/{number}/comments", {"body": marker})
    if (not isinstance(comment, dict) or (comment.get("user") or {}).get("login") != OWNER):
        raise DraftReleaseError("审查评论不是仓库 OWNER 发布，保留 Draft，需检查 GitHub Token 身份")
    # Recheck after any write, so a moved ref never authorizes draft removal.
    validate(number, expected_head)
    mark_ready(pr.get("node_id"))
    return {"released": True, "number": number, "head": expected_head,
            "message": "已解除 Draft，等待新基线 MacCI 测试及自动合并"}
