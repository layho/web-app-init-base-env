import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess

import pytest

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("app_init", SOURCE / "deploy/app-init/app_init.py")
app_init = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_init)


def test_cloudflare_first_run_prompts_once_and_saves_private_config(tmp_path, monkeypatch):
    values = iter(["a" * 32, "b" * 32, "12345678-abcd-abcd-abcd-123456789abc"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(values))
    monkeypatch.setattr(app_init, "getpass", lambda prompt: "sample_token")
    path = tmp_path / "config" / "cloudflare.json"
    config = app_init.load_cloudflare_config(path)
    assert config["api_token"] == "sample_token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert app_init.load_cloudflare_config(path) == config
    path.chmod(0o644)
    with pytest.raises(RuntimeError, match="权限为 600"):
        app_init.load_cloudflare_config(path)


def test_cloudflare_first_run_does_not_save_invalid_config(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "invalid")
    monkeypatch.setattr(app_init, "getpass", lambda prompt: "sample_token")
    path = tmp_path / "cloudflare.json"
    with pytest.raises(RuntimeError, match="account_id"):
        app_init.load_cloudflare_config(path)
    assert not path.exists()


def test_install_deps_only_installs_missing_homebrew_packages(monkeypatch):
    installed = []

    def which(command, **kwargs):
        return command if command == "brew" or installed else None

    def pg_bin():
        if not installed:
            raise RuntimeError("missing postgres")
        return Path("/opt/homebrew/opt/postgresql@17/bin")

    def run(command, **kwargs):
        if command[0] == "brew":
            installed.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(app_init.shutil, "which", which)
    monkeypatch.setattr(app_init, "pg_bin", pg_bin)
    monkeypatch.setattr(app_init.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="--install-deps"):
        app_init.check_dependencies(install=False)
    assert installed == []
    app_init.check_dependencies(install=True)
    app_init.check_dependencies(install=True)
    assert installed == [["brew", "install", "node", "postgresql@17"]]


def test_https_service_credential_is_private_and_scoped(tmp_path):
    root = tmp_path / "app with spaces"
    subprocess.run(["git", "init", "-q", root], check=True)
    app_init.configure_service_git(root, "example/demo-app", "test_token", "http://127.0.0.1:7890")
    credential_file = root / ".git" / "app-init-credentials"
    assert stat.S_IMODE(credential_file.stat().st_mode) == 0o600
    assert app_init.git(root, "config", "--local", "http.proxy") == "http://127.0.0.1:7890"

    def fill(path):
        return subprocess.run(
            ["git", "-C", root, "credential", "fill"],
            input=f"protocol=https\nhost=github.com\npath={path}\n\n",
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"},
            capture_output=True, text=True,
        )

    assert "password=test_token" in fill("example/demo-app.git").stdout
    assert fill("example/other.git").returncode != 0


def test_seeds_an_empty_git_repo_and_reuses_existing_main(tmp_path, monkeypatch):
    source_version = {"schema": 1, "repository": "layho/web-app-init-base-env", "sha": "a" * 40, "version": "0.1.5"}
    monkeypatch.setattr(app_init, "foundation_provenance", lambda source: source_version)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
    repository = "example/demo-app"
    domain = "demo.example.com"
    args = (repository, "demo-app", domain, 18123, "com.appinit.demo")
    repo = tmp_path / "first"
    meta = app_init.seed(SOURCE, str(remote), repo, *args)
    assert meta["repository"] == repository
    assert meta["domain"] == domain
    assert meta["port"] == 18123
    assert meta["label"] == "com.appinit.demo"
    assert meta["foundation"] == source_version
    assert (repo / "app/foundation_version.py").is_file()
    assert (repo / "app/upgrade_status.py").is_file()
    assert (repo / "tests/test_upgrade_status.py").is_file()
    assert "基座版本" in (repo / "frontend/src/ProjectInfoPage.tsx").read_text()
    initial = app_init.git(repo, "rev-parse", "HEAD")
    assert "hash_password" in (repo / "app/auth.py").read_text()
    assert not (repo / ".env").exists()
    assert not (repo / "app/ti_client.py").exists()
    assert "class Product" not in (repo / "app/models.py").read_text()
    assert json.loads((repo / "app/project_info.json").read_text())["tasks"] == []
    assert "example/demo-app" in (repo / "scripts/sync_feedback.py").read_text()
    assert (repo / "deploy/macmini/release.py").exists()
    assert app_init.seed(SOURCE, str(remote), tmp_path / "second", *args) == meta
    assert app_init.git(tmp_path / "second", "rev-parse", "HEAD") == initial
    assert app_init.git(repo, "rev-list", "--count", "HEAD") == "1"


def test_cloudflare_mapping_preserves_routes_and_rejects_conflicts(monkeypatch):
    config = {"zone_id": "zone", "account_id": "account", "tunnel_id": "test-tunnel"}
    old_route = {"hostname": "other.example.com", "service": "http://127.0.0.1:8018"}
    fallback = {"service": "http_status:404"}
    remote = {"originRequest": {"connectTimeout": 8}, "ingress": [old_route, fallback]}
    calls = []

    def api(config, method, path, data=None):
        calls.append((method, path, data))
        if method != "GET":
            return {}
        if "dns_records" in path:
            return []
        return {"config": remote}

    monkeypatch.setattr(app_init, "cloudflare", api)
    app_init.map_domain(config, "demo.example.com", 18123)
    assert [method for method, _, _ in calls] == ["GET", "GET"]
    calls.clear()
    app_init.map_domain(config, "demo.example.com", 18123, apply=True)
    assert [method for method, _, _ in calls] == ["GET", "GET", "PUT", "POST"]
    new_rule, *others = calls[2][2]["config"]["ingress"]
    assert new_rule == {"hostname": "demo.example.com", "service": "http://127.0.0.1:18123"}
    assert others == [old_route, fallback]
    assert calls[2][2]["config"]["originRequest"] == remote["originRequest"]
    calls.clear()
    with pytest.raises(RuntimeError, match="其他回源"):
        app_init.map_domain(config, "other.example.com", 18123, apply=True)
    assert [method for method, _, _ in calls] == ["GET", "GET"]


def test_db_job_repairs_only_known_legacy_plist(tmp_path, monkeypatch):
    import plistlib

    monkeypatch.setattr(app_init, "LAUNCH_DAEMONS", tmp_path)
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["sudo", "install"]:
            Path(args[-1]).write_bytes(Path(args[-2]).read_bytes())
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(app_init.subprocess, "run", run)
    label = "com.appinit.test.db"
    path = tmp_path / (label + ".plist")
    def install():
        app_init.install_job(tmp_path, label, ["postgres"], keepalive=True)
    install()
    payload = plistlib.loads(path.read_bytes())
    assert payload["EnvironmentVariables"].pop("LC_ALL") == "C"
    path.write_bytes(plistlib.dumps(payload))
    calls.clear()
    install()
    assert [args[2] for args in calls if args[:2] == ["sudo", "launchctl"]] == ["bootout", "bootstrap"]
    calls.clear()
    install()
    assert all(args[0] != "sudo" for args in calls)
    payload["UserName"] = "another-user"
    path.write_bytes(plistlib.dumps(payload))
    with pytest.raises(RuntimeError, match="不同内容"):
        install()
    assert all(args[0] != "sudo" for args in calls)


def test_public_sha_failure_reports_expected_and_observed(tmp_path, monkeypatch):
    expected, observed = "1" * 40, "2" * 40
    monkeypatch.setattr(app_init, "public_request", lambda *args: ({"running_sha": observed}, {}))
    clock = iter([0, 0, 0, 130, 130, 130])
    monkeypatch.setattr(app_init.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(app_init.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match=f"期望 {expected}，公网返回 {observed}"):
        app_init.verify_public(tmp_path, "app.example.com", expected, "user", "secret", "example/app")


def test_public_login_handles_extra_edge_cookie(monkeypatch):
    from email.message import Message

    def request(method, url, **kwargs):
        if method == "POST":
            headers = Message()
            headers.add_header("Set-Cookie", "edge=other; Secure")
            headers.add_header("Set-Cookie", "webapp_session=test; Secure; HttpOnly; Path=/api")
            return {"authenticated": True}, headers
        assert kwargs["headers"]["Cookie"] == "webapp_session=test"
        return {"username": "admin", "csrf_token": "csrf"}, {}

    monkeypatch.setattr(app_init, "public_request", request)
    assert app_init.verify_login("https://app.example.com", "admin", "password")["X-CSRF-Token"] == "csrf"


def test_provenance_real_git_checkout(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    subprocess.run(["git", "init", "-q", root], check=True)
    (root / "app").mkdir()
    (root / "app/release_notes.json").write_text('[{"version":"0.1.6"}]')
    app_init.git(root, "add", ".")
    app_init.git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
    actual = app_init.foundation_provenance(root)
    assert actual == {"schema": 1, "repository": "layho/web-app-init-base-env", "sha": app_init.git(root, "rev-parse", "HEAD"), "version": "0.1.6"}
    (root / "app/release_notes.json").write_text('[{"version":"0.1.7"}]')
    with pytest.raises(ValueError, match="checkout is not clean"):
        app_init.foundation_provenance(root)
