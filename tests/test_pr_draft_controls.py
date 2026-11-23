"""Exact-head owner-controlled Draft release; no GitHub network."""
import pytest
from app import pr_draft_controls as d

A, B, H = "a" * 40, "b" * 40, "c" * 40

def target_pr():
    return {
        "state": "open", "draft": True, "author_association": "OWNER",
        "title": "fix: reusable draft safety control",
        "user": {"login": "layho"}, "node_id": "PR_example",
        "mergeable": True, "mergeable_state": "clean",
        "base": {"ref": "preview", "repo": {"full_name": d.REPO}},
        "head": {"sha": H, "repo": {"full_name": d.REPO}}
    }


@pytest.fixture
def github(monkeypatch):
    monkeypatch.setattr(d.release_acceptance, "enabled", lambda: True)
    monkeypatch.setenv("APP_RUNNING_SHA", A)
    refs = {"main": A, "preview": A}
    pr = target_pr()
    comments = []
    def mock_api(path, payload=None):
        if path.startswith("/branches/"):
            return {"commit": {"sha": refs[path.removeprefix("/branches/")]}}
        if path == "/pulls/54":
            return pr
        if path == "/issues/54/comments":
            comments.append(payload["body"])
            return {"id": 3, "user": {"login": "layho"}}
        raise AssertionError(path)
    monkeypatch.setattr(d, "github", mock_api)
    monkeypatch.setattr(d, "mark_ready", lambda node: True)
    return refs, pr, comments


def test_owner_can_release_draft_after_official_release(github):
    refs, pr, comments = github
    result = d.release(54, H)
    assert result["released"] is True
    assert len(comments) == 1
    assert comments[0].startswith(d.REVIEW_MARKER)
    import json
    data = json.loads(comments[0][len(d.REVIEW_MARKER):])
    assert data == {"schema": 1, "head": H, "decision": "approved", "requires_ancestor": A}


@pytest.mark.parametrize("failure", ["freeze", "runtime", "wrong_head", "not_draft", "wrong_owner",
                                      "foreign_repo", "disabled"])
def test_release_rejected_safely(github, monkeypatch, failure):
    refs, pr, comments = github
    if failure == "freeze":
        refs["preview"] = B
    elif failure == "runtime":
        monkeypatch.setenv("APP_RUNNING_SHA", B)
    elif failure == "wrong_head":
        pr["head"]["sha"] = B
    elif failure == "not_draft":
        pr["draft"] = False
    elif failure == "wrong_owner":
        pr["user"]["login"] = "someone-else"
    elif failure == "foreign_repo":
        pr["head"]["repo"]["full_name"] = "attacker/fork"
    elif failure == "disabled":
        monkeypatch.setattr(d.release_acceptance, "enabled", lambda: False)
    with pytest.raises(d.DraftReleaseError):
        d.release(54, H)
    assert comments == []


def test_changes_after_review_do_not_unlock(github, monkeypatch):
    refs, pr, comments = github
    def moved(node):
        refs["preview"] = B
        return True
    monkeypatch.setattr(d, "mark_ready", moved)
    # External changes can race the final GraphQL call, but the subsequent
    # merge coordinator still checks exact head/base/MacCI before push.
    assert d.release(54, H)["released"] is True
    assert len(comments) == 1


def test_stale_head_never_writes(github):
    _, _, comments = github
    with pytest.raises(d.DraftReleaseError):
        d.release(54, B)
    assert not comments

def test_non_owner_review_token_does_not_unlock(github, monkeypatch):
    _, _, comments = github
    original = d.github
    def mismatched(path, payload=None):
        if path.endswith("/comments"):
            comments.append(payload["body"])
            return {"id": 5, "user": {"login": "other"}}
        return original(path, payload)
    monkeypatch.setattr(d, "github", mismatched)
    with pytest.raises(d.DraftReleaseError, match="OWNER"):
        d.release(54, H)


def test_mark_ready_uses_dedicated_pull_request_token(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv("APP_GITHUB_FEEDBACK_TOKEN", "issues-only-token")
    monkeypatch.setenv("APP_GITHUB_PR_TOKEN", "owner-pull-request-token")
    captured = []
    class FakeOpener:
        def open(self, request, timeout=None):
            captured.append((request.headers, request.data))
            class Resp:
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self, *args): return b'{"data":{"markPullRequestReadyForReview":{"pullRequest":{"isDraft":false}}}}'
            return Resp()
    monkeypatch.setattr(d, "build_opener", lambda *args: FakeOpener())
    assert d.mark_ready("PR_abc123")
    assert captured[0][0]["Authorization"] == "Bearer owner-pull-request-token"
    assert b"markPullRequestReadyForReview" in captured[0][1]


def test_graphql_permission_denial_does_not_claim_success(monkeypatch):
    monkeypatch.setenv("APP_GITHUB_PR_TOKEN", "test-only")
    class FakeOpener:
        def open(self, request, timeout=None):
            class Resp:
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self, *args): return b'{"errors":[{"message":"Resource not accessible by personal access token"}]}'
            return Resp()
    monkeypatch.setattr(d, "build_opener", lambda *args: FakeOpener())
    with pytest.raises(d.DraftReleaseError, match="GH-GQL-FORBIDDEN"):
        d.mark_ready("PR_abc123")


def test_conflicted_pr_can_exit_draft_but_never_claim_merge(github):
    refs, pr, comments = github
    pr["mergeable"] = False
    pr["mergeable_state"] = "dirty"
    result = d.release(54, H)
    assert result["released"] is True
    assert result["merge_blocked"] is True
    assert "冲突" in result["message"]
    assert len(comments) == 1


def test_unknown_mergeability_can_exit_draft(github):
    _, pr, comments = github
    pr["mergeable"] = None
    pr["mergeable_state"] = "unknown"
    result = d.release(54, H)
    assert result["released"] is True
    assert result["merge_blocked"] is False
    assert len(comments) == 1


def test_graphql_error_classification_never_claims_all_are_missing_permissions():
    assert "GH-GQL-FORBIDDEN" in str(d.graphql_failure([
        {"message": "Resource not accessible by personal access token", "type": "FORBIDDEN"}]))
    assert "GH-GQL-VALIDATION" in str(d.graphql_failure([
        {"message": "Field 'wrongField' doesn't exist on type Mutation"}]))
    assert "GH-GQL-NODE" in str(d.graphql_failure([
        {"message": "Could not resolve to a node with the global id"}]))
    assert "GH-GQL-RATE" in str(d.graphql_failure([
        {"message": "API rate limit exceeded"}]))
    assert "the-user-token" not in str(d.graphql_failure([
        {"message": "Unknown error the-user-token"}]))


def test_out_of_order_release_stays_draft_without_writing_review(github, monkeypatch):
    _, pr, comments = github
    pr["title"] = "feat(feedback): V0.1.25 反馈交付中心"
    monkeypatch.setattr(d, "published_version", lambda: (0, 1, 23))
    with pytest.raises(d.DraftReleaseError, match="版本顺序受阻"):
        d.release(54, H)
    assert comments == []


def test_immediate_next_release_may_exit_draft(github, monkeypatch):
    _, pr, comments = github
    pr["title"] = "feat(tasks): V0.1.24 开发任务"
    monkeypatch.setattr(d, "published_version", lambda: (0, 1, 23))
    assert d.release(54, H)["released"] is True
    assert comments


def test_non_release_prs_unaffected_by_version_gate(github, monkeypatch):
    _, pr, comments = github
    pr["title"] = "fix: prevent old sha acceptance"
    monkeypatch.setattr(d, "published_version", lambda: None)
    assert d.release(54, H)["released"]


def test_pr_rest_preflight_uses_dedicated_pr_token(monkeypatch):
    import json
    monkeypatch.setenv("APP_GITHUB_FEEDBACK_TOKEN", "issues-only")
    monkeypatch.setenv("APP_GITHUB_PR_TOKEN", "pr-write-token")
    captured = []
    class FakeOpener:
        def open(self, request, timeout=None):
            captured.append(request)
            class Resp:
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self, *args): return b'{"commit":{"sha":"123"}}'
            return Resp()
    monkeypatch.setattr(d, "build_opener", lambda *args: FakeOpener())
    assert d.github("/branches/preview") == {"commit": {"sha": "123"}}
    assert captured[0].headers["Authorization"] == "Bearer pr-write-token"
    assert captured[0].get_method() == "GET"
    assert captured[0].full_url.endswith("/repos/layho/web-app-init-base-env/branches/preview")
    d.github("/issues/90/comments", {"body": "safe review marker"})
    assert captured[1].get_method() == "POST"
    assert captured[1].headers["Authorization"] == "Bearer pr-write-token"


def test_missing_pr_token_stops_rest_without_fallback(monkeypatch):
    monkeypatch.delenv("APP_GITHUB_PR_TOKEN", raising=False)
    monkeypatch.setenv("APP_GITHUB_FEEDBACK_TOKEN", "issues-only")
    calls = []
    monkeypatch.setattr(d, "build_opener", lambda *args: calls.append(args))
    with pytest.raises(d.DraftReleaseError, match="GH-PR-TOKEN-MISSING"):
        d.github("/branches/preview")
    assert calls == []


def test_pr_rest_rejects_shared_token_and_unsafe_urls(monkeypatch):
    monkeypatch.setenv("APP_GITHUB_PR_TOKEN", "same")
    monkeypatch.setenv("APP_GITHUB_FEEDBACK_TOKEN", "same")
    with pytest.raises(d.DraftReleaseError, match="GH-PR-TOKEN-SHARED"):
        d.github("/branches/preview")
    monkeypatch.setenv("APP_GITHUB_PR_TOKEN", "dedicated")
    with pytest.raises(d.DraftReleaseError, match="路径无效"):
        d.github("https://attacker.invalid/steal")
