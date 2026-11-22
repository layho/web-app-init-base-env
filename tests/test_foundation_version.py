import base64
import json

from app import foundation_version as fv


def test_legacy_is_unregistered(tmp_path, monkeypatch):
    monkeypatch.setattr(fv, "ROOT", tmp_path)
    monkeypatch.delenv("APP_BASE_PREVIEW", raising=False)
    (tmp_path / ".app-init.json").write_text('{"repository":"example/app"}')
    result = fv.snapshot()
    assert result["status"] == "unregistered"
    assert result["current"] is None
    assert not result["auto_upgrade_enabled"]


def test_pinned_latest(monkeypatch):
    sha = "b" * 40
    calls = []
    def fake(url):
        calls.append(url)
        if url.endswith("/branches/main"):
            return {"commit": {"sha": sha}}
        return {"content": base64.b64encode(b'[{"version":"0.1.5"}]').decode()}
    monkeypatch.setattr(fv, "get_json", fake)
    assert fv.fetch_latest() == {"version": "0.1.5", "sha": sha}
    assert calls[-1].endswith("?ref=" + sha)


def test_registered_version_and_stale_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(fv, "ROOT", tmp_path)
    monkeypatch.delenv("APP_BASE_PREVIEW", raising=False)
    metadata = {"foundation": {"schema": 1, "repository": fv.REPOSITORY, "version": "0.1.3", "sha": "a" * 40}, "secret": "never expose"}
    (tmp_path / ".app-init.json").write_text(json.dumps(metadata))
    old = {"version": "0.1.5", "sha": "b" * 40}
    monkeypatch.setattr(fv, "_cache", {"latest": old, "checked_at": "prior", "stale": False, "expires": 0})
    def offline():
        raise OSError("offline")
    monkeypatch.setattr(fv, "fetch_latest", offline)
    monkeypatch.setattr(fv, "fetch_latest_via_git", offline)
    result = fv.snapshot()
    assert result["current"] == {"version": "0.1.3", "sha": "a" * 40}
    assert result["latest"] == old and result["stale"]
    assert result["status"] == "unknown"
    assert "never expose" not in json.dumps(result)


def test_api_failure_uses_pinned_git_fallback(monkeypatch):
    from urllib.error import HTTPError
    from types import SimpleNamespace
    sha = "c" * 40
    monkeypatch.setattr(fv, "_cache", {"latest":None, "checked_at":None, "stale":True, "expires":0})
    def limited():
        raise HTTPError("https://api.github.com", 403, "limited", None, None)
    monkeypatch.setattr(fv, "fetch_latest", limited)
    def git(args, **kwargs):
        assert args[-2:] == [fv.GIT_URL, "refs/heads/main"]
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        return SimpleNamespace(stdout=sha + "\trefs/heads/main\n")
    monkeypatch.setattr(fv.subprocess, "run", git)
    def raw(url):
        assert url == f"{fv.RAW}/{sha}/app/release_notes.json"
        return [{"version": "0.1.12"}]
    monkeypatch.setattr(fv, "get_json", raw)
    result = fv.latest_status()
    assert result["latest"] == {"version":"0.1.12","sha":sha}
    assert result["source"] == "git_raw" and not result["stale"]
    assert result["error"] is None


def test_manual_refresh_bypasses_cache_but_throttles(monkeypatch):
    monkeypatch.setattr(fv, "_cache", {"latest":None, "checked_at":None, "stale":True, "expires":0})
    clock = [100]
    monkeypatch.setattr(fv.time, "monotonic", lambda: clock[0])
    calls = []
    def fetch():
        calls.append(1)
        return {"version":"0.1.12","sha":"d"*40}
    monkeypatch.setattr(fv, "fetch_latest", fetch)
    fv.latest_status()
    clock[0] += 11
    fv.latest_status()
    assert len(calls) == 1
    fv.latest_status(force=True)
    fv.latest_status(force=True)
    assert len(calls) == 2


def test_git_fallback_rejects_preview(monkeypatch):
    from types import SimpleNamespace
    import pytest
    monkeypatch.setattr(fv.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="a"*40 + "\trefs/heads/preview\n"))
    with pytest.raises(ValueError):
        fv.fetch_latest_via_git()
