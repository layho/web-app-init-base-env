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
