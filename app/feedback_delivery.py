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
    linked = next((row for row in prs if feedback.get("developer_pr") == row.get("number")
                   and valid_sha(feedback.get("delivered_sha"))
                   and row.get("head") == feedback["delivered_sha"]), None)
    tested = linked.get("test", {}) if linked else {}
    deployment = linked.get("deployment", {}) if linked else {}
    events = {
        "submitted": feedback.get("created_at"),
        "synced": feedback.get("synced_at") if feedback.get("github_issue_number") else None,
        "claimed": feedback.get("claimed_at"),
        "coding": feedback.get("development_started_at"),
        "pr": feedback.get("development_submitted_at") if linked else None,
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
    if feedback.get("overall_status") == "blocked":
        phase = "blocked"
    elif any(x["state"] == "failed" for x in steps):
        phase = "failed"
    else:
        phase = next((x["key"] for x in steps if x["state"] == "pending"), "accepted")
    # This data source has no authenticated executor heartbeat nor next-run
    # record from ChatGPT Automations. Never calculate fictitious run times.
    return {
        "steps": steps, "current_stage": phase,
        "executor": {"name": None, "mode": "unverified",
                     "running": None, "last_run_at": None,
                     "next_run_at": None, "schedule_source": None},
        "evidence": {"issue": feedback.get("github_issue_number"),
                     "pr": linked.get("number") if linked else None,
                     "head_sha": linked.get("head") if linked else None,
                     "merge_sha": linked.get("merge_sha") if linked else None},
        "last_verified_at": feedback.get("progress_synced_at"),
        "stale": bool(feedback.get("progress_stale")),
    }
