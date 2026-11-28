from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, StaticPool
from app.db import Base, get_db, serialize_sqlite
from app.main import app
from app.auth import COOKIE_NAME, hash_password

@pytest.fixture
def client(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'auth.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=NullPool,
    )
    serialize_sqlite(engine)
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
    # Starlette's TestClient keeps one cookie jar and one portal. Sharing that
    # client across threads can send a request with no session cookie, and the
    # server correctly answers 401. Two tabs are two clients with the same cookie.
    def session_from(copy):
        with TestClient(app, base_url="https://webapp.example") as tab:
            tab.cookies.set(COOKIE_NAME, token, path="/api")
            return tab.get("/api/session")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_tab, refreshed = pool.map(session_from, (1, 2))
    assert first_tab.status_code == 200
    assert first_tab.json()["csrf_token"] == response.json()["csrf_token"]
    assert refreshed.status_code == 200
    assert refreshed.json()["username"] == "admin"
    csrf = response.json()["csrf_token"]
    assert refreshed.json()["csrf_token"] == csrf
    assert csrf != token
    assert client.post("/api/v1/feedback",json={"category":"bug","message":"Auth regression feedback"},headers={"Origin":"https://webapp.example","X-CSRF-Token":csrf}).status_code==200
    assert client.post("/api/logout",headers={"Origin":"https://webapp.example","X-CSRF-Token":csrf}).status_code==200
    client.cookies.set(COOKIE_NAME, token, path="/api")
    assert client.get("/api/session").status_code == 401
    assert client.get("/api/v1/feedback").status_code == 401

def test_cross_origin_denied(client):
    response=client.post("/api/login",json={"username":"admin","password":"A-very-strong-passphrase-123"},headers={"Origin":"https://evil.example"})
    assert response.status_code==403


def test_login_rate_limit_is_per_client_with_global_cap(client, monkeypatch):
    from types import SimpleNamespace
    from app import auth

    clock = [0]
    monkeypatch.setattr(auth, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(auth, "_login_attempts", {})
    monkeypatch.setattr(auth, "LOGIN_CLIENT_LIMIT", 1)
    monkeypatch.setattr(auth, "LOGIN_GLOBAL_LIMIT", 2)
    monkeypatch.setattr(auth, "check_password", lambda password, _: password == "correct")

    def attempt(address, password="wrong", forwarded="198.51.100.100"):
        with TestClient(app, base_url="https://webapp.example", client=(address, 12345)) as peer:
            return peer.post("/api/login", json={"username": "admin", "password": password},
                             headers={"X-Forwarded-For": forwarded}).status_code

    assert attempt("192.0.2.1") == 401
    assert attempt("192.0.2.1", "correct", "198.51.100.101") == 429
    assert attempt("192.0.2.2", "correct") == 200
    assert attempt("192.0.2.2") == 401
    assert attempt("192.0.2.3", "correct") == 429
    clock[0] = auth.LOGIN_WINDOW_SECONDS
    assert attempt("192.0.2.1", "correct") == 200


def test_security_headers_use_https_and_html_boundaries(client):
    response = client.get("/api/version")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["strict-transport-security"] == "max-age=31536000"
    assert "content-security-policy" not in response.headers
    with TestClient(app, base_url="http://webapp.example") as local:
        assert "strict-transport-security" not in local.get("/api/version").headers


def test_login_prunes_only_one_batch_of_expired_sessions(client, monkeypatch):
    from datetime import timedelta
    from sqlalchemy import select
    from app import auth
    from app.models import AdminSession, utcnow

    now = utcnow()
    monkeypatch.setattr(auth, "utcnow", lambda: now)
    monkeypatch.setattr(auth, "SESSION_CLEANUP_BATCH", 2)
    sessions = app.dependency_overrides[get_db]()
    db = next(sessions)
    try:
        expiry = {"oldest": now - timedelta(days=1),
                  "boundary": now, "boundary-next": now,
                  "active": now + timedelta(seconds=1)}
        db.add_all(AdminSession(token_hash=auth.token_hash(token), csrf_hash="unused",
                                username="admin", expires_at=expires)
                   for token, expires in expiry.items())
        db.commit()
        response = client.post("/api/login", json={
            "username": "admin", "password": "A-very-strong-passphrase-123",
        })
        assert response.status_code == 200
        assert set(db.scalars(select(AdminSession.token_hash))) == {
            auth.token_hash("boundary-next"), auth.token_hash("active"),
            auth.token_hash(client.cookies[COOKIE_NAME]),
        }
    finally:
        sessions.close()


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
    guide = data["operating_guide"]
    assert guide["scope"] == "foundation"
    assert len(guide["sections"]) >= 5
    assert {"before", "initialize", "develop", "downstream", "rebuild"} <= {row["id"] for row in guide["sections"]}
    assert {"web-accept", "publish-main", "upgrade-scheduler", "app-test-deploy"} <= {row["id"] for row in data["development_loop"]}
    assert any("8018" in step["detail"] for section in guide["sections"] for step in section["steps"])
    assert "app_development_loop" in json.loads((Path(__file__).resolve().parents[1] / "app/project_info.json").read_text())
    assert all(row["acceptance"] for row in data["tasks"])


def test_system_service_update_signals_only_local_updater(monkeypatch, tmp_path):
    from app import operations

    monkeypatch.setenv("APP_INIT_UPDATE_SIGNAL", "1")
    monkeypatch.setattr(operations, "OPERATIONS_LOG", tmp_path / "operations.tsv")
    target = operations.updater_target()
    assert target == "local-signal"
    operations.request_update(target)
    assert (tmp_path / "update-request").read_bytes() == b"1"


def test_release_acceptance_requires_admin_session_csrf_origin_and_current_version(client, monkeypatch, tmp_path):
    import json
    import app.main as api_main
    from app import release_acceptance, operations

    path = "/api/v1/releases/accept"
    sha = "b" * 40
    payload = {"sha": sha, "version": app.version}
    origin = {"Origin": "https://webapp.example"}
    assert client.post(path, json=payload, headers=origin).status_code == 401
    monkeypatch.setenv("APP_API_TOKEN", "service-only-test")
    assert client.post(path, json=payload, headers={**origin, "Authorization": "Bearer service-only-test"}).status_code == 401
    monkeypatch.setattr(release_acceptance, "enabled", lambda: True)
    notes = tmp_path / "release_notes.json"
    notes.write_text(json.dumps([{"version": app.version, "acceptance_required": True}]))
    monkeypatch.setattr(api_main, "RELEASE_NOTES", notes)
    log = tmp_path / "logs" / "operations.tsv"
    log.parent.mkdir()
    log.write_text(f"2026-11-22T13:16:03Z\tupdate_ok\t{sha}\n")
    monkeypatch.setattr(operations, "OPERATIONS_LOG", log)
    calls = []
    def submit(candidate, version, deployed):
        calls.append((candidate, version, deployed))
        return {"enabled": True, "state": "queued", "issue": 91}
    monkeypatch.setattr(release_acceptance, "submit", submit)
    login = client.post("/api/login", json={"username": "admin", "password": "A-very-strong-passphrase-123"}, headers=origin)
    assert login.status_code == 200
    assert client.post(path, json=payload, headers=origin).status_code == 403
    headers = {**origin, "X-CSRF-Token": login.json()["csrf_token"]}
    assert client.post(path, json=payload, headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    assert client.post(path, json=payload, headers={"X-CSRF-Token": headers["X-CSRF-Token"]}).status_code == 403
    assert client.post(path, json={**payload, "version": "old"}, headers=headers).status_code == 409
    assert not calls
    response = client.post(path, json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json()["state"] == "queued"
    assert calls == [(sha, app.version, True)]


def test_deploy_status_requires_login_and_reports_unacknowledged(client, monkeypatch, tmp_path):
    import os
    import time
    from app import operations
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(operations, "OPERATIONS_LOG", logs / "operations.tsv")
    signal = logs / "update-request"
    signal.write_bytes(b"1")
    os.utime(signal, (time.time()-120, time.time()-120))
    path = "/api/v1/deploy/status"
    assert client.get(path).status_code == 401
    assert client.post("/api/login", json={
        "username": "admin", "password": "A-very-strong-passphrase-123",
    }, headers={"Origin":"https://webapp.example"}).status_code == 200
    assert client.get(path).json()["state"] == "request_unacknowledged"


def test_deploy_status_does_not_claim_success_for_different_running_sha(monkeypatch, tmp_path):
    from app import operations
    from datetime import datetime, timezone
    log = tmp_path / "operations.tsv"
    monkeypatch.setattr(operations, "OPERATIONS_LOG", log)
    stamp = datetime.now(timezone.utc).isoformat()
    log.write_text(stamp + "\tupdate_ok\t" + "a"*40 + "\n")
    assert operations.deploy_status("b"*40)["state"] == "running_sha_mismatch"
    assert operations.deploy_status("a"*40)["state"] == "update_ok"


def test_draft_release_requires_admin_session_csrf_and_base_preview(client, monkeypatch):
    from app import pr_draft_controls
    payload = {"number": 54, "head": "a" * 40}
    calls = []
    monkeypatch.setattr(pr_draft_controls, "release", lambda n, h: calls.append((n, h)) or {"released": True})
    endpoint = "/api/v1/pr-drafts/release"
    origin = {"Origin": "https://webapp.example"}
    assert client.post(endpoint, json=payload, headers=origin).status_code == 401
    csrf = client.post("/api/login", json={
        "username": "admin", "password": "A-very-strong-passphrase-123"
    }, headers=origin).json()["csrf_token"]
    assert client.post(endpoint, json=payload, headers=origin).status_code == 403
    assert client.post(endpoint, json=payload, headers={
        "Origin": "https://evil.example", "X-CSRF-Token": csrf
    }).status_code == 403
    assert not calls
    response = client.post(endpoint, json=payload, headers={
        **origin, "X-CSRF-Token": csrf
    })
    assert response.status_code == 200
    assert calls == [(54, "a" * 40)]
    assert client.post(endpoint, json={"number": -1, "head": "not-a-sha"}, headers={
        **origin, "X-CSRF-Token": csrf
    }).status_code == 422


def test_runtime_branding_persists_and_updates_public_title_source(client, monkeypatch, tmp_path):
    from app import admin_settings
    store = tmp_path / "admin-settings.json"
    monkeypatch.setattr(admin_settings, "SETTINGS", store)
    initial = client.get("/api/branding")
    assert initial.status_code == 200
    assert initial.json()["reference_name"]
    login = client.post("/api/login", json={
        "username": "admin", "password": "A-very-strong-passphrase-123"
    }, headers={"Origin": "https://webapp.example"})
    csrf = login.json()["csrf_token"]
    endpoint = "/api/v1/account/settings"
    assert client.post(endpoint, json={
        "workspace_name": "My Workspace", "reference_name": "Custom Title"
    }).status_code == 403
    headers = {"Origin": "https://webapp.example", "X-CSRF-Token": csrf}
    response = client.post(endpoint, headers=headers, json={
        "workspace_name": "My Workspace", "reference_name": "Custom Title"
    })
    assert response.status_code == 200, response.text
    assert response.json() == {"workspace_name": "My Workspace", "reference_name": "Custom Title"}
    assert client.get("/api/branding").json()["reference_name"] == "Custom Title"
    assert client.get(endpoint).json()["workspace_name"] == "My Workspace"
    assert store.stat().st_mode & 0o077 == 0
    assert client.post(endpoint, headers=headers, json={
        "workspace_name": " Bad", "reference_name": "Custom Title"
    }).status_code == 422
    assert client.get(endpoint).json()["workspace_name"] == "My Workspace"


def test_password_change_revokes_sessions_and_uses_new_hash(client, monkeypatch, tmp_path):
    from app import admin_settings
    monkeypatch.setattr(admin_settings, "SETTINGS", tmp_path / "private.json")
    origin = {"Origin": "https://webapp.example"}
    first = client.post("/api/login", json={
        "username": "admin", "password": "A-very-strong-passphrase-123"
    }, headers=origin)
    csrf = first.json()["csrf_token"]
    endpoint = "/api/v1/account/password"
    assert client.post(endpoint, json={
        "current_password": "A-very-strong-passphrase-123", "new_password": "Different-strong-password-456"
    }, headers=origin).status_code == 403
    headers = {**origin, "X-CSRF-Token": csrf}
    wrong = client.post(endpoint, headers=headers, json={
        "current_password": "wrong", "new_password": "Different-strong-password-456"
    })
    assert wrong.status_code == 403
    changed = client.post(endpoint, headers=headers, json={
        "current_password": "A-very-strong-passphrase-123", "new_password": "Different-strong-password-456"
    })
    assert changed.status_code == 200, changed.text
    assert changed.json()["sessions_revoked"] is True
    assert client.get("/api/session").status_code == 401
    assert client.post("/api/login", json={
        "username": "admin", "password": "A-very-strong-passphrase-123"
    }, headers=origin).status_code == 401
    assert client.post("/api/login", json={
        "username": "admin", "password": "Different-strong-password-456"
    }, headers=origin).status_code == 200
    stored = admin_settings.read()
    assert "Different-strong-password-456" not in str(stored)
    assert stored["password_hash"].startswith("pbkdf2_sha256$")


def test_private_settings_rejects_symlink(monkeypatch, tmp_path):
    from app import admin_settings
    actual = tmp_path / "real.json"
    actual.write_text('{"schema":1}')
    shortcut = tmp_path / "shortcut.json"
    shortcut.symlink_to(actual)
    monkeypatch.setattr(admin_settings, "SETTINGS", shortcut)
    import pytest
    with pytest.raises(ValueError):
        admin_settings.read()


def test_shared_connection_login_is_visible_before_commit():
    """One SQLite connection has one transaction.

    A session lookup on that connection sees the login row before commit and
    loses it if the login rolls back. That is the 401 race. Independent
    connections must not observe the uncommitted row, and a new connection
    must observe it after commit.
    """
    from datetime import timedelta
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool, StaticPool
    from app.models import AdminSession, utcnow

    shared = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(shared)
    Shared = sessionmaker(bind=shared, expire_on_commit=False)
    reader = Shared()
    writer = Shared()
    assert reader.connection().connection.dbapi_connection is writer.connection().connection.dbapi_connection
    now = utcnow()
    writer.add(AdminSession(token_hash="a" * 64, csrf_hash="b" * 64, username="admin", expires_at=now + timedelta(hours=1)))
    writer.flush()
    leaked = reader.scalar(select(AdminSession).where(AdminSession.token_hash == "a" * 64))
    assert leaked is not None and leaked.username == "admin"
    writer.rollback()
    assert reader.scalar(select(AdminSession).where(AdminSession.token_hash == "a" * 64)) is None
    reader.close()
    writer.close()
    shared.dispose()


def test_independent_connection_login_is_hidden_until_commit(tmp_path):
    from datetime import timedelta
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool
    from app.models import AdminSession, utcnow

    engine = create_engine(
        f"sqlite:///{tmp_path / 'auth.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=NullPool,
    )
    from sqlalchemy import event
    @event.listens_for(engine, "connect")
    def _wal(dbapi, _record):
        cursor = dbapi.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    writer = Session()
    reader = Session()
    assert writer.connection().connection.dbapi_connection is not reader.connection().connection.dbapi_connection
    now = utcnow()
    writer.add(AdminSession(token_hash="c" * 64, csrf_hash="d" * 64, username="admin", expires_at=now + timedelta(hours=1)))
    writer.flush()
    assert reader.scalar(select(AdminSession).where(AdminSession.token_hash == "c" * 64)) is None
    writer.commit()
    reader.close()
    fresh = Session()
    found = fresh.scalar(select(AdminSession).where(AdminSession.token_hash == "c" * 64))
    assert found is not None and found.username == "admin"
    fresh.close()
    writer.close()
    engine.dispose()


def test_branding_default_and_control_characters(monkeypatch, tmp_path):
    from app import admin_settings
    monkeypatch.setattr(admin_settings, "SETTINGS", tmp_path / "brand.json")
    monkeypatch.setenv("APP_BASE_PREVIEW", "1")
    assert admin_settings.public()["reference_name"] == "基座验收中心"
    import pytest
    with pytest.raises(ValueError):
        admin_settings.validate_name("Title\nInjected")
    with pytest.raises(ValueError):
        admin_settings.validate_name(" ")
