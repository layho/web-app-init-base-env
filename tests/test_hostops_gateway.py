from fastapi.testclient import TestClient
import pytest

from app.main import app
from app import hostops, auth


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("HOSTOPS_ENABLED", "true")
    monkeypatch.setenv("HOSTOPS_HOST_ID", "macmini-01")
    monkeypatch.setenv("HOSTOPS_AGENT_TOKEN", "a" * 40)
    monkeypatch.setattr(hostops, "_latest", None)
    return TestClient(app)


def payload():
    return {"host_id": "macmini-01", "system": "Darwin", "architecture": "arm64",
            "disk": None, "services": [], "capabilities": ["host.health"]}


def test_agent_authentication_and_identity(client):
    url = "/api/hostops/v1/agent/heartbeat"
    assert client.post(url, json=payload()).status_code == 401
    headers = {"Authorization": "Bearer " + "a" * 40}
    assert client.post(url, json={**payload(), "host_id": "other"}, headers=headers).status_code == 403
    assert client.post(url, json={**payload(), "capabilities": ["shell"]}, headers=headers).status_code == 422
    assert client.post(url, json=payload(), headers=headers).status_code == 200


def test_admin_view_and_staleness(client, monkeypatch):
    monkeypatch.setattr(auth, "authorize_session", lambda *args: "admin")
    url = "/api/hostops/v1/hosts"
    assert client.get(url).json()["hosts"][0]["state"] == "never_seen"
    monkeypatch.setattr(hostops, "_clock", lambda: 1000)
    assert client.post("/api/hostops/v1/agent/heartbeat", json=payload(),
                       headers={"Authorization": "Bearer " + "a" * 40}).status_code == 200
    assert client.get(url).json()["hosts"][0]["state"] == "online"
    monkeypatch.setattr(hostops, "_clock", lambda: 1091)
    assert client.get(url).json()["hosts"][0]["state"] == "stale"


def test_disabled_gateway(client, monkeypatch):
    monkeypatch.delenv("HOSTOPS_ENABLED")
    assert client.post("/api/hostops/v1/agent/heartbeat", json=payload()).status_code == 503
