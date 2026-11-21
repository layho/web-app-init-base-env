import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.db import Base, get_db
from app.main import app
from app.auth import COOKIE_NAME, hash_password

@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    def db_override():
        with Session() as session:
            yield session
    app.dependency_overrides[get_db] = db_override
    monkeypatch.setenv("APP_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("APP_ADMIN_PASSWORD_HASH", hash_password("A-very-strong-passphrase-123"))
    monkeypatch.setenv("APP_PUBLIC_ORIGIN", "https://webapp.example")
    monkeypatch.setenv("APP_COOKIE_SECURE", "true")
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    with TestClient(app, base_url="https://webapp.example") as test_client:
        yield test_client
    app.dependency_overrides.clear()
    engine.dispose()

def test_login_and_csrf(client):
    assert client.get("/api/v1/feedback").status_code == 401
    assert client.post("/api/login", json={"username":"admin","password":"wrong"},headers={"Origin":"https://webapp.example"}).status_code == 401
    response=client.post("/api/login",json={"username":"admin","password":"A-very-strong-passphrase-123"},headers={"Origin":"https://webapp.example"})
    assert response.status_code==200
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "secure" in response.headers["set-cookie"].lower()
    assert "samesite=strict" in response.headers["set-cookie"].lower()
    assert client.get("/api/v1/feedback").status_code==200
    assert client.post("/api/v1/feedback",json={"category":"bug","message":"Auth regression feedback"},headers={"Origin":"https://webapp.example"}).status_code==403
    token = client.cookies[COOKIE_NAME]
    refreshed = client.get("/api/session")
    assert refreshed.status_code == 200
    assert refreshed.json()["username"] == "admin"
    csrf = refreshed.json()["csrf_token"]
    assert client.post("/api/v1/feedback",json={"category":"bug","message":"Auth regression feedback"},headers={"Origin":"https://webapp.example","X-CSRF-Token":csrf}).status_code==200
    assert client.post("/api/logout",headers={"Origin":"https://webapp.example","X-CSRF-Token":csrf}).status_code==200
    client.cookies.set(COOKIE_NAME, token, path="/api")
    assert client.get("/api/session").status_code == 401
    assert client.get("/api/v1/feedback").status_code == 401

def test_cross_origin_denied(client):
    response=client.post("/api/login",json={"username":"admin","password":"A-very-strong-passphrase-123"},headers={"Origin":"https://evil.example"})
    assert response.status_code==403


def test_operation_views_require_login_and_hide_raw_log_lines(client, monkeypatch, tmp_path):
    from app import operations

    for path in ("/api/release-notes", "/api/v1/system-logs"):
        assert client.get(path).status_code == 401

    sha = "a" * 40
    log = tmp_path / "operations.tsv"
    log.write_text(
        f"2026-11-21T11:00:00Z\tinstall_ok\t{sha}\n"
        f"2026-11-21T11:01:00Z\trollback\t{sha}\n"
        f"2026-11-21T11:02:00Z\tstartup_ok\t{sha}\n"
        f"test-secret\tstartup_ok\t{sha}\n"
        "raw log containing test-secret\n"
        f"2026-11-21T11:03:00Z\tunknown_code\t{sha}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(operations, "OPERATIONS_LOG", log)
    monkeypatch.setenv("APP_RUNNING_SHA", sha)
    assert client.post("/api/login", json={
        "username": "admin", "password": "A-very-strong-passphrase-123",
    }, headers={"Origin": "https://webapp.example"}).status_code == 200

    notes = client.get("/api/release-notes").json()
    assert notes["running_sha"] == sha
    assert [row["code"] for row in notes["deployments"]] == ["install_ok"]
    assert notes["releases"][0]["version"] == notes["version"]
    logs = client.get("/api/v1/system-logs")
    assert [row["code"] for row in logs.json()["events"]] == ["startup_ok", "rollback", "install_ok"]
    assert "test-secret" not in logs.text


def test_manual_update_uses_only_the_configured_launch_agent(client, monkeypatch):
    import os
    from app import operations

    target = f"gui/{os.getuid()}/com.example.webapp.acceptance.smoke.updater"
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        assert kwargs["check"] and kwargs["timeout"] == 5

    monkeypatch.setattr(operations.subprocess, "run", run)
    monkeypatch.setenv("APP_UPDATER_TARGET", target)
    monkeypatch.setenv("APP_API_TOKEN", "test-service-token")
    headers = {"Origin": "https://webapp.example"}
    path = "/api/v1/deploy/update"
    assert client.post(path, headers=headers).status_code == 401
    assert client.post(path, headers={**headers, "Authorization": "Bearer test-service-token"}).status_code == 401
    csrf = client.post("/api/login", json={
        "username": "admin", "password": "A-very-strong-passphrase-123",
    }, headers=headers).json()["csrf_token"]
    assert client.post(path, headers=headers).status_code == 403
    authorized = {**headers, "X-CSRF-Token": csrf}
    assert client.post(path, headers={**authorized, "Origin": "https://evil.example"}).status_code == 403
    assert not calls

    # The target in a client-supplied payload has no effect on the command.
    response = client.post(path, headers=authorized, json={"target": "system/other"})
    assert response.status_code == 202
    assert response.json() == {"accepted": True}
    assert calls == [["/bin/launchctl", "kickstart", target]]
    version = client.get("/api/version")
    assert version.json()["version"] == app.version
    assert version.json()["manual_update_available"] is True
    assert version.headers["cache-control"] == "no-store"

    monkeypatch.setenv("APP_UPDATER_TARGET", "system/other")
    assert client.get("/api/version").json()["manual_update_available"] is False
    assert client.post(path, headers=authorized).status_code == 503
    assert len(calls) == 1


def test_manual_update_returns_unavailable_when_launchctl_fails(client, monkeypatch):
    import os
    import subprocess
    from app import operations

    monkeypatch.setenv("APP_UPDATER_TARGET", f"gui/{os.getuid()}/com.example.webapp.updater")

    def launch_failed(target):
        raise subprocess.CalledProcessError(1, ["/bin/launchctl", "kickstart", target])

    monkeypatch.setattr(operations, "request_update", launch_failed)
    headers = {"Origin": "https://webapp.example"}
    auth = client.post("/api/login", json={
        "username": "admin", "password": "A-very-strong-passphrase-123",
    }, headers=headers)
    response = client.post("/api/v1/deploy/update", headers={**headers, "X-CSRF-Token": auth.json()["csrf_token"]})
    assert response.status_code == 503
    assert "launchctl" not in response.text


def test_development_tasks_require_authentication(client, monkeypatch, tmp_path):
    from app import development_tasks

    monkeypatch.setattr(development_tasks, "CACHE_PATH", tmp_path / "missing.json")
    path = "/api/v1/development-tasks"
    assert client.get(path).status_code == 401
    assert client.post("/api/login", json={
        "username": "admin", "password": "A-very-strong-passphrase-123",
    }, headers={"Origin": "https://webapp.example"}).status_code == 200
    response = client.get(path)
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"synced_at": None, "stale": True, "tasks": []}


def test_project_info_exposes_versioned_design_to_signed_in_users(client):
    path = "/api/v1/project-info"
    assert client.get(path).status_code == 401
    assert client.post("/api/login", json={
        "username": "admin", "password": "A-very-strong-passphrase-123",
    }, headers={"Origin": "https://webapp.example"}).status_code == 200

    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["name"] == "Web App Foundation"
    assert {"浏览器", "应用接口", "数据"} <= {row["layer"] for row in data["architecture"]}
    assert data["workflow"][0]["title"] == "登记需求"
    assert data["tasks"] == []
    assert all(row["acceptance"] for row in data["tasks"])


def test_system_service_update_signals_only_local_updater(monkeypatch, tmp_path):
    from app import operations

    monkeypatch.setenv("APP_INIT_UPDATE_SIGNAL", "1")
    monkeypatch.setattr(operations, "OPERATIONS_LOG", tmp_path / "operations.tsv")
    target = operations.updater_target()
    assert target == "local-signal"
    operations.request_update(target)
    assert (tmp_path / "update-request").read_bytes() == b"1"
