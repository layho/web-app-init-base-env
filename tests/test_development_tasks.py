from datetime import datetime, timedelta, timezone
import json
from urllib.error import URLError

import pytest

from app import development_tasks
from scripts import sync_development_tasks

ISSUE = {
    "number": 42,
    "title": "模块联调",
    "updated_at": "2026-11-21T11:00:00Z",
    "body": """## 开发进度
状态：开发中
更新说明：接口已通过测试。
- [x] 接口测试
- [ ] 浏览器验收

## 其他事项
- [x] 不计入进度
""",
}


def test_explicit_development_progress():
    parse = development_tasks.parse_issue
    repository = "owner/repo"
    task = parse(ISSUE, repository)
    assert task["status"] == "开发中"
    assert task["note"] == "接口已通过测试。"
    assert (task["completed_steps"], task["total_steps"]) == (1, 2)
    assert task["url"] == "https://github.com/owner/repo/issues/42"
    ordinary = parse({**ISSUE, "body": "ordinary issue"}, repository)
    assert ordinary["number"] == 42
    assert ordinary["status"] == "未记录开发状态"
    assert ordinary["completed_steps"] == ordinary["total_steps"] == 0
    assert parse({**ISSUE, "pull_request": {}}, repository) is None
    assert parse({**ISSUE, "title": "[APP-FEEDBACK #1] bug"}, repository)["origin"] == "feedback"
    assert parse({**ISSUE, "body": ISSUE["body"].replace("状态：开发中", "")}, repository)["status"] == "未记录开发状态"
    assert parse({**ISSUE, "state": "closed", "state_reason": "not_planned"}, repository)["status"] == "已关闭"


def test_sync_keeps_last_snapshot_on_github_failure(tmp_path, monkeypatch):
    cache = tmp_path / "development-tasks.json"
    monkeypatch.setattr(development_tasks, "CACHE_PATH", cache)
    calls = []

    def github(method, path):
        calls.append((method, path))
        return [ISSUE]

    monkeypatch.setattr(sync_development_tasks, "github", github)
    sync_development_tasks.sync()
    snapshot = development_tasks.snapshot()
    assert snapshot["tasks"][0]["number"] == 42
    assert snapshot["stale"] is False
    sync_development_tasks.sync()
    assert len(calls) == 1
    assert calls[0][0] == "GET" and "state=all" in calls[0][1]

    stale = {**snapshot, "synced_at": (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()}
    cache.write_text(json.dumps(stale), encoding="utf-8")

    def offline(*args):
        raise URLError("unavailable")

    monkeypatch.setattr(sync_development_tasks, "github", offline)
    with pytest.raises(URLError):
        sync_development_tasks.sync()
    assert development_tasks.snapshot() == {**stale, "stale": True}


def test_unformatted_issue_is_visible_but_does_not_claim_progress():
    issue = {**ISSUE, "title": "Improve API", "body": "Please add an endpoint", "state": "open"}
    task = development_tasks.parse_issue(issue, "owner/repo")
    assert task["status"] == "未记录开发状态"
    assert task["note"] == ""
    assert task["total_steps"] == 0
    assert development_tasks.parse_issue({**issue, "state":"closed", "state_reason":"completed"}, "owner/repo")["status"] == "已完成"


def test_unified_tasks_include_merged_pr_without_issue():
    issue = development_tasks.parse_issue(ISSUE, "owner/repo")
    result = development_tasks.unified_tasks([issue], {"prs": [
        {"number":77,"title":"Manual PR actions","url":"https://github.com/owner/repo/pull/77",
         "head":"a"*40,"base":"b"*40,"test":{"state":"success","finished_at":"2026-11-23T04:26:55Z"},
         "merge_state":"merged","deployment":{"state":"unverified"},"merged_at":"2026-11-23T04:27:00Z"}
    ]})
    assert [x["key"] for x in result] == ["issue-42", "pr-77"]
    row = result[1]
    assert row["source"] == "pr" and row["pr_number"] == 77
    assert row["test_state"] == "success"
    assert row["merge_state"] == "merged"
    assert row["deployment_state"] == "unverified"
    assert row["delivery_status"] == "已合并 · 部署待核验"
    assert row["status"] != "已完成"
    assert row["url"].endswith("/pull/77")


def test_unified_tasks_do_not_treat_macci_success_as_release():
    prs = [
        {"number":71,"title":"Feedback center","url":"url", "test":{"state":"success"},
         "merge_state":"open","deployment":{"state":"not_merged"}},
        {"number":72,"title":"Draft","url":"url","test":{"state":"unknown"},
         "merge_state":"draft","deployment":{"state":"not_merged"}},
        {"number":73,"title":"Conflict","url":"url", "test":{"state":"success"},
         "merge_state":"draft","deployment":{"state":"not_merged"},
         "conflict":{"blocked":True}},
    ]
    tasks = development_tasks.unified_tasks([], {"prs": prs})
    assert tasks[0]["delivery_status"] == "测试通过 · 待合并"
    assert tasks[1]["status"] == "开发中"
    assert tasks[2]["status"] == "受阻"
    assert all(task["status"] != "已完成" for task in tasks)


def test_unified_tasks_disambiguate_same_number_for_issue_and_pr():
    issue = development_tasks.parse_issue({**ISSUE, "number":77}, "owner/repo")
    result = development_tasks.unified_tasks([issue], {"prs":[
        {"number":77,"title":"PR","test":{"state":"pending"},
         "merge_state":"open","deployment":{"state":"not_merged"}}
    ]})
    assert {x["key"] for x in result} == {"issue-77","pr-77"}
    assert len(result) == 2


def test_unified_tasks_handles_missing_or_partial_tracking():
    issue = development_tasks.parse_issue(ISSUE, "owner/repo")
    assert len(development_tasks.unified_tasks([issue], {})) == 1
    row = development_tasks.unified_tasks([], {"prs":[{"number":80,"title":"Partial PR"}]})[0]
    assert row["delivery_status"] == "等待测试"
    assert row["deployment_state"] == "unverified"


def test_one_issue_can_have_multiple_prs_with_delivery_history():
    issue = development_tasks.parse_issue({
        **ISSUE, "body": ISSUE["body"] + "\n## 关联 PR\n- #81\n- #82\n",
        "assignees": [{"login": "developer-a"}]}, "owner/repo")
    prs = [
        {"number":81, "title":"Frontend", "owner":"a", "test":{"state":"success"},
         "merge_state":"merged","deployment":{"state":"deployed"}},
        {"number":82, "title":"Backend", "owner":"b", "test":{"state":"pending"},
         "merge_state":"open","deployment":{"state":"not_merged"}},
    ]
    result = development_tasks.unified_tasks([issue], {"prs":prs})
    assert len(result) == 1
    assert result[0]["pr_numbers"] == [81,82]
    assert result[0]["owner"] == "developer-a"
    assert result[0]["test_state"] == "pending"
    assert result[0]["merge_state"] == "open"
    assert len(result[0]["prs"]) == 2
    assert result[0]["delivery_status"] == "代码开发与合并中"


def test_pr_explicit_issue_reference_links_and_preserves_unrelated():
    issue = development_tasks.parse_issue(ISSUE, "owner/repo")
    prs = [
        {"number":83,"title":"One", "linked_issues":[42], "test":{"state":"success"},
         "merge_state":"merged","deployment":{"state":"unverified"}},
        {"number":84,"title":"Other", "linked_issues":[99], "test":{"state":"success"},
         "merge_state":"merged","deployment":{"state":"running"}},
    ]
    result = development_tasks.unified_tasks([issue], {"prs":prs})
    assert len(result) == 2
    assert result[0]["pr_numbers"] == [83]
    assert result[1]["key"] == "pr-84"
    assert result[1]["delivery_status"] == "当前版本运行中 · 待验收确认"


def test_issue_body_pr_references_only_from_explicit_section():
    issue = development_tasks.parse_issue({**ISSUE,
      "body":"See #91\n\n## 关联 PR\n- #92\n- #92\n\n## Notes\n#93",
      "assignees":[]}, "owner/repo")
    assert issue["linked_prs"] == [92]
    assert issue["owner"] == "未指定"


def test_feedback_task_uses_shared_json_protocol():
    issue = {**ISSUE, "title": "[APP-FEEDBACK #2] bug", "state": "open", "body": ""}
    def receipt(number, status, timestamp, **fields):
        return {"id": number, "author_association": "OWNER",
                "user": {"login": "layho"}, "created_at": timestamp,
                "updated_at": timestamp,
                "body": "[APP_PROGRESS] " + json.dumps({"status": status, "agent": "Codex", **fields})}
    claim = receipt(1, "claimed", "2026-11-24T00:01:00Z")
    start = receipt(2, "in_progress", "2026-11-24T00:02:00Z")
    row = development_tasks.parse_issue(issue, "owner/repo", [start, claim])
    assert row["owner"] == "Codex"
    assert row["github_actor"] == "layho"
    assert row["claimed_at"] == "2026-11-24T00:01:00+00:00"
    assert row["development_started_at"] == "2026-11-24T00:02:00+00:00"
    assert row["status"] == "开发中"
    invalid = {**start, "body": "[APP_PROGRESS] status=in_progress\\nagent=Grok"}
    outsider = {**start, "author_association": "NONE"}
    row = development_tasks.parse_issue(issue, "owner/repo", [invalid, outsider])
    assert row["owner"] == "未指定"
    assert row["claimed_at"] is None
    assert row["development_started_at"] is None
    assert row["status"] == "未记录开发状态"


def test_unknown_deployment_is_not_success():
    issue = development_tasks.parse_issue(ISSUE, "owner/repo")
    rows = development_tasks.unified_tasks([issue], {"prs": [
        {"number": 81, "linked_issues": [42], "merge_state": "merged",
         "test": {"state": "success"}, "deployment": {"state": "unknown"}}
    ]})
    assert rows[0]["delivery_status"] == "代码已合并 · 部署待核验"


def test_force_issue_refresh_uses_verified_repository_and_bypasses_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(development_tasks, "CACHE_PATH", tmp_path / "tasks.json")
    calls = []
    def request(method, path):
        calls.append((method, path))
        assert method == "GET"
        assert path.startswith("/repos/owner/verified/")
        if "/comments?" in path:
            return [{"id": 1, "author_association": "OWNER",
                     "user": {"login": "owner"},
                     "created_at": "2026-11-24T00:00:00Z",
                     "updated_at": "2026-11-24T00:00:00Z",
                     "body": '[APP_PROGRESS] {"status":"claimed","agent":"Codex"}'}]
        return [{**ISSUE, "comments": 1}]
    sync_development_tasks.sync(force=True, repository="owner/verified", request=request)
    assert development_tasks.snapshot()["tasks"][0]["owner"] == "Codex"
    assert development_tasks.snapshot()["tasks"][0]["url"] == "https://github.com/owner/verified/issues/42"
    sync_development_tasks.sync(repository="owner/verified", request=request)
    assert len(calls) == 2
    sync_development_tasks.sync(force=True, repository="owner/verified", request=request)
    assert len(calls) == 4


def test_incremental_sync_reuses_only_unchanged_same_repo_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(development_tasks, "CACHE_PATH", tmp_path / "tasks.json")
    calls = []
    issue = {**ISSUE, "comments": 1, "updated_at": "2026-11-24T00:00:00Z"}
    def request(method, path):
        calls.append(path)
        if "/comments?" in path:
            return [{"id": 1, "author_association": "OWNER",
                     "user": {"login": "owner"},
                     "created_at": "2026-11-24T00:00:00Z",
                     "updated_at": "2026-11-24T00:00:00Z",
                     "body": '[APP_PROGRESS] {"status":"claimed","agent":"Codex"}'}]
        return [issue]
    def run(repo):
        sync_development_tasks.sync(force=True, repository=repo, request=request, incremental=True)
    run("owner/one")
    assert len(calls) == 2
    run("owner/one")
    assert len(calls) == 3
    assert development_tasks.snapshot()["tasks"][0]["owner"] == "Codex"
    issue["updated_at"] = "2026-11-24T00:01:00Z"
    run("owner/one")
    assert len(calls) == 5
    run("owner/two")
    assert len(calls) == 7
    assert development_tasks.snapshot()["tasks"][0]["url"].startswith("https://github.com/owner/two/")


def test_enqueue_refresh_deduplicates_and_reports_spawn_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(development_tasks, "CACHE_PATH", tmp_path / "tasks.json")
    calls = []
    def spawn(*args, **kwargs):
        calls.append(args)
    monkeypatch.setattr(sync_development_tasks.subprocess, "Popen", spawn)
    assert sync_development_tasks.enqueue_refresh()["state"] == "queued"
    assert sync_development_tasks.enqueue_refresh()["state"] == "queued"
    assert len(calls) == 1
    sync_development_tasks.write_status({"state": "success"})
    def fail(*args, **kwargs):
        raise OSError("spawn failed")
    monkeypatch.setattr(sync_development_tasks.subprocess, "Popen", fail)
    assert sync_development_tasks.enqueue_refresh()["state"] == "failed"
