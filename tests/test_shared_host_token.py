import os
from pathlib import Path
import pytest
from deploy.macmini import github_token_migrate as m

def test_shared_migration_is_explicit_private_and_non_overwriting(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(Path,"home",lambda:tmp_path)
    root=tmp_path/"base"
    root.mkdir()
    env=root/".env"
    token="ghp_"+"a"*36
    env.write_text("APP_PUBLIC_ORIGIN=https://base.layho.com\nAPP_GITHUB_TOKEN="+token+"\n")
    env.chmod(0o600)
    home=tmp_path/".local/share"
    home.mkdir(parents=True)
    legacy=home/"macmini-pr-tests"
    legacy.mkdir()
    old=legacy/"github-token"
    old.write_text(token)
    old.chmod(0o600)
    scheduler=home/"macmini-foundation-upgrades"
    scheduler.mkdir()
    (scheduler/"github-token").write_text(token)
    (scheduler/"github-token").chmod(0o600)
    dest=home/"macmini-github/github-token"
    m.shared_migrate(root)
    assert not dest.exists()
    assert token not in capsys.readouterr().out
    m.shared_migrate(root,True)
    assert dest.read_text().strip()==token
    assert dest.stat().st_mode&0o777==0o600
    assert old.read_text()==token
    assert token not in capsys.readouterr().out
    env.write_text(env.read_text().replace(token,"c"*40))
    with pytest.raises(ValueError,match="先核实"):
        m.shared_migrate(root,True)
    assert dest.read_text().strip()==token
