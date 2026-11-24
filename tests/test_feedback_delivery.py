from app import feedback_delivery as delivery

SHA = "a" * 40
BASE = {
    "created_at": "2026-11-23T01:00:00+00:00",
    "synced_at": "2026-11-23T01:05:00+00:00",
    "github_issue_number": 25, "claimed_at": "2026-11-23T01:06:00+00:00",
    "development_started_at": "2026-11-23T01:07:00+00:00",
    "development_submitted_at": "2026-11-23T01:09:00+00:00",
    "developer_pr": 19, "delivered_sha": SHA,
    "overall_status": "submitted", "progress_stale": False,
}


def test_all_stages_are_evidence_only():
    row = delivery.timeline(BASE)
    assert [x["key"] for x in row["steps"]] == list(delivery.STAGES)
    assert [x["state"] for x in row["steps"][:4]] == ["complete"] * 4
    assert row["steps"][4]["state"] == "pending"
    assert row["steps"][5]["state"] == "pending"
    assert row["current_stage"] == "pr"
    assert row["executor"]["running"] is None
    assert row["executor"]["next_run_at"] is None
    assert row["evidence"]["pr"] is None


def test_verified_macci_merge_and_deployment_are_distinct():
    pr = {"number": 19, "head": SHA, "test": {
        "state": "success", "finished_at": "2026-11-23T01:10:00+00:00"},
        "merge_state": "merged", "merged_at": "2026-11-23T01:12:00+00:00",
        "merge_sha": "b" * 40,
        "deployment": {"state": "unverified", "at": None}}
    row = delivery.timeline(BASE, [pr])
    assert [x["state"] for x in row["steps"][:7]] == ["complete"] * 7
    assert row["steps"][7]["state"] == "pending"
    assert row["steps"][8]["state"] == "pending"
    assert row["current_stage"] == "deployed"
    assert row["evidence"]["merge_sha"] == "b" * 40
    pr["deployment"] = {"state": "deployed", "at": "2026-11-23T01:14:00+00:00"}
    deployed = delivery.timeline(BASE, [pr])
    assert deployed["current_stage"] == "accepted"
    assert deployed["steps"][8]["state"] == "pending"
    # Closed GitHub Issue and PR deployment aren't user acceptance.
    row = delivery.timeline({**BASE, "issue_state": "closed"}, [pr])
    assert row["steps"][8]["state"] == "pending"


def test_failing_exact_test_does_not_become_complete():
    pr = {"number": 19, "head": SHA, "test": {
        "state": "failure", "finished_at": "2026-11-23T01:10:00+00:00"},
        "merge_state": "open", "deployment": {"state": "not_merged"}}
    row = delivery.timeline(BASE, [pr])
    assert row["current_stage"] == "failed"
    assert row["steps"][5]["state"] == "failed"
    assert row["steps"][6]["state"] == "pending"


def test_mismatched_pr_sha_cannot_claim_test_or_deploy():
    row = delivery.timeline(BASE, [{
        "number": 19, "head": "f" * 40, "test": {"state": "success"},
        "merge_state": "merged", "deployment": {"state": "deployed", "at": "now"}}])
    assert row["evidence"]["pr"] is None
    assert row["steps"][5]["state"] == "pending"
    assert row["steps"][7]["state"] == "pending"


def test_blocked_is_not_executor_running():
    row = delivery.timeline({**BASE, "overall_status": "blocked", "progress_stale": True})
    assert row["current_stage"] == "blocked"
    assert row["stale"] is True
    assert row["executor"]["mode"] == "unverified"
    assert row["executor"]["last_run_at"] is None


def test_owner_execution_receipt_is_separate_from_development_status():
    from app import feedback_progress
    import json
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    comment = {
        "id": 2, "body": feedback_progress.EXECUTION_MARKER + json.dumps({
            "state": "started", "executor": "Panel Automation", "task_id": "panel-feedback",
            "next_run_at": "2026-11-24T02:00:00+00:00"}),
        "created_at": now, "updated_at": now, "author_association": "OWNER"}
    evidence = feedback_progress.progress_from_comments([comment])
    assert evidence["overall_status"] == "awaiting_claim"
    assert evidence["claimed_at"] is None
    assert evidence["executor_receipt"]["state"] == "started"
    row = delivery.timeline({**BASE, **evidence})
    assert row["executor"]["mode"] == "github_execution_receipt"
    assert row["executor"]["running"] is True
    assert row["executor"]["name"] == "Panel Automation"
    assert row["executor"]["next_run_at"] == "2026-11-24T02:00:00+00:00"


def test_untrusted_execution_receipt_cannot_claim_task_running():
    from app import feedback_progress
    from datetime import datetime, timezone
    import json
    now = datetime.now(timezone.utc).isoformat()
    comment = {
        "id": 3, "body": feedback_progress.EXECUTION_MARKER + json.dumps({
            "state": "started", "executor": "Unknown", "task_id": "claimed-task"}),
        "created_at": now, "updated_at": now, "author_association": "NONE"}
    evidence = feedback_progress.progress_from_comments([comment])
    assert evidence["executor_receipt"] is None
    row = delivery.timeline({**BASE, **evidence})
    assert row["executor"]["running"] is None


def test_stale_start_receipt_does_not_claim_current_execution():
    row = delivery.timeline({**BASE, "executor_receipt": {
        "state": "started", "executor": "Panel Automation", "task_id": "task",
        "last_event_at": "2026-01-01T00:00:00+00:00",
        "next_run_at": None}})
    assert row["executor"]["running"] is None


def test_later_verified_pr_and_macci_do_not_leave_coding_as_current_wait():
    """Panel #1: submitted/ready_for_review exists, no in_progress receipt."""
    row = {**BASE, "development_started_at": None, "overall_status": "ready_for_review"}
    pr = {"number": 19, "head": SHA, "test": {
        "state": "success", "finished_at": "2026-11-23T01:10:00+00:00"},
        "merge_state": "draft", "deployment": {"state": "not_merged"}}
    progress = delivery.timeline(row, [pr])
    steps = {step["key"]: step for step in progress["steps"]}
    assert steps["coding"]["state"] == "unrecorded"
    assert steps["coding"]["at"] is None  # never invent start time
    assert steps["pr"]["state"] == "complete"
    assert steps["tests"]["state"] == "complete"
    assert steps["merged"]["state"] == "pending"
    assert progress["current_stage"] == "merged"


def test_missing_earlier_milestone_without_later_evidence_remains_pending():
    row = {**BASE, "development_started_at": None,
           "development_submitted_at": None}
    progress = delivery.timeline(row)
    steps = {step["key"]: step for step in progress["steps"]}
    assert steps["coding"]["state"] == "pending"
    assert progress["current_stage"] == "coding"


def test_macci_success_not_merge_deployment_or_acceptance():
    row = {**BASE, "development_started_at": None}
    pr = {"number": 19, "head": SHA, "test": {
        "state": "success", "finished_at": "2026-11-23T01:10:00+00:00"},
        "merge_state": "draft", "deployment": {"state": "not_merged"}}
    progress = delivery.timeline(row, [pr])
    for key in ("merged", "deployed", "accepted"):
        assert next(x for x in progress["steps"] if x["key"] == key)["state"] == "pending"


def test_issue_shows_all_explicit_related_prs_but_keeps_primary_sha_gate():
    row = {**BASE, "github_issue_number": 1, "developer_pr": 3}
    prs = [
        {"number": 3, "head": SHA, "linked_issues": [1],
         "test": {"state": "success", "finished_at": "2026-11-23T01:10:00+00:00"},
         "merge_state": "draft"},
        {"number": 4, "head": "b" * 40, "linked_issues": [1, 2],
         "test": {"state": "unknown"}, "merge_state": "draft"},
        {"number": 5, "head": "c" * 40, "linked_issues": [2],
         "test": {"state": "success"}, "merge_state": "merged"},
    ]
    result = delivery.timeline(row, prs)
    assert [p["number"] for p in result["evidence"]["related_prs"]] == [3, 4]
    assert result["evidence"]["pr"] == 3
    assert result["evidence"]["related_prs"][1]["test_state"] == "unknown"
    assert result["steps"][5]["state"] == "complete"
    assert result["steps"][6]["state"] == "pending"


def test_related_pr_never_substitutes_for_primary_sha_test():
    row = {**BASE, "github_issue_number": 1, "developer_pr": 3}
    prs = [{"number": 4, "head": "b" * 40, "linked_issues": [1],
            "test": {"state": "success", "finished_at": "2026-11-23T01:10:00+00:00"},
            "merge_state": "merged"}]
    result = delivery.timeline(row, prs)
    assert result["evidence"]["pr"] is None
    assert [p["number"] for p in result["evidence"]["related_prs"]] == [4]
    assert result["steps"][5]["state"] == "pending"
    assert result["steps"][6]["state"] == "pending"


def test_pr_creation_time_not_later_progress_comment():
    """GitHub PR created_at is the event time, not the delayed progress reply."""
    progress = {**BASE, "development_submitted_at": "2026-11-23T03:00:00+00:00"}
    pr = {"number": 19, "head": SHA, "created_at": "2026-11-23T01:08:00+00:00",
          "test": {"state": "success", "finished_at": "2026-11-23T01:10:00+00:00"},
          "merge_state": "draft"}
    events = {row["key"]: row for row in delivery.timeline(progress, [pr])["steps"]}
    assert events["pr"]["at"] == "2026-11-23T01:08:00+00:00"
    assert events["tests"]["at"] == "2026-11-23T01:10:00+00:00"


def test_pr_created_time_falls_back_to_explicit_progress_for_old_cache():
    pr = {"number": 19, "head": SHA, "test": {"state": "pending"}, "merge_state": "draft"}
    events = {row["key"]: row for row in delivery.timeline(BASE, [pr])["steps"]}
    assert events["pr"]["at"] == BASE["development_submitted_at"]


def test_submission_diagnostic_distinguishes_receipt_sync_and_head_change():
    missing = delivery.timeline({**BASE, "developer_pr": None})
    assert missing["submission_diagnostic"] == "awaiting_receipt"
    waiting = delivery.timeline(BASE)
    assert waiting["submission_diagnostic"] == "awaiting_pr_sync"
    assert waiting["reported_pr"] == 19
    assert waiting["reported_head"] == SHA
    changed = delivery.timeline(BASE, [{"number": 19, "head": "f" * 40,
        "test": {"state": "success"}, "merge_state": "merged"}])
    assert changed["submission_diagnostic"] == "head_mismatch"
    assert changed["observed_head"] == "f" * 40
    assert changed["evidence"]["pr"] is None
    assert changed["steps"][5]["state"] == "pending"
    verified = delivery.timeline(BASE, [{"number": 19, "head": SHA}])
    assert verified["submission_diagnostic"] == "verified"
