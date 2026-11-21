import os
from pathlib import Path
import plistlib
import subprocess
import sys

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.db import Base, get_db
from app.main import app
from deploy.macmini import acceptance


def test_acceptance_agents_use_separate_labels_and_manual_triggers(tmp_path):
    relative = Path("deploy/macmini/manage.sh")
    staged_script = tmp_path / relative
    staged_script.parent.mkdir(parents=True)
    script = (Path(__file__).resolve().parents[1] / relative).read_text()
    before_dispatch = script.split('case "$1" in', 1)[0]
    staged_script.write_text(before_dispatch + """
launchctl() { return 0; }
agents
""")
    bin_dir = tmp_path / ".venv/bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").symlink_to(sys.executable)
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "APP_ACCEPTANCE_ID": "local_test",
           "APP_ACCEPTANCE_PORT": "18018"}
    subprocess.run(["bash", staged_script], env=env, check=True)
    assert not (home / "Library/LaunchAgents").exists()
    plists = (tmp_path / ".webapp-acceptance-agents").glob("*.plist")
    specs = {p.stem.rsplit(".", 1)[-1]: plistlib.loads(p.read_bytes()) for p in plists}
    assert set(specs) == {"web", "updater", "feedback"}
    for role, spec in specs.items():
        assert spec["Label"] == f"com.example.webapp.acceptance.local_test.{role}"
        assert spec["EnvironmentVariables"] == {
            "APP_ACCEPTANCE_ID": "local_test", "APP_ACCEPTANCE_PORT": "18018",
        }
        assert spec["RunAtLoad"] is (role == "web")
        assert "StartInterval" not in spec


def test_acceptance_session_probe_against_app(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    password = "regression-test-password"
    monkeypatch.setenv("APP_PUBLIC_ORIGIN", acceptance.ORIGIN)
    monkeypatch.setenv("APP_ADMIN_USERNAME", "acceptance")
    monkeypatch.setenv("APP_ADMIN_PASSWORD_HASH", hash_password(password))
    monkeypatch.setenv("APP_COOKIE_SECURE", "true")

    def db():
        with Session() as session:
            yield session

    monkeypatch.setitem(app.dependency_overrides, get_db, db)
    with TestClient(app, base_url=acceptance.ORIGIN) as client:
        def request(port, path, expected, body=None, headers=None):
            client.cookies.clear()
            response = client.request(
                "GET" if body is None else "POST", path, json=body,
                headers={"Origin": acceptance.ORIGIN, **(headers or {})},
            )
            assert response.status_code == expected
            return response.json(), response.headers.get("Set-Cookie", "")

        monkeypatch.setattr(acceptance, "local_request", request)
        acceptance.check_session(18018, password)
    engine.dispose()


def test_launcher_skips_an_old_default_python(tmp_path):
    script = Path(acceptance.__file__).with_suffix(".sh")
    old = tmp_path / "python3"
    old.write_text("#!/bin/sh\nexit 1\n")
    ready = tmp_path / "python3.12"
    ready.write_text('#!/bin/sh\n[ "$1" = "-c" ] || printf "%s\\n" "$1"\n')
    for executable in (old, ready):
        executable.chmod(0o700)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    result = subprocess.run(["bash", script], env=env, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == acceptance.__file__


def test_feedback_wait_fails_fast_on_issue_creation_denial(tmp_path, monkeypatch):
    import pytest

    output = tmp_path / "feedback.out.log"
    output.write_text("Feedback sync failed for ID 42: create: GitHub HTTP 403\n")
    monkeypatch.setattr(acceptance, "local_request", lambda *args, **kwargs: ([], ""))
    with pytest.raises(RuntimeError, match="创建 Issue返回 GitHub HTTP 403"):
        acceptance.wait_for_feedback(18018, {}, 42, output)


def test_feedback_wait_accepts_database_evidence_without_launchd_state(tmp_path, monkeypatch):
    row = {"id": 42, "status": "synced", "github_issue_number": 7}
    monkeypatch.setattr(acceptance, "local_request", lambda *args, **kwargs: ([row], ""))
    assert acceptance.wait_for_feedback(18018, {}, 42, tmp_path / "no.log") == 7
