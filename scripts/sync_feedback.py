"""Push pending Web App admin feedback to GitHub Issues; no GitHub token in browser."""
import json, os, urllib.request, urllib.parse, urllib.error
from datetime import timedelta, timezone
from sqlalchemy import select
from app.db import SessionLocal
from app.models import UserFeedback, utcnow

REPO = "example/web-app"
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
def issue_body(entry):
    """Construct readable Markdown, not escaped literal backslash-n sequences."""
    return ("Source: app.example.com admin feedback\n"
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
                      "body":issue_body(entry)
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

if __name__=="__main__":
    raise SystemExit(main())
