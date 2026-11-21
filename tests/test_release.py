import subprocess

import pytest

from deploy.macmini import acceptance


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(acceptance.SOURCE / "deploy" / "macmini"))
    import release

    head, base, preview, merged = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
    source, production = tmp_path / "source", tmp_path / "production"
    calls = []
    pr = {"headRefOid": head, "baseRefOid": base, "baseRefName": "main", "state": "OPEN", "isDraft": True}
    fetches = []

    def fake_git(repo, *args):
        if args[:2] == ("remote", "get-url"):
            return f"git@github.com:{release.REPO}.git"
        if args[0] == "fetch":
            fetches.append(args[-1])
        if args == ("rev-parse", "FETCH_HEAD"):
            return preview if fetches[-1].endswith("/merge") else merged
        if args[0] == "show":
            return f"{base} {head}"
        if args[0] == "rev-parse":
            return "same-tree"
        return ""

    def fake_command(args, **kwargs):
        if args[0] == "/bin/launchctl":
            calls.append("deploy")
        elif args[0] == "gh":
            calls.append("ready")
        elif "--require-feedback" in args:
            calls.append("test")

    def merge(*args):
        assert f"sha={head}" in args
        calls.append("merge")
        return {"merged": True, "sha": merged}

    monkeypatch.setattr(release, "git", fake_git)
    monkeypatch.setattr(release, "require_production_checkout", lambda path: "gui/501/test.updater")
    monkeypatch.setattr(release, "pull_request", lambda number: dict(pr))
    monkeypatch.setattr(release, "command", fake_command)
    monkeypatch.setattr(release, "github_json", merge)
    monkeypatch.setattr(release, "verify_deployment", lambda sha: calls.append(sha))
    return release, calls, pr, (8, head, source, production)


def test_release_validates_before_merge_and_deploy(pipeline):
    release, calls, _, args = pipeline
    release.release(*args)
    assert calls == ["test", "ready", "merge", "deploy", "d" * 40]


def test_release_stops_if_acceptance_fails(pipeline, monkeypatch):
    release, calls, _, args = pipeline

    def command(argv, **kwargs):
        if "--require-feedback" in argv:
            raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(release, "command", command)
    with pytest.raises(subprocess.CalledProcessError):
        release.release(*args)
    assert not calls


def test_release_stops_when_base_changes_during_acceptance(pipeline, monkeypatch):
    release, calls, pr, args = pipeline
    states = iter([pr, {**pr, "baseRefOid": "e" * 40}])
    monkeypatch.setattr(release, "pull_request", lambda number: next(states))
    with pytest.raises(RuntimeError, match="验收期间"):
        release.release(*args)
    assert calls == ["test"]
