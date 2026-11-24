"""Read-only development progress derived from trusted GitHub issue comments.

The worker refreshes a local cache; the web API never needs a GitHub token.
An outbound sync is not the same as completed development.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

CACHE_PATH = Path(__file__).resolve().parents[1] / "logs" / "feedback-progress.json"
MARKER = "[APP_PROGRESS] "
EXECUTION_MARKER = "[APP_EXECUTION] "
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
STAGES = {
    "claimed": "claimed_at",
    "in_progress": "development_started_at",
    "development_finished": "development_finished_at",
    "submitted": "development_submitted_at",
}
STATUSES = set(STAGES) | {"ready_for_review", "accepted", "blocked"}
REFRESH_INTERVAL = timedelta(minutes=5)


def parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return ts.astimezone(timezone.utc) if ts.tzinfo is not None else None
    except ValueError:
        return None


def blank_progress():
    return {
        **{field: None for field in STAGES.values()},
        "overall_status": "awaiting_claim",
        "developer_reply": "",
        "delivered_sha": None,
        "developer_pr": None,
        "accepted_at": None,
        "developer_actor": None,
        "developer_agent": None,
        "executor_receipt": None,
    }


def progress_from_comments(comments):
    """Use explicit stages only. Never invent missing event times or acceptance."""
    result = blank_progress()
    events = []
    for comment in comments:
        if not isinstance(comment, dict) or comment.get("author_association") not in TRUSTED_ASSOCIATIONS:
            continue
        created = parse_time(comment.get("created_at"))
        updated = parse_time(comment.get("updated_at"))
        # Edits must not be backdated. Developers should append new comments.
        if created is None or (updated is not None and updated != created):
            continue
        body = comment.get("body")
        number = comment.get("id")
        if not isinstance(body, str) or not isinstance(number, int):
            continue
        # Execution evidence is separate from development milestones. A
        # scheduled task is not running merely because its schedule exists.
        if body.startswith(EXECUTION_MARKER) and len(body) <= 3000:
            try:
                receipt = json.loads(body[len(EXECUTION_MARKER):])
            except ValueError:
                continue
            if not isinstance(receipt, dict):
                continue
            state, runner, task = receipt.get("state"), receipt.get("executor"), receipt.get("task_id")
            if (state not in {"started", "finished", "failed", "queued"} or
                    not isinstance(runner, str) or not re.fullmatch(r"[A-Za-z0-9_. -]{1,64}", runner) or
                    not isinstance(task, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", task)):
                continue
            next_run = parse_time(receipt.get("next_run_at"))
            events.append((created, number, {"status": "_execution", "state": state, "executor": runner,
                                             "task_id": task, "next_run_at": next_run.isoformat() if next_run else None}))
            continue
        lines = [line for line in body.splitlines() if line.startswith(MARKER)]
        if not lines and body.startswith("[APP-CLAIM] "):
            events.append((created, number, {"status": "claimed", "reply": body[len("[APP-CLAIM] "):],
                                         "actor": (comment.get("user") or {}).get("login")}))
            continue
        if len(lines) != 1 or len(lines[0]) > 10000:
            continue
        try:
            event = json.loads(lines[0][len(MARKER):])
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        status = event.get("status")
        if not isinstance(status, str) or status not in STATUSES:
            continue
        sha, pr = event.get("sha"), event.get("pr")
        if status in {"submitted", "ready_for_review", "accepted"}:
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
                continue
            if type(pr) is not int or pr <= 0:
                continue
        if event.get("agent") not in ("Codex", "Grok", "Claude", "DeepSeek", "Qwen"):
            event.pop("agent", None)
        event.pop("actor", None)
        actor = (comment.get("user") or {}).get("login")
        if isinstance(actor, str) and re.fullmatch(r"[A-Za-z0-9-]{1,39}", actor):
            event = {**event, "actor": actor}
        events.append((created, number, event))
    for at, _, event in sorted(events, key=lambda row: (row[0], row[1])):
        status = event["status"]
        if status == "_execution":
            result["executor_receipt"] = {"state": event["state"],
                 "executor": event["executor"], "task_id": event["task_id"],
                 "last_event_at": at.isoformat(), "next_run_at": event["next_run_at"]}
            continue
        if status in STAGES:
            field = STAGES[status]
            if result[field] is None:
                result[field] = at.isoformat()
        result["overall_status"] = status
        if status == "accepted" and result["accepted_at"] is None:
            result["accepted_at"] = at.isoformat()
        if status in {"claimed", "in_progress", "development_finished", "submitted", "blocked"}:
            actor = event.get("actor")
            if isinstance(actor, str) and re.fullmatch(r"[A-Za-z0-9-]{1,39}", actor):
                result["developer_actor"] = actor
                result["developer_agent"] = event.get("agent")
        reply = event.get("reply")
        if isinstance(reply, str):
            result["developer_reply"] = reply[:6000]
        if status in {"submitted", "ready_for_review", "accepted"}:
            result["delivered_sha"] = event["sha"]
            result["developer_pr"] = event["pr"]
    return result


def load_cache(path=None):
    path = CACHE_PATH if path is None else path
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
        return content if isinstance(content, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(cache, path=None):
    path = CACHE_PATH if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temp:
        try:
            json.dump(cache, temp, ensure_ascii=False)
            temp.close()
            os.replace(temp.name, path)
        finally:
            if os.path.exists(temp.name):
                os.unlink(temp.name)


def current_entry(cache, feedback_id, issue_number):
    entry = cache.get(str(issue_number))
    return entry if isinstance(entry, dict) and entry.get("feedback_id") == feedback_id else {}


def feedback_details(cache, feedback_id, issue_number, now=None):
    result = blank_progress()
    if issue_number is None:
        result["overall_status"] = "pending_sync"
    entry = current_entry(cache, feedback_id, issue_number)
    at = parse_time(entry.get("checked_at"))
    for key in result:
        if key in entry:
            result[key] = entry[key]
    result["progress_synced_at"] = at.isoformat() if at else None
    result["progress_stale"] = issue_number is not None and (
        at is None or (now or datetime.now(timezone.utc)) - at > 2 * REFRESH_INTERVAL
    )
    return result
