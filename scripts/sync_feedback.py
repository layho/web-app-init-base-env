"""Push pending Web App admin feedback to GitHub Issues; no GitHub token in browser."""
import json, os, urllib.request, urllib.parse, urllib.error
from datetime import timedelta, timezone
from sqlalchemy import select
from app.db import SessionLocal
from app import feedback_progress
from app.models import UserFeedback, utcnow

REPO = "layho/web-app-init-base-env" if os.getenv("APP_PREVIEW_FEEDBACK") == "1" else "example/web-app"
API = "https://api.github.com"
TOKEN = os.getenv("APP_GITHUB_FEEDBACK_TOKEN", "")
def github(method, path, data=None):
    body=json.dumps(data).encode() if data is not None else None
    req=urllib.request.Request(API+path, data=body, method=method,
        headers={"Authorization":"Bearer "+TOKEN,"Accept":"application/vnd.github+json",
                 "X-GitHub-Api-Version":"2022-11-28","User-Agent":"web-app-feedback",
                 "Content-Type":"application/json"})
    with urllib.request.urlopen(req,timeout=20) as resp:
        return json.load(resp)
def issue_body(entry, source="app.example.com"):
    """Construct readable Markdown, not escaped literal backslash-n sequences."""
    return (f"Source: {source} admin feedback\n"
            f"Internal feedback ID: {entry.id}\n"
            f"Category: {entry.category}\n\n"
            f"{entry.message}\n\n"
            "Do not treat this as tested/verified without evidence.")

def find_existing_issue(entry, marker):
    # An issue can only be created after its feedback row. Allow for clock skew
    # and include closed issues so a retry cannot reopen or duplicate them.
    created_at = entry.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    since = (created_at.astimezone(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    page_size = 100
    page = 1
    while True:
        query = urllib.parse.urlencode({"state": "all", "since": since, "per_page": page_size, "page": page})
        issues = github("GET", f"/repos/{REPO}/issues?{query}")
        if not isinstance(issues, list):
            raise ValueError("Unexpected GitHub issue list")
        for issue in issues:
            title = issue.get("title", "")
            if "pull_request" not in issue and (title == marker or title.startswith(marker + " ")):
                return issue
        if len(issues) < page_size:
            return None
        page += 1


def main():
    if not TOKEN:
        print("GitHub feedback token missing; items remain queued")
        return
    with SessionLocal() as db:
        items=db.scalars(select(UserFeedback).where(UserFeedback.status=="pending").order_by(UserFeedback.id).limit(15)).all()
        for entry in items:
            marker=f"[APP-FEEDBACK #{entry.id}]"
            operation = "lookup"
            try:
                exact = find_existing_issue(entry, marker)
                if exact is None:
                    operation = "create"
                    result=github("POST",f"/repos/{REPO}/issues",{
                      "title":f"{marker} {entry.category}",
                      "body":issue_body(entry, "base.layho.com" if os.getenv("APP_PREVIEW_FEEDBACK") == "1" else "app.example.com")
                    })
                else:
                    result=exact
                entry.github_issue_number=result["number"]
                entry.status="synced"
                entry.synced_at=utcnow()
                db.commit()
                print("Synced feedback",entry.id,"to issue",entry.github_issue_number)
            except (urllib.error.URLError,ValueError,KeyError) as exc:
                db.rollback()
                reason = f"GitHub HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
                print(f"Feedback sync failed for ID {entry.id}: {operation}: {reason}", flush=True)
                return 1
    return 0

def refresh_progress():
    """Pull trusted issue milestones independently from the outbound queue."""
    if not TOKEN:
        return 0
    cache = feedback_progress.load_cache()
    now = utcnow()
    with SessionLocal() as db:
        rows = db.scalars(select(UserFeedback).where(UserFeedback.github_issue_number.is_not(None))
                          .order_by(UserFeedback.id.desc()).limit(100)).all()
        linked = [(row.id, row.github_issue_number) for row in rows]
    # Prioritize the oldest or never refreshed entries, at most 15 each run.
    def checked_at(link):
        entry = feedback_progress.current_entry(cache, *link)
        return feedback_progress.parse_time(entry.get("checked_at")) or now - timedelta(days=36500)
    due = [link for link in linked if now - checked_at(link) >= feedback_progress.REFRESH_INTERVAL]
    failed = False
    for feedback_id, issue_number in sorted(due, key=checked_at)[:15]:
        try:
            comments = []
            page = 1
            while True:
                batch = github("GET", f"/repos/{REPO}/issues/{issue_number}/comments?per_page=100&page={page}")
                if not isinstance(batch, list):
                    raise ValueError("Unexpected GitHub comments response")
                comments.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
                if page > 50:
                    raise ValueError("Feedback comments exceed refresh limit")
            cache[str(issue_number)] = {
                "feedback_id": feedback_id,
                "checked_at": utcnow().isoformat(),
                **feedback_progress.progress_from_comments(comments),
            }
        except (urllib.error.URLError, ValueError, KeyError) as exc:
            failed = True
            print(f"Feedback progress refresh failed for issue {issue_number}: {type(exc).__name__}", flush=True)
    # Retain only entries tied to the currently visible feedback records.
    cache = {str(number): cache[str(number)] for _, number in linked if str(number) in cache}
    feedback_progress.save_cache(cache)
    return 1 if failed else 0


if __name__=="__main__":
    outbound = main()
    inbound = refresh_progress()
    raise SystemExit(outbound or inbound)
