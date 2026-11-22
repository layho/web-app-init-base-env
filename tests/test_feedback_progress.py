"""Regression tests for bidirectional feedback milestones."""
from datetime import datetime, timedelta, timezone
import json

from app import feedback_progress as progress

SHA = "a" * 40


def event(id, status, at=None, association="OWNER", **extra):
    at = at or f"2026-11-22T02:{id:02d}:00Z"
    return {
        "id": id, "author_association": association,
        "created_at": at, "updated_at": at,
        "body": progress.MARKER + json.dumps({"status": status, **extra}),
    }


def test_progress_tracks_only_explicit_milestones():
    rows = [
        event(4, "submitted", sha=SHA, pr=14, reply="已提交测试候选"),
        event(2, "in_progress"),
        event(1, "claimed"),
        event(3, "development_finished"),
    ]
    result = progress.progress_from_comments(rows)
    assert result["overall_status"] == "submitted"
    assert result["claimed_at"] == "2026-11-22T02:01:00+00:00"
    assert result["development_started_at"] == "2026-11-22T02:02:00+00:00"
    assert result["development_finished_at"] == "2026-11-22T02:03:00+00:00"
    assert result["development_submitted_at"] == "2026-11-22T02:04:00+00:00"
    assert result["delivered_sha"] == SHA
    assert result["developer_pr"] == 14
    assert result["developer_reply"] == "已提交测试候选"


def test_untrusted_and_invalid_release_comments_do_not_overwrite_status():
    rows = [
        event(1, "claimed"),
        event(2, "accepted", association="NONE", sha=SHA, pr=14),
        event(3, "submitted", sha="bad", pr=14),
        event(4, "accepted", sha=SHA, pr=True),
        event(5, "not_a_status"),
        {**event(6, "in_progress"), "updated_at": "2026-11-22T02:08:00Z"},
        {**event(7, "in_progress"), "body": "[APP_PROGRESS] broken"},
    ]
    result = progress.progress_from_comments(rows)
    assert result["overall_status"] == "claimed"
    assert result["development_started_at"] is None
    assert result["delivered_sha"] is None


def test_existing_claim_is_not_backdated_or_fabricated():
    first = {**event(1, "claimed"), "body": "[APP-CLAIM] 已领取"}
    result = progress.progress_from_comments([first, event(2, "submitted", sha=SHA, pr=14)])
    assert result["claimed_at"] == "2026-11-22T02:01:00+00:00"
    assert result["development_started_at"] is None
    assert result["development_finished_at"] is None
    assert result["overall_status"] == "submitted"


def test_cache_staleness_and_feedback_id_isolation(tmp_path):
    path = tmp_path / "feedback.json"
    now = datetime(2026, 11, 22, 3, tzinfo=timezone.utc)
    rows = progress.progress_from_comments([event(1, "claimed")])
    cache = {"13": {"feedback_id": 1, "checked_at": now.isoformat(), **rows}}
    progress.save_cache(cache, path)
    assert progress.load_cache(path) == cache
    assert (path.stat().st_mode & 0o777) == 0o600
    assert progress.feedback_details(cache, 1, 13, now)["overall_status"] == "claimed"
    assert not progress.feedback_details(cache, 1, 13, now)["progress_stale"]
    assert progress.feedback_details(cache, 1, 13, now + timedelta(minutes=11))["progress_stale"]
    assert progress.feedback_details(cache, 2, 13, now)["overall_status"] == "awaiting_claim"
    assert progress.feedback_details(cache, 1, None, now)["overall_status"] == "pending_sync"
    path.write_text("broken")
    assert progress.load_cache(path) == {}
