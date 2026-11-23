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
