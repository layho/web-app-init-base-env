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
    result = fv.snapshot()
    assert result["current"] == {"version": "0.1.3", "sha": "a" * 40}
    assert result["latest"] == old and result["stale"]
    assert result["status"] == "unknown"
    assert "never expose" not in json.dumps(result)
