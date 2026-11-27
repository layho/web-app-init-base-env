"""Release acceptance must authorize exactly a tested candidate; no live Github calls."""
import json
import pytest
from app import release_acceptance as acceptance

HEAD = "a" * 40
BASE = "b" * 40
MERGE = "c" * 40
RUNNING = "d" * 40
MAIN = "e" * 40
TREE = "f" * 40


@pytest.fixture
def github(monkeypatch, tmp_path):
    monkeypatch.setattr(acceptance, "RECEIPTS", tmp_path / "receipts")
    monkeypatch.setenv("APP_BASE_PREVIEW", "1")
    monkeypatch.setenv("APP_PUBLIC_ORIGIN", "https://base.layho.com")
    monkeypatch.setenv("APP_GITHUB_FEEDBACK_TOKEN", "test-only")
    monkeypatch.setenv("APP_RUNNING_SHA", RUNNING)
    refs = {"main": MAIN, "preview": RUNNING}
    statuses = [{"context": acceptance.CONTEXT, "state": "success",
                 "description": f"base={BASE} merge={MERGE} passed"}]
    issues = []
    commits = {s: {"parents": [{"sha": BASE}, {"sha": HEAD}], "tree": {"sha": TREE}}
               for s in [RUNNING, MERGE]}
    def api(path, data=None):
        if path.startswith("/branches/"):
            return {"commit": {"sha": refs[path.split("/")[-1]]}}
        if path.startswith("/compare/"):
            left, right = path.split("/")[-1].split("...")
            return {"status": "ahead" if (left, right) == (MAIN, RUNNING) else "behind"}
        if path.startswith("/git/commits/"):
            return commits[path.split("/")[-1]]
        if path.startswith("/commits/"):
            return statuses
        if path.startswith("/issues?"):
            return issues
        if path == "/issues":
            issue = {**data, "number": 91, "author_association": "OWNER", "state": "open"}
            issues.append(issue)
            return issue
        if path == "/issues/91":
            return issues[0]
        raise AssertionError(path)
    monkeypatch.setattr(acceptance, "api", api)
    return refs, statuses, issues, commits


def test_acceptance_records_tested_exact_version_once(github):
    refs, statuses, issues, commits = github
    assert acceptance.submit(RUNNING, "0.1.2", True)["state"] == "queued"
    assert len(issues) == 1
    payload = json.loads(issues[0]["body"].split(" ", 1)[1])
    assert payload == {"schema": 1, "candidate_sha": RUNNING, "expected_main_sha": MAIN,
                       "version": "0.1.2", "test_head": HEAD, "test_base": BASE,
                       "tested_merge": MERGE, "tree": TREE}
    assert acceptance.submit(RUNNING, "0.1.2", True)["issue"] == 91
    assert len(issues) == 1
    refs["main"] = RUNNING
    assert acceptance.state(RUNNING)["state"] == "published"


@pytest.mark.parametrize("failure", ["not_deployed", "stale_runtime", "new_preview", "failed_test", "wrong_tree", "wrong_base"])
def test_release_preconditions_fail_closed(github, failure):
    refs, statuses, issues, commits = github
    sha, deployed = RUNNING, True
    if failure == "not_deployed":
        deployed = False
    elif failure == "stale_runtime":
        sha = HEAD
    elif failure == "new_preview":
        refs["preview"] = HEAD
    elif failure == "failed_test":
        statuses[0]["state"] = "failure"
    elif failure == "wrong_tree":
        commits[MERGE]["tree"]["sha"] = HEAD
    elif failure == "wrong_base":
        statuses[0]["description"] = f"base={HEAD} merge={MERGE} passed"
    with pytest.raises(acceptance.AcceptanceError):
        acceptance.submit(sha, "0.1.2", deployed)
    assert not issues


def test_normal_app_cannot_publish(github, monkeypatch):
    monkeypatch.delenv("APP_BASE_PREVIEW")
    assert acceptance.state(RUNNING)["enabled"] is False
    with pytest.raises(acceptance.AcceptanceError):
        acceptance.submit(RUNNING, "0.1.2", True)
    assert not github[2]


def test_lost_local_receipt_reuses_owner_issue(github):
    acceptance.submit(RUNNING, "0.1.2", True)
    acceptance.receipt_path(RUNNING).unlink()
    result = acceptance.submit(RUNNING, "0.1.2", True)
    assert result["issue"] == 91
    assert len(github[2]) == 1


def test_acceptance_readiness_blocks_newer_preview(github):
    refs, _, _, _ = github
    deployments = [{"code": "update_ok", "sha": RUNNING}]
    assert acceptance.acceptance_readiness(RUNNING, "0.1.17", "0.1.17", deployments)["ready"]
    refs["preview"] = HEAD
    gate = acceptance.acceptance_readiness(RUNNING, "0.1.17", "0.1.17", deployments)
    assert not gate["ready"]
    assert gate["preview_sha"] == HEAD


def test_acceptance_readiness_requires_successful_matching_deploy(github):
    assert not acceptance.acceptance_readiness(RUNNING, "0.1.18", "0.1.18", [])["ready"]
    assert not acceptance.acceptance_readiness(RUNNING, "0.1.18", "0.1.18", [
        {"code": "update_failed", "sha": RUNNING}
    ])["ready"]
    assert not acceptance.acceptance_readiness(RUNNING, "0.1.17", "0.1.18", [
        {"code": "update_ok", "sha": RUNNING}
    ])["ready"]


def test_acceptance_readiness_fails_closed_when_github_unreachable(github, monkeypatch):
    monkeypatch.setattr(acceptance, "ref", lambda name: (_ for _ in ()).throw(acceptance.AcceptanceError("offline")))
    gate = acceptance.acceptance_readiness(RUNNING, "0.1.18", "0.1.18", [
        {"code": "update_ok", "sha": RUNNING}
    ])
    assert not gate["ready"]


def test_release_page_reports_new_preview_after_previous_sha_was_published(github, monkeypatch):
    from fastapi import Response
    from app import main

    refs, _, _, _ = github
    refs.update(main=RUNNING, preview=HEAD)
    monkeypatch.setattr(acceptance, "load_receipt", lambda sha: {"issue": 55})
    monkeypatch.setattr(main.operations, "recent_operations", lambda *a, **kw: [
        {"code": "update_ok", "sha": RUNNING}])
    monkeypatch.setattr(main, "deployment_block_reason", lambda sha: "")
    gate = main.release_notes(Response())["acceptance"]
    assert gate["state"] == "published"
    assert gate["preview_sha"] == HEAD
    assert not gate["ready"]
    assert "旧版" in gate["reason"]


def test_active_deployment_overrides_previous_success(github, monkeypatch, tmp_path):
    from fastapi import HTTPException, Response
    from unittest.mock import Mock
    from app import main

    log = tmp_path / "logs" / "operations.tsv"
    log.parent.mkdir()
    log.write_text(f"2026-11-22T13:16:03Z\tupdate_ok\t{RUNNING}\n")
    monkeypatch.setattr(main.operations, "OPERATIONS_LOG", log)
    (tmp_path / ".webapp-deploy.lock").mkdir()
    gate = main.release_notes(Response())["acceptance"]
    assert not gate["ready"]
    assert "部署请求尚未完成" in gate["reason"]
    monkeypatch.setattr(main.auth, "authorize_session", lambda *args: None)
    request = Mock(headers={"origin": "https://base.layho.com"})
    with pytest.raises(HTTPException) as error:
        main.accept_release(main.ReleaseAcceptanceIn(sha=RUNNING, version=main.app.version),
                            request, Response(), None)
    assert error.value.status_code == 409
    assert not github[2]
