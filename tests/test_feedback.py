"""Feedback API regression: authenticated, CSRF protected, database-backed."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app import feedback_progress
from app.db import Base, get_db
from app.main import app
from app.auth import hash_password
from scripts import sync_feedback

@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback_progress, 'CACHE_PATH', tmp_path / 'feedback-progress.json')
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
    assert records.json()[0]["created_at"]
    assert records.json()[0]["synced_at"] is None
    assert records.json()[0]["overall_status"] == "pending_sync"
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


def test_feedback_developer_reply_refresh_preserves_last_good_state(client, monkeypatch):
    from datetime import timedelta
    from urllib.error import URLError
    from app.models import UserFeedback, utcnow
    import json

    with sync_feedback.SessionLocal() as db:
        row = UserFeedback(category="bug", message="Check development status", status="synced",
                           github_issue_number=13, synced_at=utcnow())
        db.add(row)
        db.commit()
        feedback_id = row.id
    login = client.post("/api/login", headers={"Origin": "https://app.example.com"}, json={
        "username": "admin", "password": "secure-password-for-tests-123",
    })
    assert login.status_code == 200
    monkeypatch.setattr(sync_feedback, "TOKEN", "local-test-only")
    def github(method, path, data=None):
        assert method == "GET"
        assert path == f"/repos/{sync_feedback.REPO}/issues/13/comments?per_page=100&page=1"
        return [{"id": 1, "author_association": "OWNER",
                 "created_at": "2026-11-22T02:01:00Z", "updated_at": "2026-11-22T02:01:00Z",
                 "body": "[APP_PROGRESS] " + json.dumps({"status": "claimed", "reply": "已领取"})}]
    monkeypatch.setattr(sync_feedback, "github", github)
    assert sync_feedback.refresh_progress() == 0
    before = client.get("/api/v1/feedback").json()[0]
    assert before["overall_status"] == "claimed"
    assert before["claimed_at"] == "2026-11-22T02:01:00+00:00"
    assert before["developer_reply"] == "已领取"
    assert before["synced_at"]
    assert before["development_started_at"] is None
    cache = feedback_progress.load_cache()
    assert cache["13"]["feedback_id"] == feedback_id
    cache["13"]["checked_at"] = (utcnow() - timedelta(minutes=11)).isoformat()
    feedback_progress.save_cache(cache)
    def unavailable(*args, **kwargs):
        raise URLError("simulated unavailable GitHub")
    monkeypatch.setattr(sync_feedback, "github", unavailable)
    assert sync_feedback.refresh_progress() == 1
    after = client.get("/api/v1/feedback").json()[0]
    assert after["overall_status"] == "claimed"
    assert after["developer_reply"] == "已领取"
    assert after["progress_stale"]

def test_private_attachment_roundtrip(client,monkeypatch,tmp_path):
    import base64, io
    from PIL import Image
    from app import feedback_attachments
    monkeypatch.setattr(feedback_attachments,"ROOT",tmp_path/"images")
    out=io.BytesIO()
    Image.new("RGB",(12,12),"red").save(out,"PNG")
    screenshot=base64.b64encode(out.getvalue()).decode()
    origin={"Origin":"https://app.example.com"}
    login=client.post("/api/login",headers=origin,json={"username":"admin","password":"secure-password-for-tests-123"})
    headers={**origin,"X-CSRF-Token":login.json()["csrf_token"]}
    payload={"category":"bug","message":"Screenshot evidence test","screenshots":[screenshot]}
    assert client.post("/api/v1/feedback",headers=headers,json=payload).status_code==200
    url=client.get("/api/v1/feedback").json()[0]["attachments"][0]["url"]
    r=client.get(url)
    assert r.status_code==200 and r.headers["content-type"]=="image/jpeg"
    assert client.post("/api/v1/feedback",headers=headers,json={**payload,"screenshots":["invalid"]}).status_code==422
    client.post("/api/logout",headers=headers)
    assert client.get(url).status_code==401


def test_attachment_rejects_aggregate_before_decode(monkeypatch):
    from app import feedback_attachments as a
    monkeypatch.setattr(a, "MAX_TOTAL", 12)
    monkeypatch.setattr(a, "MAX_ENCODED", 32)
    with pytest.raises(ValueError,match="总大小"):
        a.normalize(["A"*32,"A"*32])

def test_attachment_rejects_oversized_encoded_input(monkeypatch):
    from app import feedback_attachments as a
    monkeypatch.setattr(a,"MAX_ENCODED",16)
    with pytest.raises(ValueError,match="单张"):
        a.normalize(["A"*20])


def test_feedback_prefers_shared_token(monkeypatch):
    monkeypatch.setenv("APP_GITHUB_TOKEN","shared-test-token")
    monkeypatch.setenv("APP_GITHUB_FEEDBACK_TOKEN","legacy-test-token")
    assert sync_feedback.feedback_token()=="shared-test-token"
    monkeypatch.delenv("APP_GITHUB_TOKEN")
    monkeypatch.delenv("APP_GITHUB_PR_TOKEN",raising=False)
    assert sync_feedback.feedback_token()=="legacy-test-token"
