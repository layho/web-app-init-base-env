"""Offline single-token migration: no network, no GitHub side effects."""
import os
from pathlib import Path
import stat
import pytest
from deploy.macmini import github_token_migrate as m


def make_install(tmp_path):
    root = tmp_path / "base"
    root.mkdir()
    (root / "app").mkdir()
    (root / "deploy" / "macmini").mkdir(parents=True)
    (root / "app" / "release_acceptance.py").write_text("def github_token():\n    pass\n")
    (root / "deploy" / "macmini" / "manage.sh").write_text("APP_GITHUB_TOKEN\n")
    env = root / ".env"
    env.write_text(
        "APP_PUBLIC_ORIGIN=https://base.layho.com\n"
        "APP_GITHUB_PR_TOKEN=ghp_01234567890123456789012345\n"
        "APP_GITHUB_FEEDBACK_TOKEN=ghp_abcdefghijabcdefghijabcd\n"
        "APP_ADMIN_USERNAME=admin\n")
    env.chmod(0o600)
    return root, env


def test_safe_migration_dry_run_and_apply(tmp_path):
    root, env = make_install(tmp_path)
    old = env.read_bytes()
    assert m.migrate(root) is None
    assert env.read_bytes() == old
    backup = m.migrate(root, apply=True)
    assert backup.is_file()
    assert backup.read_bytes() == old
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    new = env.read_text()
    assert new.count("APP_GITHUB_TOKEN=") == 1
    assert "APP_GITHUB_PR_TOKEN=" not in new
    assert "APP_GITHUB_FEEDBACK_TOKEN=" not in new
    assert "APP_ADMIN_USERNAME=admin" in new
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_rejects_wrong_origin_duplicate_or_insecure_file(tmp_path):
    root, env = make_install(tmp_path)
    text = env.read_text()
    env.write_text(text.replace("https://base.layho.com", "https://wrong.example"))
    with pytest.raises(ValueError, match="不是基座 Preview"):
        m.migrate(root, apply=True)
    env.write_text(text + "APP_GITHUB_PR_TOKEN=anotherlongtokentokentoken\n")
    with pytest.raises(ValueError, match="重复"):
        m.migrate(root, apply=True)
    env.write_text(text)
    env.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        m.migrate(root, apply=True)


def test_cannot_apply_before_single_token_code_is_present(tmp_path):
    root, env = make_install(tmp_path)
    (root / "app" / "release_acceptance.py").write_text("# old version\n")
    with pytest.raises(ValueError, match="先完成正式发布"):
        m.migrate(root, apply=True)
    assert not list(root.glob(".env.backup.*"))
