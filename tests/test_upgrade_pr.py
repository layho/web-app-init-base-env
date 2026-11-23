import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1] / "deploy/app-init"
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("upgrade_pr", ROOT / "upgrade_pr.py")
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)
sys.path.remove(str(ROOT))

A, B, C = "a"*40, "b"*40, "c"*40


def test_official_first_parent_only():
    with patch.object(u, "line", side_effect=[B, B+"\n"+A]), patch.object(u, "release", side_effect=[{"sha": A},{"sha": B}]):
        assert u.verify_source(".", A, B) == ({"sha": A},{"sha": B})
    with patch.object(u, "line", side_effect=[B,B+"\n"+A]):
        with pytest.raises(ValueError, match="official main history"):
            u.verify_source(".", C, B)
    with patch.object(u, "line", return_value=B):
        with pytest.raises(ValueError, match="official main"):
            u.verify_source(".", A, C)


def test_mismatched_metadata():
    values = dict(repository="example/app",domain="app.example.com",label="com.test.app",port=18123,title="Example")
    assert u.validate_params(values,"example/app") == values
    with pytest.raises(ValueError):
        u.validate_params(values,"example/other")
    with pytest.raises(ValueError):
        u.validate_params({**values,"port":True},"example/app")
    with pytest.raises(ValueError):
        u.validate_params({**values,"title":"bad\nname"},"example/app")


def setup_repo(root):
    root.mkdir()
    subprocess.run(["git","init","-q",str(root)],check=True)
    (root/"app").mkdir()
    meta=dict(repository="example/app",domain="app.example.com",label="com.test.app",port=18123,title="Example")
    (root/".app-init.json").write_text(json.dumps(meta))
    (root/"app/project_info.json").write_text('{"name":"Example"}')
    (root/"app/main.py").write_text("old\n")
    (root/"app/business.py").write_text("business\n")
    u.git(root,"add",".")
    u.git(root,"-c","user.name=Test","-c","user.email=test@example.invalid","commit","-qm","fixture")
    return meta,u.line(root,"rev-parse","HEAD")


def test_exact_legacy_reproduction(tmp_path):
    app=tmp_path/"apprepo"
    meta,sha=setup_repo(app)
    generated=tmp_path/"generated"
    generated.mkdir()
    for path in (".app-init.json","app/project_info.json","app/main.py","app/business.py"):
        p=generated/path
        p.parent.mkdir(exist_ok=True)
        p.write_bytes((app/path).read_bytes())
    assert u.legacy_check(app,sha,generated,"example/app",meta)==sha
    (generated/"app/main.py").write_text("wrong\n")
    with pytest.raises(ValueError,match="does not exactly reproduce"):
        u.legacy_check(app,sha,generated,"example/app",meta)


def test_stage_only_owned_files(tmp_path):
    app=tmp_path/"apprepo"
    meta,sha=setup_repo(app)
    (app/"app/business.py").write_text("uncommitted local changes\n")
    decisions=u.planner.plan({"app/main.py":b"old\n"},{"app/main.py":b"new\n"},{"app/main.py":b"old\n"})
    target={"schema":1,"repository":u.FOUNDATION,"sha":B,"version":"0.1.8"}
    commit=u.create_commit(app,sha,decisions,meta,target)
    assert u.line(app,"rev-parse","HEAD")==sha
    assert u.blob(app,commit,"app/business.py")==b"business\n"
    assert u.blob(app,commit,"app/main.py")==b"new\n"
    assert json.loads(u.blob(app,commit,".app-init.json"))["foundation"]==target
    assert (app/"app/main.py").read_bytes()==b"old\n"
    assert (app/"app/business.py").read_bytes()==b"uncommitted local changes\n"
    blocked=u.planner.plan({"app/main.py":b"old\n"},{"app/main.py":b"new\n"},{"app/main.py":b"custom\n"})
    with pytest.raises(ValueError,match="Unresolved"):
        u.create_commit(app,sha,blocked,meta,target)


def test_existing_pr_is_reused_without_push():
    existing={"state":"OPEN","url":"https://github.com/example/app/pull/1"}
    with patch.object(u,"prs",return_value=[existing]),patch.object(u,"git") as git,patch.object(u,"remote_ref") as ref:
        result=u.reuse_or_publish(".","example/app","git@github.com:example/app.git",A,{"sha":B,"version":"0.1.8"},C,{})
        assert result["state"]=="existing_pr"
        git.assert_not_called()
        ref.assert_not_called()


def test_changed_main_stops_push():
    with patch.object(u,"prs",return_value=[]),patch.object(u,"remote_ref",side_effect=[B,C]),patch.object(u,"git") as git:
        with pytest.raises(ValueError,match="Application main advanced"):
            u.reuse_or_publish(".","example/app","git@github.com:example/app.git",A,{"sha":B,"version":"0.1.8"},C,{})
        git.assert_not_called()


def test_verified_historical_template_files_can_be_adopted_without_overwriting_app():
    params = {"repository": "layho/macmini-office-app-panel",
              "domain": "office-panel.layho.com", "port": 25936,
              "label": "com.appinit.dfb8175428c0", "title": "macmini-office-app-panel"}
    path = "app/manual_pr_actions.py"
    historical = b'REPO = "example/web-app"\nORIGIN = "app.example.com"\n'
    rendered = u.render_proven_old_source(historical, params)
    latest = b'REPO = "layho/macmini-office-app-panel"\nORIGIN = "office-panel.layho.com"\n# fixed\n'
    with patch.object(u, "blob", return_value=historical) as read:
        before = u.adopt_verified_template_baselines("source", A, params, {},
                                                      {path: latest}, {path: rendered})
    assert before == {path: rendered}
    read.assert_called_once_with("source", A, path)
    decisions = u.planner.plan(before, {path: latest}, {path: rendered})
    assert len(decisions) == 1
    assert decisions[0].action == "replace"
    assert decisions[0].result == latest


def test_modified_or_untrusted_historical_app_file_stays_blocked():
    params = {"repository": "layho/macmini-office-app-panel",
              "domain": "office-panel.layho.com", "port": 25936,
              "label": "com.appinit.dfb8175428c0", "title": "macmini-office-app-panel"}
    path = "app/manual_pr_actions.py"
    original = b"print('foundation')\n"
    latest = b"print('new')\n"
    local = b"print('app-owned customization')\n"
    with patch.object(u, "blob", return_value=original):
        before = u.adopt_verified_template_baselines("source", A, params, {},
                                                      {path: latest}, {path: local})
    assert before == {}
    decision = u.planner.plan(before, {path: latest}, {path: local})[0]
    assert decision.action == "conflict" and decision.result is None
    # Unknown arbitrary paths must never be adopted, even if content matches.
    with patch.object(u, "blob") as reader:
        before = u.adopt_verified_template_baselines("source", A, params, {},
             {"app/business.py": b"old"}, {"app/business.py": b"new"},
             {"app/business.py": b"old"})
    assert before == {}  # A non-allowlisted path is not adopted or invented.
    reader.assert_not_called()


def test_adoption_handles_all_five_known_panel_legacy_init_gaps():
    params = {"repository": "layho/macmini-office-app-panel",
              "domain": "office-panel.layho.com", "port": 25936,
              "label": "com.appinit.dfb8175428c0", "title": "macmini-office-app-panel"}
    names = u.PROVEN_TEMPLATE_ADOPTIONS
    assert names == {"app/admin_settings.py", "app/feedback_delivery.py",
                     "app/manual_pr_actions.py", "app/pr_conflict_controls.py",
                     "app/pr_draft_controls.py"}
    old = {name: b"foundation-old\n" for name in names}
    new = {name: b"foundation-new\n" for name in names}
    with patch.object(u, "blob", return_value=b"foundation-old\n"):
        before = u.adopt_verified_template_baselines("source", A, params, {}, new, old)
    assert before == old
    decisions = u.planner.plan(before, new, old)
    assert len(decisions) == 5
    assert all(d.action == "replace" for d in decisions)


def test_adoption_does_not_swallow_missing_history_into_false_provenance():
    params = {"repository": "example/app", "domain": "app.example.com",
              "port": 25936, "label": "com.example.webapp", "title": "App"}
    path = "app/admin_settings.py"
    with patch.object(u, "blob", side_effect=ValueError("missing old source")):
        before = u.adopt_verified_template_baselines("source", A, params, {},
                                                      {path: b"new"}, {path: b"old"})
    assert before == {}
