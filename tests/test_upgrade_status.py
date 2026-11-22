"""Read-only scheduler status and privacy regressions."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

from app import upgrade_status


def fixture(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    (app / ".app-init.json").write_text(json.dumps({"repository": "example/demo-app"}))
    scheduler = tmp_path / "scheduler"
    scheduler.mkdir(mode=0o700)
    scheduler.chmod(0o700)
    monkeypatch.setattr(upgrade_status, "SCHED_ROOT", scheduler)
    return app, scheduler


def save(path, payload):
    path.write_text(json.dumps(payload))
    path.chmod(0o600)


def test_missing_scheduler_is_unregistered(tmp_path, monkeypatch):
    app, root = fixture(tmp_path, monkeypatch)
    assert upgrade_status.snapshot(app)["mode"] == "unregistered"


def test_reports_only_own_app_and_exact_pr_url(tmp_path, monkeypatch):
    app, root = fixture(tmp_path, monkeypatch)
    save(root / "registry.json", {"schema": 1, "apps": {
        "example/demo-app": {"mode": "create", "legacy_base": None},
        "example/secret-app": {"mode": "create", "private_token": "NEVER_EXPOSE"},
    }})
    filename = "status-" + hashlib.sha256(b"example/demo-app").hexdigest() + ".json"
    save(root / filename, {"schema": 1, "repository": "example/demo-app", "mode": "create",
        "state": "upgrade_pending", "pr_url": "https://github.com/example/demo-app/pull/17",
        "target_sha": "a"*40, "app_sha": "b"*40,
        "checked_at": datetime.now(timezone.utc).isoformat(), "review_count": 2,
        "private_token": "NEVER_EXPOSE"})
    result = upgrade_status.snapshot(app)
    assert result["mode"] == "create" and result["state"] == "upgrade_pending"
    assert result["stale"] is False and result["pr_url"].endswith("/17")
    assert "NEVER_EXPOSE" not in json.dumps(result)
    save(root / filename, {**json.loads((root / filename).read_text()),
        "pr_url": "https://github.com/example/secret-app/pull/18"})
    assert upgrade_status.snapshot(app)["pr_url"] is None


def test_stale_heartbeat_and_symlinks_are_not_trusted(tmp_path, monkeypatch):
    app, root = fixture(tmp_path, monkeypatch)
    save(root / "registry.json", {"schema": 1, "apps": {
        "example/demo-app": {"mode": "observe", "legacy_base": None}}})
    filename = "status-" + hashlib.sha256(b"example/demo-app").hexdigest() + ".json"
    save(root / filename, {"schema": 1, "repository": "example/demo-app", "mode": "observe",
        "state": "waiting_adoption", "checked_at":
            (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()})
    result = upgrade_status.snapshot(app)
    assert result["mode"] == "observe" and result["stale"]
    (root / filename).unlink()
    (root / filename).symlink_to(root / "registry.json")
    assert upgrade_status.snapshot(app)["state"] == "awaiting_first_check"
    (root / "registry.json").chmod(0o644)
    assert upgrade_status.snapshot(app)["mode"] == "unregistered"
