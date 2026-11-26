import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import auth, hostops
from app.main import app
from app.hostops_tasks import TaskStore, TaskConflict


@pytest.fixture
def store(tmp_path):
    tmp_path.chmod(0o700)
    return TaskStore(tmp_path)


def test_persistence_and_idempotency(store):
    args = ("admin","macmini-01","host.health","key-123","diagnostics",60)
    first, created = store.enqueue(*args, now=100)
    assert created
    again, created = TaskStore(store.path.parent).enqueue(*args, now=101)
    assert not created and first == again
    with pytest.raises(TaskConflict):
        store.enqueue(*args[:4], "other reason", 60, now=102)
    assert store.list("other", now=103) == []
    assert store.list("macmini-01", now=160)[0]["state"] == "TIMED_OUT"


def test_concurrent_duplicate(store):
    args = ("admin","macmini-01","host.health","key-123","diagnostics",60)
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: store.enqueue(*args, now=100), range(8)))
    assert sum(created for _,created in rows) == 1
    assert len({row["task_id"] for row,_ in rows}) == 1


def test_reject_unsafe_directory(tmp_path):
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError):
        TaskStore(tmp_path)
    tmp_path.chmod(0o700)
    (tmp_path / "tasks.sqlite3").symlink_to(tmp_path / "other")
    with pytest.raises(OSError):
        TaskStore(tmp_path)


def test_task_api(store, monkeypatch):
    monkeypatch.setenv("HOSTOPS_ENABLED", "true")
    monkeypatch.setenv("HOSTOPS_HOST_ID", "macmini-01")
    monkeypatch.setenv("HOSTOPS_AGENT_TOKEN", "a"*40)
    monkeypatch.setenv("HOSTOPS_STATE_DIR", str(store.path.parent))
    client = TestClient(app)
    url = "/api/hostops/v1/tasks"
    payload = {"host_id":"macmini-01","operation":"host.health",
               "idempotency_key":"idempotency-key-123","reason":"diagnostics"}
    assert client.post(url,json=payload).status_code == 401
    monkeypatch.setattr(auth,"authorize_session",lambda *args:"admin")
    assert client.post(url,json={**payload,"operation":"shell"}).status_code == 422
    assert client.post(url,json={**payload,"host_id":"other"}).status_code == 403
    assert client.post(url,json={**payload,"parameters":{"command":"test"}}).status_code == 422
    response = client.post(url,json=payload)
    assert response.status_code == 202
    assert response.json()["dispatch_enabled"] is False
    assert client.post(url,json=payload).status_code == 200
    assert client.post(url,json={**payload,"reason":"other"}).status_code == 409
    assert len(client.get(url).json()["tasks"]) == 1
