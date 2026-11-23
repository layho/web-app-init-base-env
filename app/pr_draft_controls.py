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


def pr_token():
    """One credential for the entire owner PR-review transaction."""
    token = os.getenv("APP_GITHUB_PR_TOKEN", "").strip()
    if not token:
        raise DraftReleaseError(
            "GH-PR-TOKEN-MISSING：基座服务未加载专用 PR Token，未执行操作")
    if token == os.getenv("APP_GITHUB_FEEDBACK_TOKEN", "").strip():
        raise DraftReleaseError(
            "GH-PR-TOKEN-SHARED：PR Token 与反馈 Token 混用，未执行操作")
    return token


def github(path, payload=None):
    """Keep REST PR reads on the PR token, Issue review writes on Issues token.

    Previously REST preflight used the Issues credential even when the separate
    PR credential was valid. Never make a PR-read failure look like a write error.
    """
    if not isinstance(path, str) or not path.startswith("/") or "://" in path:
        raise DraftReleaseError("GitHub 请求路径无效")
    # Owner review comments use the dedicated Issues writer, while all
    # PR state reads and GraphQL mutations use the PR token. Never substitute
    # one credential for the other: their fine-grained scopes may differ.
    is_review_comment = payload is not None and re.fullmatch(
        r"/issues/[1-9][0-9]{0,6}/comments", path) is not None
    if payload is not None and not is_review_comment:
        raise DraftReleaseError("仅允许向指定 PR 的 Issue 评论端点写入审查回执")
    pr_token()  # required even if the next step is an Issues review comment
    if is_review_comment:
        token = os.getenv("APP_GITHUB_FEEDBACK_TOKEN", "").strip()
        if not token:
            raise DraftReleaseError(
                "GH-REVIEW-TOKEN-MISSING：未配置用于 OWNER 审查评论的 Issues 写入凭据")
    else:
        token = pr_token()
    request = Request(
        "https://api.github.com/repos/" + REPO + path,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "base-owner-draft-release"},
        method="POST" if payload is not None else "GET")
    try:
        with build_opener(release_acceptance.NoRedirect()).open(request, timeout=10) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise DraftReleaseError(
                f"GH-PR-REST-{exc.code}：GitHub 拒绝"
                + ("Issues OWNER 审查评论" if is_review_comment else "PR Token 的状态读取")
                + "；请核对该用途 Token 的仓库授权和相应权限。未解除 Draft"
            ) from None
        raise DraftReleaseError(
            f"GH-PR-REST-{exc.code}：GitHub REST 请求失败，未解除 Draft") from None
    except (URLError, OSError, ValueError, TypeError):
        raise DraftReleaseError(
            "GH-PR-REST-NETWORK：PR Token 状态核对连接失败或返回数据无效，未解除 Draft"
        ) from None


def graphql_failure(errors):
    """Classify GitHub GraphQL failures without exposing raw data or tokens."""
    if not isinstance(errors, list) or not errors or not isinstance(errors[0], dict):
        return DraftReleaseError("GH-GQL-UNKNOWN：GitHub 返回异常错误结构")
    error = errors[0]
    message = str(error.get("message", "")).lower()[:500]
    ext = error.get("extensions") if isinstance(error.get("extensions"), dict) else {}
    code = str(ext.get("type", "")).upper()
    if ("resource not accessible" in message or "forbidden" in message
            or "insufficient" in message or code == "FORBIDDEN"):
        return DraftReleaseError("GH-GQL-FORBIDDEN：GraphQL 拒绝本次操作，不能据此断定缺少 Pull requests 权限")
    if "could not resolve to a node" in message or "not found" in message:
        return DraftReleaseError("GH-GQL-NODE：PR 节点不存在或该 Token 无法访问")
    if ("validation" in message or "field " in message or
            code in {"GRAPHQL_VALIDATION_FAILED", "UNPROCESSABLE"}):
        return DraftReleaseError("GH-GQL-VALIDATION：GitHub GraphQL 请求未通过校验")
    if "rate limit" in message or "abuse" in message:
        return DraftReleaseError("GH-GQL-RATE：触发 GitHub 请求限制，请稍后重试")
    return DraftReleaseError("GH-GQL-OTHER：GitHub 拒绝解除 Draft，需查看服务端脱敏诊断记录")


def published_version():
    """The running, already-accepted application's release version."""
    try:
        from pathlib import Path
        data = json.loads(Path(__file__).with_name("release_notes.json").read_text(encoding="utf-8"))
        value = data[0]["version"]
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value)
        return tuple(map(int, match.groups())) if match else None
    except (OSError, IndexError, KeyError, TypeError, ValueError):
        return None


def release_order_gate(title):
    """Drafts for later releases must not bypass an unresolved earlier release."""
    if not isinstance(title, str):
        raise DraftReleaseError("无法核对 PR 版本")
    match = re.search(r"(?i)\bv(\d+)\.(\d+)\.(\d+)\b", title)
    if not match:
        return
    candidate = tuple(map(int, match.groups()))
    current = published_version()
    if current is None:
        raise DraftReleaseError("无法读取当前已发布版本，解除 Draft 已锁定")
    if candidate[:2] == current[:2] and candidate[2] > current[2] + 1:
        raise DraftReleaseError(f"版本顺序受阻：当前正式版 V{'.'.join(map(str,current))}，请先完成 V{current[0]}.{current[1]}.{current[2] + 1} 的测试与发布")


def mark_ready(node_id):
    if not isinstance(node_id, str) or not re.fullmatch(r"PR_[A-Za-z0-9_]+", node_id):
        raise DraftReleaseError("PR 标识无效，不能解除 Draft")
    # Feedback tokens commonly grant Issues write but not Pull requests write.
    # A dedicated owner PR token can be supplied independently.
    token = pr_token()
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
        try:
            payload = json.loads(exc.read(8192))
        except (OSError, ValueError):
            payload = {}
        if isinstance(payload, dict) and payload.get("errors"):
            raise graphql_failure(payload["errors"]) from None
        if exc.code in (401, 403):
            raise DraftReleaseError(f"GH-HTTP-{exc.code}：GitHub 拒绝请求，需核对运行服务的 Token 身份或仓库授权") from None
        raise DraftReleaseError(f"GH-HTTP-{exc.code}：GitHub 请求失败，Draft 状态未改变") from None
    except (URLError, OSError, ValueError):
        raise DraftReleaseError("GH-NETWORK：GitHub GraphQL 无法连接或回执无效") from None
    if result.get("errors"):
        raise graphql_failure(result["errors"])
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
    release_order_gate(pr.get("title"))
    # Releasing a Draft is an owner workflow decision, not a merge.
    # Conflicts and pending merge calculations remain blocked by MacCI and
    # the preview coordinator; do not prevent the owner from unpausing work.
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
    conflict = pr.get("mergeable") is False or pr.get("mergeable_state") == "dirty"
    return {"released": True, "number": number, "head": expected_head,
            "merge_blocked": conflict,
            "message": "已解除 Draft，但当前存在冲突，需修复后才能进入测试与自动合并" if conflict
                       else "已解除 Draft，等待新基线 MacCI 测试及自动合并"}
