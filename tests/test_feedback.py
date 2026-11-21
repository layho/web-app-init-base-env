"""Feedback API regression: authenticated, CSRF protected, database-backed."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.db import Base, get_db
from app.main import app
from app.auth import hash_password
from scripts import sync_feedback

@pytest.fixture
def client(monkeypatch):
    engine=create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine)
    def override():
        with Session() as db:
            yield db
    app.dependency_overrides[get_db]=override
    monkeypatch.setattr(sync_feedback, "SessionLocal", Session)
    monkeypatch.setenv("APP_ADMIN_USERNAME","admin")
    monkeypatch.setenv("APP_ADMIN_PASSWORD_HASH",hash_password("secure-password-for-tests-123"))
    monkeypatch.setenv("APP_PUBLIC_ORIGIN","https://app.example.com")
    monkeypatch.setenv("APP_COOKIE_SECURE","true")
    monkeypatch.delenv("APP_API_TOKEN",raising=False)
    with TestClient(app,base_url="https://app.example.com") as c:
        yield c
    app.dependency_overrides.clear()
    engine.dispose()

def test_feedback_requires_login_and_csrf(client):
    payload={"category":"bug","message":"Regression: test feedback submission"}
    origin={"Origin":"https://app.example.com"}
    assert client.post("/api/v1/feedback",json=payload,headers=origin).status_code==401
    login=client.post("/api/login",json={"username":"admin","password":"secure-password-for-tests-123"},headers=origin)
    assert login.status_code==200
    assert client.post("/api/v1/feedback",json=payload,headers=origin).status_code==403
    headers={**origin,"X-CSRF-Token":login.json()["csrf_token"]}
    bad=client.post("/api/v1/feedback",json={"category":"secret","message":"Should reject invalid category"},headers=headers)
    assert bad.status_code==422
    created=client.post("/api/v1/feedback",json=payload,headers=headers)
    assert created.status_code==200
    assert created.json()["status"]=="pending"
    records=client.get("/api/v1/feedback")
    assert records.status_code==200
    assert records.json()[0]["id"]==created.json()["id"]
    assert records.json()[0]["github_issue_number"] is None
    assert records.json()[0]["message"]==payload["message"]
    assert client.post("/api/logout",headers=headers).status_code==200
    assert client.get("/api/v1/feedback").status_code==401


def test_feedback_worker_retries_and_reuses_existing_issue(client, monkeypatch):
    from urllib.error import URLError
    from urllib.parse import parse_qs, urlsplit

    origin = {"Origin": "https://app.example.com"}
    login = client.post("/api/login", headers=origin, json={
        "username": "admin", "password": "secure-password-for-tests-123",
    })
    feedback = client.post(
        "/api/v1/feedback",
        headers={**origin, "X-CSRF-Token": login.json()["csrf_token"]},
        json={"category": "acceptance", "message": "Feedback sync regression"},
    ).json()
    marker = f"[APP-FEEDBACK #{feedback['id']}]"
    monkeypatch.setattr(sync_feedback, "TOKEN", "local-test-only")
    calls = []

    def github(method, path, data=None):
        calls.append(method)
        if method == "GET":
            url = urlsplit(path)
            assert url.path == f"/repos/{sync_feedback.REPO}/issues"
            query = parse_qs(url.query)
            assert query["state"] == ["all"]
            assert query["since"][0].endswith("Z")
            if calls == ["GET"]:
                return []
            if query["page"] == ["1"]:
                pull = {"title": f"{marker} acceptance", "number": 41, "pull_request": {}}
                return [pull] + [{"title": "Unrelated issue"}] * 99
            assert query["page"] == ["2"]
            return [{"title": f"{marker} acceptance", "number": 42, "state": "closed"}]
        assert path == f"/repos/{sync_feedback.REPO}/issues"
        assert data["title"] == f"{marker} acceptance"
        assert "Feedback sync regression" in data["body"]
        raise URLError("simulated response lost after issue creation")

    monkeypatch.setattr(sync_feedback, "github", github)
    sync_feedback.main()
    assert client.get("/api/v1/feedback").json()[0]["status"] == "pending"
    sync_feedback.main()
    result = client.get("/api/v1/feedback").json()[0]
    assert result["status"] == "synced"
    assert result["github_issue_number"] == 42
    sync_feedback.main()
    assert calls == ["GET", "POST", "GET", "GET"]
