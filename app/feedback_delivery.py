"""Evidence-based feedback delivery timeline.

Do not confuse background polling with an AI task actually running.  Events
shown here are derived only from persisted feedback, trusted issue milestones,
exact PR/MacCI records and locally verified deployment events.
"""
from datetime import datetime, timezone
import re

STAGES = ("submitted", "synced", "claimed", "coding", "pr", "tests", "merged", "deployed", "accepted")
LABELS = {
    "submitted": "反馈提交", "synced": "同步 GitHub", "claimed": "领取任务",
    "coding": "开始开发", "pr": "提交修复 PR", "tests": "MacCI 测试",
    "merged": "合并代码", "deployed": "部署验证", "accepted": "用户验收",
}
SHA = re.compile(r"[0-9a-f]{40}\Z")


def valid_sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def timeline(feedback, prs=None):
    """Return stages with grounded event times and conservative statuses."""
    prs = prs or []
    # Keep exact submitted SHA as the primary milestone. Other PRs are shown
    # only when they explicitly reference this Issue; no title-based guessing.
    issue = feedback.get("github_issue_number")
    primary_number = feedback.get("developer_pr")
    related = [row for row in prs if isinstance(row, dict) and (
        (type(row.get("number")) is int and row["number"] == primary_number)
        or (type(issue) is int and issue in (row.get("linked_issues") or [])))]
    related = sorted({row["number"]: row for row in related
                      if type(row.get("number")) is int and row["number"] > 0}.values(),
                     key=lambda row: row["number"])
    linked = next((row for row in related if primary_number == row.get("number")
                   and valid_sha(feedback.get("delivered_sha"))
                   and row.get("head") == feedback["delivered_sha"]), None)
    primary = next((row for row in related if row.get("number") == primary_number), None)
    has_receipt = type(primary_number) is int and primary_number > 0 and valid_sha(feedback.get("delivered_sha"))
    if linked:
        diagnostic = "verified"
    elif not has_receipt:
        diagnostic = "awaiting_receipt"
    elif primary is None:
        diagnostic = "awaiting_pr_sync"
    else:
        diagnostic = "head_mismatch"
    tested = linked.get("test", {}) if linked else {}
    # Current PR failure is independent diagnostic evidence. Never use a
    # mismatched HEAD to complete the submitted candidate's milestones.
    failures = [{"number": row["number"], "head": row.get("head"),
                 "phase": (row.get("test") or {}).get("phase"),
                 "at": (row.get("test") or {}).get("finished_at")}
                for row in related if (row.get("test") or {}).get("state") in {"failure", "error"}]
    deployment = linked.get("deployment", {}) if linked else {}
    events = {
        "submitted": feedback.get("created_at"),
        "synced": feedback.get("synced_at") if feedback.get("github_issue_number") else None,
        "claimed": feedback.get("claimed_at"),
        "coding": feedback.get("development_started_at"),
        "pr": (linked.get("created_at") or feedback.get("development_submitted_at")) if linked else None,
        "tests": tested.get("finished_at") if tested.get("state") == "success" else None,
        "merged": linked.get("merged_at") if linked and linked.get("merge_state") == "merged" else None,
        "deployed": deployment.get("at") if deployment.get("state") in ("running", "deployed") else None,
        # An explicit trusted accepted milestone is necessary; a closed
        # GitHub issue or deployed SHA must never masquerade as user acceptance.
        "accepted": feedback.get("accepted_at") if feedback.get("overall_status") == "accepted" else None,
    }
    steps = []
    for code in STAGES:
        at = events[code]
        state = "complete" if isinstance(at, str) and at else "pending"
        if code == "tests" and linked and tested.get("state") in ("failure", "error"):
            state = "failed"
            at = tested.get("finished_at")
        if code == "deployed" and deployment.get("state") == "failed":
            state = "failed"
            at = deployment.get("at")
        steps.append({"key": code, "label": LABELS[code], "state": state, "at": at})
    # Missing historical milestones must stay explicitly unverified, without
    # hiding a real later PR or MacCI event behind "waiting to start coding".
    # No date is inferred and no milestone is marked completed by implication.
    last_verified = max((i for i, row in enumerate(steps)
                         if row["state"] == "complete"), default=-1)
    for i, row in enumerate(steps):
        if i < last_verified and row["state"] == "pending":
            row["state"] = "unrecorded"
    if feedback.get("overall_status") == "blocked":
        phase = "blocked"
    elif any(x["state"] == "failed" for x in steps):
        phase = "failed"
    else:
        phase = next((x["key"] for x in steps if x["state"] == "pending"), "accepted")
    # An owner-authored GitHub execution receipt is a declared executor
    # status, not an independent platform heartbeat. Expire stale "started"
    # evidence rather than silently claiming ongoing activity.
    receipt = feedback.get("executor_receipt")
    actor = {"name": None, "reported_actor": feedback.get("developer_actor"),
             "mode": "unverified", "running": None, "last_run_at": None,
             "next_run_at": None, "schedule_source": None}
    if isinstance(receipt, dict):
        at = receipt.get("last_event_at")
        try:
            when = datetime.fromisoformat(at) if isinstance(at, str) else None
            age = (datetime.now(timezone.utc) - when).total_seconds() if when and when.tzinfo else None
        except ValueError:
            age = None
        state = receipt.get("state")
        actor.update(name=receipt.get("executor"), mode="github_execution_receipt",
                     running=True if state == "started" and age is not None and 0 <= age <= 900 else
                     False if state in ("finished", "failed", "queued") else None,
                     last_run_at=at if state in ("started", "finished", "failed") else None,
                     next_run_at=receipt.get("next_run_at"),
                     schedule_source="GitHub Issue OWNER execution receipt")
    return {
        "steps": steps, "current_stage": phase,
        "developer_agent": feedback.get("developer_agent"), "developer_actor": feedback.get("developer_actor"),
        "test_failures": failures,
        "submission_diagnostic": diagnostic,
        "reported_pr": primary_number if has_receipt else None,
        "reported_head": feedback.get("delivered_sha") if has_receipt else None,
        "observed_head": primary.get("head") if primary else None,
        "executor": actor,
        "evidence": {"issue": feedback.get("github_issue_number"),
                     "pr": linked.get("number") if linked else None,
                     "related_prs": [{"number": row["number"], "head_sha": row.get("head"),
                                      "test_state": (row.get("test") or {}).get("state"),
                                      "merge_state": row.get("merge_state")}
                                     for row in related],
                     "head_sha": linked.get("head") if linked else None,
                     "merge_sha": linked.get("merge_sha") if linked else None},
        "last_verified_at": feedback.get("progress_synced_at"),
        "stale": bool(feedback.get("progress_stale")),
    }
