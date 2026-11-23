import os
from types import SimpleNamespace
import pytest
from app import manual_pr_actions as m

HEAD = "a" * 40
BASE = "b" * 40
MERGE = "c" * 40


def setup(monkeypatch, *, draft=False, base=BASE, mergeable=True, status="success", main=BASE):
    monkeypatch.setattr(m.pr_tracking, "target", lambda: (m.REPO, "preview"))
    monkeypatch.setenv("APP_BASE_PREVIEW", "1")
    monkeypatch.setenv("APP_PUBLIC_ORIGIN", "https://base.layho.com")
    paths = []
    def github(path):
        paths.append(path)
        if path == "/branches/preview":
            return {"commit": {"sha": base}}
        if path == "/branches/main":
            return {"commit": {"sha": main}}
        if path == "/pulls/71":
            return {"state": "open", "draft": draft, "mergeable": mergeable,
                    "mergeable_state": "clean" if mergeable else "dirty",
                    "head": {"sha": HEAD, "repo": {"full_name": m.REPO}},
                    "base": {"ref": "preview"}, "merge_commit_sha": MERGE,
                    "user": {"login": "layho"}}
        if path == "/git/commits/" + MERGE:
            return {"parents": [{"sha": BASE}, {"sha": HEAD}]}
        if path == "/commits/" + HEAD + "/statuses?per_page=100":
            return [{"context": m.CONTEXT, "state": status,
                     "description": f"base={BASE} merge={MERGE} passed"}]
        raise AssertionError(path)
    monkeypatch.setattr(m, "api", github)
    return paths


def test_manual_merge_kicks_only_trusted_coordinator(monkeypatch):
    setup(monkeypatch)
    commands = []
    def run(cmd, **kwargs):
        commands.append(cmd)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(m.subprocess, "run", run)
    result = m.request_merge(71, HEAD, BASE)
    assert result["accepted"] is True
    assert result["action"] == "merge-check"
    assert commands == [["/bin/launchctl", "kickstart",
                         f"gui/{os.getuid()}/com.layho.macci-preview-merge"]]
    assert "已唤醒" in result["message"]


def test_manual_merge_rejects_failed_test_draft_and_freeze(monkeypatch):
    commands = []
    monkeypatch.setattr(m.subprocess, "run", lambda *args, **kwargs: commands.append(args))
    setup(monkeypatch, status="failure")
    with pytest.raises(m.ManualActionError, match="MacCI"):
        m.request_merge(71, HEAD, BASE)
    setup(monkeypatch, draft=True)
    with pytest.raises(m.ManualActionError, match="草稿"):
        m.request_merge(71, HEAD, BASE)
    setup(monkeypatch, main="f" * 40)
    with pytest.raises(m.ManualActionError, match="冻结"):
        m.request_merge(71, HEAD, BASE)
    assert commands == []


def test_manual_actions_fail_closed_on_stale_refs_and_other_apps(monkeypatch):
    setup(monkeypatch)
    with pytest.raises(m.ManualActionError, match="变化"):
        m.current_pr(71, "f" * 40, BASE)
    with pytest.raises(m.ManualActionError, match="变化"):
        m.current_pr(71, HEAD, "f" * 40)
    with pytest.raises(m.ManualActionError, match="无效"):
        m.current_pr(71, "../", BASE)
    monkeypatch.setattr(m.pr_tracking, "target", lambda: ("layho/panel", "main"))
    with pytest.raises(m.ManualActionError, match="未授权"):
        m.current_pr(71, HEAD, BASE)


def test_manual_test_targets_exact_pr_without_force_merging(monkeypatch, tmp_path):
    setup(monkeypatch)
    monkeypatch.setattr(m, "ROOT", tmp_path)
    monkeypatch.setattr(m, "TEST_RUNNER", tmp_path / "runner.py")
    (tmp_path / "runner.py").write_text("LOCK_HANDOFF_PROTOCOL = 1\n")
    monkeypatch.setattr(m, "trusted_runner", lambda path: None)
    launched = []
    def launch(args, **kwargs):
        launched.append((args, kwargs))
        return SimpleNamespace(pid=313)
    monkeypatch.setattr(m.subprocess, "Popen", launch)
    result = m.request_test(71, HEAD, BASE)
    assert result["accepted"] is True
    assert result["candidate"] == MERGE
    args, options = launched[0]
    assert args[2:7] == ["--repo", m.REPO, "--pr", "71", "--force"]
    assert args[-2] == "--lock-fd"
    assert options["pass_fds"] == (int(args[-1]),)
    assert options["start_new_session"] is True


def test_manual_test_refuses_busy_worker(monkeypatch, tmp_path):
    setup(monkeypatch)
    monkeypatch.setattr(m, "ROOT", tmp_path)
    monkeypatch.setattr(m, "TEST_RUNNER", tmp_path / "runner.py")
    (tmp_path / "runner.py").write_text("x")
    monkeypatch.setattr(m, "trusted_runner", lambda path: None)
    import fcntl
    original = m.fcntl.flock
    def busy(file, flags):
        raise BlockingIOError()
    monkeypatch.setattr(m.fcntl, "flock", busy)
    with pytest.raises(m.ManualActionError, match="已有测试"):
        m.request_test(71, HEAD, BASE)


def test_manual_worker_lock_remains_held_through_spawn(monkeypatch, tmp_path):
    """No second worker may acquire the file while Popen is being called."""
    import fcntl
    setup(monkeypatch)
    monkeypatch.setattr(m, "ROOT", tmp_path)
    monkeypatch.setattr(m, "TEST_RUNNER", tmp_path / "runner.py")
    (tmp_path / "runner.py").write_text("LOCK_HANDOFF_PROTOCOL = 1\n")
    monkeypatch.setattr(m, "trusted_runner", lambda path: None)
    observed = []

    def launch(args, **kwargs):
        fd = kwargs["pass_fds"][0]
        assert int(args[-1]) == fd
        with (tmp_path / "worker.lock").open("a") as second:
            with pytest.raises(BlockingIOError):
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
        observed.append(fd)
        return SimpleNamespace(pid=313)

    monkeypatch.setattr(m.subprocess, "Popen", launch)
    assert m.request_test(71, HEAD, BASE)["accepted"]
    assert observed


def test_failed_spawn_releases_worker_lock(monkeypatch, tmp_path):
    import fcntl
    setup(monkeypatch)
    monkeypatch.setattr(m, "ROOT", tmp_path)
    monkeypatch.setattr(m, "TEST_RUNNER", tmp_path / "runner.py")
    (tmp_path / "runner.py").write_text("# trusted fixture")
    monkeypatch.setattr(m, "trusted_runner", lambda path: None)

    def fail_launch(args, **kwargs):
        raise OSError("failed to spawn")

    monkeypatch.setattr(m.subprocess, "Popen", fail_launch)
    with pytest.raises(m.ManualActionError, match="启动失败"):
        m.request_test(71, HEAD, BASE)
    with (tmp_path / "worker.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_old_worker_still_receives_compatible_arguments(monkeypatch, tmp_path):
    setup(monkeypatch)
    monkeypatch.setattr(m, "ROOT", tmp_path)
    monkeypatch.setattr(m, "TEST_RUNNER", tmp_path / "runner.py")
    (tmp_path / "runner.py").write_text("# legacy runner")
    monkeypatch.setattr(m, "trusted_runner", lambda path: None)
    launched = []
    monkeypatch.setattr(m.subprocess, "Popen",
                        lambda args, **kwargs: launched.append((args, kwargs)) or SimpleNamespace(pid=314))
    result = m.request_test(71, HEAD, BASE)
    assert result["accepted"] is True
    assert result["lock_handoff"] is False
    assert "兼容模式" in result["message"]
    assert launched[0][0][-5:] == ["--repo", m.REPO, "--pr", "71", "--force"]
    assert "pass_fds" not in launched[0][1]
