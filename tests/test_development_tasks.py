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
    assert parse({**ISSUE, "body": "ordinary issue"}, repository) is None
    assert parse({**ISSUE, "pull_request": {}}, repository) is None
    assert parse({**ISSUE, "title": "[APP-FEEDBACK #1] bug"}, repository) is None
    assert parse({**ISSUE, "body": ISSUE["body"].replace("状态：开发中", "")}, repository)["status"] == "未标记"
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
