import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "upgrade_plan.py" if (HERE / "upgrade_plan.py").exists() else HERE.parent / "deploy/app-init/upgrade_plan.py"
spec = importlib.util.spec_from_file_location("foundation_upgrade_plan", SOURCE)
p = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = p
spec.loader.exec_module(p)


def test_three_way_preserves_business_changes():
    file = "app/main.py"
    old = {file: b"one\ntwo\nthree\nfour\nfive\n", "app/release_notes.json": b"old"}
    new = {file: b"ONE\ntwo\nthree\nfour\nfive\n", "app/release_notes.json": b"upstream"}
    app = {file: b"one\ntwo\nthree\nfour\nFIVE\n", "app/business.py": b"business", "app/release_notes.json": b"local"}
    decisions = p.plan(old, new, app)
    assert len(decisions) == 1
    assert decisions[0].action == "merge"
    assert decisions[0].result == b"ONE\ntwo\nthree\nfour\nFIVE\n"


def test_conflicts_and_manual_actions_fail_closed():
    old = {"app/main.py": b"old\n", "app/remove.py": b"remove", "migrations/004.sql": b"old", "app/local_removed.py": b"old"}
    new = {"app/main.py": b"new\n", "migrations/004.sql": b"new", "app/new.py": b"new", "app/local_removed.py": b"changed"}
    app = {"app/main.py": b"custom\n", "app/remove.py": b"remove", "migrations/004.sql": b"old", "app/new.py": b"custom"}
    actions = {d.path: d.action for d in p.plan(old, new, app)}
    assert actions == {"app/main.py":"conflict", "app/remove.py":"review", "migrations/004.sql":"review", "app/new.py":"conflict", "app/local_removed.py":"conflict"}
    assert all(d.result is None for d in p.plan(old, new, app))


def test_new_files_and_idempotence():
    old = {"app/core.py": b"a"}
    new = {"app/core.py": b"b", "app/new.py": b"c"}
    assert [d.action for d in p.plan(old, new, old)] == ["replace", "add"]
    assert p.plan(old, new, new) == []


def test_protected_and_nonmanaged():
    for path in [".env", ".app-init.json", "README.md", "../app/main.py", "app/project_info.json", "app/release_notes.json", "app/credentials.pem", "app/data/business.db", "frontend/node_modules/a.js", "secrets/token.json"]:
        assert not p.managed(path)
    assert p.managed("app/main.py") and p.managed(".gitignore")


def test_reads_committed_app_not_live_worktree(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    subprocess.run(["git", "init", "-q", str(app)], check=True)
    (app / "app").mkdir()
    core = app / "app/core.py"
    core.write_bytes(b"old")
    p.git(app,"add",".")
    p.git(app,"-c","user.name=Test","-c","user.email=test@example.invalid","commit","-qm","fixture")
    core.write_bytes(b"uncommitted local data")
    result = p.report({"app/core.py":b"old"},{"app/core.py":b"new"},app)
    assert result["changes"][0]["action"] == "replace"
    assert result["read_only"] and not result["blocked"]
    assert core.read_bytes() == b"uncommitted local data"
    assert "uncommitted local data" not in str(result)


def test_generated_symlink_fails_closed(tmp_path):
    (tmp_path / "app").mkdir()
    outside = tmp_path / "secret"
    outside.write_bytes(b"private")
    (tmp_path / "app/core.py").symlink_to(outside)
    with pytest.raises(ValueError, match="Unsafe generated file"):
        p.generated_snapshot(tmp_path)
