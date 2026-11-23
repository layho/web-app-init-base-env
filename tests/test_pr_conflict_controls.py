import pytest
from app import pr_conflict_controls as ctl
from app import pr_tracking

HEAD, BASE = "a" * 40, "b" * 40
REPO = "layho/web-app-init-base-env"

def setup(monkeypatch, *, dirty=True, existing=False):
    monkeypatch.setattr(pr_tracking, "target", lambda: (REPO, "preview"))
    calls = []
    def fake(path, data=None):
        calls.append((path, data))
        if path.endswith("/branches/preview"): return {"commit":{"sha":BASE}}
        if path.endswith("/pulls/71"):
            return {"state":"open","draft":True,"merged_at":None,
                    "head":{"sha":HEAD,"repo":{"full_name":REPO}},
                    "base":{"ref":"preview"},
                    "mergeable":False if dirty else True,
                    "mergeable_state":"dirty" if dirty else "clean"}
        if "/issues?state=all" in path:
            return [{"number":88,"title":"[PR-CONFLICT #71] existing"}] if existing else []
        if "/pulls/71/files" in path: return [{"filename":"app/main.py"}]
        if path.endswith("/issues"):
            assert "[PR-CONFLICT #71]" in data["title"]
            assert "不是已确认的冲突文件" in data["body"]
            assert HEAD in data["body"] and BASE in data["body"]
            return {"number":89}
        raise AssertionError(path)
    monkeypatch.setattr(ctl.release_acceptance, "github", fake)
    return calls

def test_create_conflict_issue_is_explicit_and_bounded(monkeypatch):
    calls = setup(monkeypatch)
    result = ctl.request_repair(71, HEAD, BASE)
    assert result["created"] and result["issue_number"] == 89
    assert sum(path.endswith("/issues") for path, _ in calls) == 1

def test_existing_issue_reused_without_duplicate(monkeypatch):
    calls = setup(monkeypatch, existing=True)
    assert ctl.request_repair(71, HEAD, BASE)["created"] is False
    assert not any(path.endswith("/issues") and data is not None for path, data in calls)

def test_conflict_is_required_and_moving_sha_rejected(monkeypatch):
    calls = setup(monkeypatch, dirty=False)
    with pytest.raises(ctl.ConflictError, match="尚未确认"):
        ctl.request_repair(71, HEAD, BASE)
    assert not any(path.endswith("/issues") and data is not None for path, data in calls)
    with pytest.raises(ctl.ConflictError, match="参数无效"):
        ctl.request_repair(71, "old", BASE)
    with pytest.raises(ctl.ConflictError, match="已变化"):
        ctl.request_repair(71, HEAD, "c"*40)

def test_repository_not_derived_from_browser(monkeypatch):
    monkeypatch.setattr(pr_tracking, "target", lambda: None)
    with pytest.raises(ctl.ConflictError, match="无法验证"):
        ctl.request_repair(71, HEAD, BASE)
