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


def test_claim_atomic_and_expiry(store):
    row,_ = store.enqueue("admin","macmini-01","host.health","key-123","diagnostics",120,now=100)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.claim("macmini-01",b"k"*32,now=101), range(8)))
    claims = [x for x in results if x is not None]
    assert len(claims) == 1
    claim = claims[0]
    assert claim["job"]["task_id"] == row["task_id"]
    assert claim["job"]["actor"] == "admin"
    assert store.list("macmini-01",now=102)[0]["state"] == "CLAIMED"
    assert store.list("macmini-01",now=161)[0]["state"] == "UNKNOWN"
    assert store.claim("macmini-01",b"k"*32,now=162) is None


def test_claim_api_auth_and_opt_in(store, monkeypatch):
    monkeypatch.setenv("HOSTOPS_ENABLED","true")
    monkeypatch.setenv("HOSTOPS_HOST_ID","macmini-01")
    monkeypatch.setenv("HOSTOPS_AGENT_TOKEN","a"*40)
    monkeypatch.setenv("HOSTOPS_STATE_DIR",str(store.path.parent))
    monkeypatch.delenv("HOSTOPS_DISPATCH_ENABLED",raising=False)
    client = TestClient(app)
    url = "/api/hostops/v1/agent/claim"
    payload = {"host_id":"macmini-01"}
    headers = {"Authorization":"Bearer "+"a"*40}
    assert client.post(url,json=payload).status_code == 401
    assert client.post(url,json=payload,headers=headers).status_code == 503
    monkeypatch.setenv("HOSTOPS_DISPATCH_ENABLED","true")
    monkeypatch.setenv("HOSTOPS_JOB_SIGNING_KEY","b"*64)
    assert client.post(url,json={"host_id":"other"},headers=headers).status_code == 403
    store.enqueue("admin","macmini-01","host.health","key-123","diagnostics",120)
    response = client.post(url,json=payload,headers=headers)
    assert response.status_code == 200
    assert response.json()["claim"]["job"]["policy_version"] == "readonly-v1"
    assert client.post(url,json=payload,headers=headers).json()["claim"] is None


def test_receipt_idempotency_and_lease(store):
    row,_=store.enqueue("admin","macmini-01","host.health","key","diag",120,now=100)
    claim=store.claim("macmini-01",b"k"*32,now=101)
    task=row["task_id"]
    token=claim["lease_token"]
    result={"operation":"host.health"}
    with pytest.raises(TaskConflict):
        store.report("macmini-01",task,"x"*43,"SUCCESS",result,now=102)
    assert store.report("macmini-01",task,token,"SUCCESS",result,now=102)
    assert not store.report("macmini-01",task,token,"SUCCESS",result,now=200)
    with pytest.raises(TaskConflict):
        store.report("macmini-01",task,token,"FAILED",None,now=103)
    assert store.detail("macmini-01",task)["state"]=="SUCCESS"


def test_diagnostic_api_full_cycle(store, monkeypatch):
    monkeypatch.setenv("HOSTOPS_ENABLED","true")
    monkeypatch.setenv("HOSTOPS_HOST_ID","macmini-01")
    monkeypatch.setenv("HOSTOPS_AGENT_TOKEN","a"*40)
    monkeypatch.setenv("HOSTOPS_DISPATCH_ENABLED","true")
    monkeypatch.setenv("HOSTOPS_JOB_SIGNING_KEY","b"*64)
    monkeypatch.setenv("HOSTOPS_STATE_DIR",str(store.path.parent))
    monkeypatch.setattr(auth,"authorize_session",lambda *args:"admin")
    client=TestClient(app)
    prefix="/api/hostops/v1"
    headers={"Authorization":"Bearer "+"a"*40}
    created=client.post(prefix+"/tasks",json={"host_id":"macmini-01","operation":"host.health",
        "idempotency_key":"full-cycle-key-001","reason":"diagnostic"})
    assert created.status_code==202 and created.json()["dispatch_enabled"]
    task=created.json()["task"]["task_id"]
    claim=client.post(prefix+"/agent/claim",headers=headers,json={"host_id":"macmini-01"}).json()["claim"]
    assert claim["job"]["task_id"]==task
    receipt={"host_id":"macmini-01","task_id":task,"lease_token":claim["lease_token"],
        "outcome":"SUCCESS","result":{"schema":1,"operation":"host.health","observed_at":"2026-11-26T12:00:00+00:00",
        "result":{"system":"Darwin","architecture":"arm64","disk":None}}}
    assert client.post(prefix+"/agent/report",json=receipt).status_code==401
    assert client.post(prefix+"/agent/report",headers=headers,json={**receipt,"lease_token":"x"*43}).status_code==409
    assert client.post(prefix+"/agent/report",headers=headers,json=receipt).json()=={"accepted":True,"created":True}
    assert client.post(prefix+"/agent/report",headers=headers,json=receipt).json()=={"accepted":True,"created":False}
    detail=client.get(prefix+"/tasks/"+task).json()
    assert detail["state"]=="SUCCESS" and detail["receipt"]["result"]["result"]["system"]=="Darwin"
    assert "lease_token" not in detail
    assert client.get(prefix+"/tasks").json()["tasks"][0]["state"]=="SUCCESS"
