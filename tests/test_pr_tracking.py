from datetime import datetime, timezone
import os
from app import pr_tracking as p

H, B, M = "a"*40, "b"*40, "c"*40

def status(state, at, base=B, phase="passed", merge=M):
    return {"context": p.CONTEXT, "state": state, "created_at": at, "description": f"base={base} merge={merge} {phase}"}

def test_exact_base_and_attempt():
    statuses = [status("success", "2026-11-22T03:15:20Z"),
                status("pending", "2026-11-22T03:14:50Z", phase="pending", merge="unknown"),
                status("failure", "2026-11-22T03:10:00Z", phase="pytest-failed")]
    result = p.evidence(statuses, B)
    assert result["duration_seconds"] == 30 and result["state"] == "success"
    assert p.evidence(statuses, H)["state"] == "unknown"
    assert p.evidence(statuses[:1], B)["duration_seconds"] is None

def test_log_only_safe_exact_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "LOG_ROOT", tmp_path)
    path = tmp_path / f"layho--web-app-init-base-env-19-{H[:12]}-{B[:12]}.log"
    path.write_text("+ git init --bare repo\nsecret-example-never-return\n1 failed in 1.0s\n+ git init --bare repo\n45 passed, 2 skipped in 4.20s\nsecret-example-never-return")
    ts = datetime(2026,11,22,3,15,19,tzinfo=timezone.utc).timestamp()
    os.utime(path,(ts,ts))
    test = p.evidence([status("success","2026-11-22T03:15:20Z"), status("pending","2026-11-22T03:14:50Z",phase="pending",merge="unknown")],B)
    result = p.log_summary(19,H,B,test)
    assert result["pytest"] == {"passed":45,"skipped":2}
    assert "secret-example" not in str(result)
    assert p.log_summary(20,H,B,test) is None
    os.utime(path,(ts-1000,ts-1000))
    assert p.log_summary(19,H,B,test) is None
    path.unlink()
    other=tmp_path/"different"
    other.write_text("secret")
    path.symlink_to(other)
    assert p.log_summary(19,H,B,test) is None


def test_app_init_metadata_enables_generic_tracking(tmp_path, monkeypatch):
    import json
    repo = "layho/macmini-office-app-panel"
    path = tmp_path / ".app-init.json"
    path.write_text(json.dumps({"repository": repo, "domain": "panel.layho.com",
                                "foundation": {"version": "0.1.23"}}))
    monkeypatch.setattr(p, "METADATA", path)
    monkeypatch.delenv("APP_BASE_PREVIEW", raising=False)
    monkeypatch.setenv("APP_PUBLIC_ORIGIN", "https://panel.layho.com")
    assert p.enabled()
    assert p.target() == (repo, "main")
    assert p.log_summary(5, H, B, {"state": "unknown"}, repo) is None
    monkeypatch.setenv("APP_PUBLIC_ORIGIN", "https://other.layho.com")
    assert not p.enabled()


def test_base_preview_uses_preview_and_invalid_metadata_is_rejected(tmp_path, monkeypatch):
    import json
    path = tmp_path / ".app-init.json"
    monkeypatch.setattr(p, "METADATA", path)
    monkeypatch.setenv("APP_BASE_PREVIEW", "1")
    monkeypatch.setenv("APP_PUBLIC_ORIGIN", "https://base.layho.com")
    assert p.target() == (p.REPO, "preview")
    monkeypatch.delenv("APP_BASE_PREVIEW")
    path.write_text(json.dumps({"repository": "../other", "domain": "base.layho.com"}))
    assert p.target() is None


def test_collect_normal_app_prs_exact_main_without_cross_repo():
    repo = "layho/macmini-office-app-panel"
    calls = []
    def github(method, path, data=None):
        calls.append(path)
        if path.endswith("/branches/main"):
            return {"commit": {"sha": B}}
        if "/pulls?" in path:
            return [
                {"number": 9, "title": "Panel fix", "state": "open",
                 "base": {"ref": "main", "sha": B},
                 "head": {"sha": H, "repo": {"full_name": repo}},
                 "draft": False, "merged_at": None, "merge_commit_sha": None},
                {"number": 10, "title": "Other branch", "state": "open",
                 "base": {"ref": "preview", "sha": B},
                 "head": {"sha": H, "repo": {"full_name": repo}},
                 "draft": False, "merged_at": None, "merge_commit_sha": None},
            ]
        if path.endswith("/pulls/9"):
            return {"number": 9, "title": "Panel fix", "state": "open",
                    "base": {"ref": "main", "sha": B},
                    "head": {"sha": H, "repo": {"full_name": repo}},
                    "draft": False, "merged_at": None, "merge_commit_sha": None,
                    "mergeable": True, "mergeable_state": "clean"}
        if path.endswith("/pulls/10"):
            return {"number": 10, "title": "Other branch", "state": "open",
                    "base": {"ref": "preview", "sha": B},
                    "head": {"sha": H, "repo": {"full_name": repo}},
                    "draft": False, "merged_at": None, "merge_commit_sha": None}
        if path.endswith("/statuses?per_page=100"):
            return [status("success", "2026-11-22T03:15:20Z")]
        raise AssertionError(path)
    rows = p.collect(github, repo, "main")
    assert len(rows) == 1
    assert rows[0]["url"] == "https://github.com/" + repo + "/pull/9"
    assert rows[0]["test"]["state"] == "success"
    assert all("/repos/" + repo + "/" in u for u in calls)


def test_tracking_cache_never_leaks_another_application(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(p, "CACHE", tmp_path / "tracking.json")
    monkeypatch.setattr(p, "METADATA", tmp_path / ".app-init.json")
    monkeypatch.delenv("APP_BASE_PREVIEW", raising=False)
    monkeypatch.setenv("APP_PUBLIC_ORIGIN", "https://panel.layho.com")
    p.METADATA.write_text(json.dumps({"repository": "layho/macmini-office-app-panel",
                                      "domain": "panel.layho.com"}))
    p.CACHE.write_text(json.dumps({"synced_at": "2026-11-23T01:00:00+00:00",
                                   "repository": "layho/another-app", "branch": "main",
                                   "prs": [{"number": 1}]}))
    assert p.snapshot()["prs"] == []


def test_repository_validation_blocks_relative_segments():
    for bad in ("../other", "./other", "owner/..", "owner/.", "../..", "owner//repo", "owner/repo/extra", "owner/repo?x=1"):
        assert p.valid_repository(bad) is False, bad
    for good in ("layho/web-app-init-base-env", "layho/macmini-office-app-panel", "owner/.github"):
        assert p.valid_repository(good) is True, good


def test_dirty_draft_is_blocked_even_when_old_macci_was_successful():
    repo = "layho/web-app-init-base-env"
    new_base = "d" * 40
    calls = []
    def github(method, path, data=None):
        calls.append(path)
        if path.endswith("/branches/preview"):
            return {"commit": {"sha": new_base}}
        if "/pulls?state=all" in path:
            return [{"number": 71, "state": "open"}]
        if path.endswith("/pulls/71"):
            return {"number":71, "title":"PR conflict", "state":"open", "draft":True,
                    "mergeable":False, "mergeable_state":"dirty", "changed_files":1,
                    "base":{"ref":"preview", "sha": B},
                    "head":{"sha":H,"repo":{"full_name":repo}}}
        if "/pulls/71/files?" in path:
            return [{"filename":"app/main.py"}]
        if "/statuses?" in path:
            return [status("success", "2026-11-23T02:53:43Z", base=B)]
        raise AssertionError(path)
    rows = p.collect(github, repo, "preview")
    assert len(rows) == 1
    row = rows[0]
    assert row["merge_state"] == "draft"
    assert row["merge_blocked"] is True
    assert row["conflict"]["blocked"] is True
    assert row["conflict"]["base_sha"] == new_base
    assert row["conflict"]["candidate_files"] == ["app/main.py"]
    assert row["base"] == new_base
    assert row["test"]["state"] == "unknown"
    assert "/repos/" + repo + "/pulls/71" in calls


def test_explicit_related_issue_refs_from_office_panel_pr():
    assert p.explicit_issue_refs("Refs #2\\nRelated #1") == [1, 2]
    assert p.explicit_issue_refs("related to #3 and closes #8") == [3, 8]
    assert p.explicit_issue_refs("arbitrary #23 is not an explicit link") == []
