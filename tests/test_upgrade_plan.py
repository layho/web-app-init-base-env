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


def _host(label="com.appinit.test", port="25936", domain="https://office-panel.layho.com", body="echo old"):
    return (f"#!/bin/bash\nLABEL_PREFIX={label}\nPORT={port}\n"
            f"TRACK_BRANCH=main\nEXPECTED_ORIGIN=\"{domain}\"\n{body}\n").encode()


def test_host_manage_script_safe_upgrade_preserves_instance_identity():
    path = "deploy/macmini/manage.sh"
    before = _host(body="echo old")
    after = _host(body="echo new")
    d = p.plan({path: before}, {path: after}, {path: before})
    assert len(d) == 1
    assert d[0].action == "replace" and d[0].result == after
    assert d[0].reason.startswith("verified host script")


def test_host_manage_script_merges_nonoverlapping_local_customizations():
    path = "deploy/macmini/manage.sh"
    before = _host(body="echo old\n# extra\n# end")
    after = _host(body="echo new\n# extra\n# end")
    local = _host(body="echo old\n# extra\n# end\n# local configuration")
    result = p.plan({path: before}, {path: after}, {path: local})[0]
    assert result.action == "merge"
    assert b"echo new" in result.result
    assert b"# local configuration" in result.result


def test_host_manage_script_rejects_app_instance_rebinding():
    path = "deploy/macmini/manage.sh"
    before = _host()
    after = _host(port="38080")
    d = p.plan({path: before}, {path: after}, {path: before})[0]
    assert d.action == "review" and d.result is None


def test_host_manage_script_rejects_bash_syntax_and_conflicting_edit():
    path = "deploy/macmini/manage.sh"
    before = _host(body="echo hello")
    invalid = _host(body="if true; then")
    d = p.plan({path: before}, {path: invalid}, {path: before})[0]
    assert d.action == "review" and d.result is None
    conflict = p.plan({path: before}, {path: _host(body="echo upstream")},
                      {path: _host(body="echo local")})[0]
    assert conflict.action == "review" and conflict.result is None


def test_other_host_migrations_and_deletions_still_require_review():
    files = ["deploy/macmini/release.py", "deploy/macmini/acceptance.sh",
             "migrations/002_admin_sessions.sql"]
    for path in files:
        d = p.plan({path: b"old"}, {path: b"new"}, {path: b"old"})[0]
        assert d.action == "review" and d.result is None
    gone = p.plan({"deploy/macmini/manage.sh": _host()}, {}, 
                  {"deploy/macmini/manage.sh": _host()})[0]
    assert gone.action == "review" and gone.result is None


def test_manage_script_nested_preview_overrides_are_not_instance_identity():
    path = "deploy/macmini/manage.sh"
    old = (_host(body="echo previous\n") +
           b'if test -n "$BASE_PREVIEW_PORT"; then\n'
           b'  LABEL_PREFIX="com.layho.basepreview"\n'
           b'  PORT="$BASE_PREVIEW_PORT"\n'
           b'  TRACK_BRANCH=preview\n'
           b'  EXPECTED_ORIGIN="https://base.layho.com"\nfi\n')
    new = old.replace(b"echo previous", b"echo upstream")
    local = old
    assert p.host_identity(local) == {
        "LABEL_PREFIX": "com.appinit.test",
        "PORT": "25936",
        "TRACK_BRANCH": "main",
        "EXPECTED_ORIGIN": '"https://office-panel.layho.com"',
    }
    result = p.plan({path: old}, {path: new}, {path: local})[0]
    assert result.action == "replace"
    assert b"echo upstream" in result.result
    assert b'  LABEL_PREFIX="com.layho.basepreview"' in result.result
    assert b'  TRACK_BRANCH=preview' in result.result


def test_manage_script_top_level_identity_redefinition_is_not_safe():
    path = "deploy/macmini/manage.sh"
    old = _host()
    changed = old + b'PORT=12345\n'
    assert p.host_identity(changed) is None
    d = p.plan({path: old}, {path: _host(body="echo updated")}, {path: changed})[0]
    assert d.action == "review"


def test_manage_script_app_port_is_unchanged_after_safe_upstream_patch():
    path = "deploy/macmini/manage.sh"
    old = _host(port="25936", body="echo old")
    new = _host(port="25936", body="echo new")
    result = p.plan({path: old}, {path: new}, {path: old})[0]
    assert result.action == "replace"
    assert p.host_identity(result.result) == p.host_identity(old)


def test_application_modules_never_managed():
    for path in ("app_modules/registry.py", "APP_MODULES/notes.py", "frontend/src/app-modules/notes.tsx", "migrations/modules/notes/001.sql"):
        assert p.app_owned(path) and not p.managed(path)
        assert p.plan({path:b"old"}, {path:b"new"}, {path:b"business"}) == []


def test_module_seed_first_upgrade_and_preservation(tmp_path):
    folder=tmp_path/"app_modules"
    folder.mkdir()
    (folder/"registry.py").write_text("MODULES = ()")
    (folder/"__init__.py").write_text("")
    (folder/"business.py").write_text("private business code")
    seeds=p.module_seed_snapshot(tmp_path)
    assert set(seeds)==p.MODULE_SEEDS
    first=p.module_seed_decisions(seeds,{})
    assert len(first)==2 and all(d.action=="add" for d in first)
    assert p.module_seed_decisions(seeds,{"app_modules/registry.py":"100644","app_modules/__init__.py":"100644"})==[]
    assert [d.path for d in p.module_seed_decisions(seeds,{"app_modules/registry.py":"100644"})]==["app_modules/__init__.py"]


@pytest.mark.parametrize("observed",[{"APP_MODULES/REGISTRY.PY":"100644"},{"app_modules":"100644"},{"app_modules/registry.py":"120000"},{"app_modules/registry.py/nested":"100644"}])
def test_module_seed_collision_fails_closed(observed):
    with pytest.raises(ValueError):
        p.module_seed_decisions({"app_modules/registry.py":b"seed"},observed)
