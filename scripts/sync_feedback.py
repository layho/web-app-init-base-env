"""Push pending Web App admin feedback to GitHub Issues; no GitHub token in browser."""
import json, os, urllib.request, urllib.parse, urllib.error
from datetime import timedelta, timezone
from sqlalchemy import select
from app.db import SessionLocal
from app import feedback_progress, feedback_attachments
from app.models import UserFeedback, utcnow

REPO = "layho/web-app-init-base-env" if os.getenv("APP_PREVIEW_FEEDBACK") == "1" else "example/web-app"
API = "https://api.github.com"
TOKEN = os.getenv("APP_GITHUB_FEEDBACK_TOKEN", "")

def feedback_token():
    return (os.getenv("APP_GITHUB_TOKEN", "").strip()
            or os.getenv("APP_GITHUB_PR_TOKEN", "").strip()
            or os.getenv("APP_GITHUB_FEEDBACK_TOKEN", "").strip()
            or TOKEN.strip())
def github(method, path, data=None):
    body=json.dumps(data).encode() if data is not None else None
    req=urllib.request.Request(API+path, data=body, method=method,
        headers={"Authorization":"Bearer "+feedback_token(),"Accept":"application/vnd.github+json",
                 "X-GitHub-Api-Version":"2022-11-28","User-Agent":"web-app-feedback",
                 "Content-Type":"application/json"})
    with urllib.request.urlopen(req,timeout=20) as resp:
        return json.load(resp)
def feedback_source():
    return "base.layho.com" if os.getenv("APP_PREVIEW_FEEDBACK") == "1" else "app.example.com"


def issue_body(entry, source="app.example.com"):
    """Construct readable Markdown, not escaped literal backslash-n sequences."""
    attachments=feedback_attachments.listing("feedback",entry.id)
    origin=os.getenv("APP_PUBLIC_ORIGIN","").rstrip("/")
    evidence="\n".join(f"- 截图（需授权）：{origin}{x['url']}" for x in attachments) if origin.startswith("https://") else ""
    return (f"Source: {source} admin feedback\n"
            f"Internal feedback ID: {entry.id}\n"
            f"Category: {entry.category}\n\n"
            f"{entry.message}\n\n"
            f"{evidence + chr(10) + chr(10) if evidence else ''}"
            "Do not treat this as tested/verified without evidence.")


def matches_issue(issue, feedback_id, category):
    if not isinstance(issue, dict) or "pull_request" in issue:
        return False
    number = issue.get("number")
    body = issue.get("body")
    association = issue.get("author_association")
    marker = f"[APP-FEEDBACK #{feedback_id}]"
    return (type(number) is int and number > 0
            and isinstance(association, str) and association in feedback_progress.TRUSTED_ASSOCIATIONS
            and issue.get("title") in (marker, f"{marker} {category}")
            and isinstance(body, str)
            and body.splitlines()[:3] == [f"Source: {feedback_source()} admin feedback",
                                         f"Internal feedback ID: {feedback_id}", f"Category: {category}"])


def find_existing_issue(entry):
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
            if matches_issue(issue, entry.id, entry.category):
                return issue
        if len(issues) < page_size:
            return None
        page += 1


def main():
    if not feedback_token():
        print("GitHub feedback token missing; items remain queued")
        return
    with SessionLocal() as db:
        items=db.scalars(select(UserFeedback).where(UserFeedback.status=="pending").order_by(UserFeedback.id).limit(15)).all()
        for entry in items:
            marker=f"[APP-FEEDBACK #{entry.id}]"
            operation = "lookup"
            try:
                exact = find_existing_issue(entry)
                if exact is None:
                    operation = "create"
                    result=github("POST",f"/repos/{REPO}/issues",{
                      "title":f"{marker} {entry.category}",
                      "body":issue_body(entry, feedback_source())
                    })
                else:
                    result=exact
                if not matches_issue(result, entry.id, entry.category):
                    raise ValueError("Unverified feedback issue")
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
    if not feedback_token():
        return 0
    cache = feedback_progress.load_cache()
    now = utcnow()
    with SessionLocal() as db:
        rows = db.scalars(select(UserFeedback).where(UserFeedback.github_issue_number.is_not(None))
                          .order_by(UserFeedback.id.desc()).limit(100)).all()
        linked = [(row.id, row.github_issue_number, row.category) for row in rows]
    # Prioritize the oldest or never refreshed entries, at most 15 each run.
    def checked_at(link):
        entry = feedback_progress.current_entry(cache, *link[:2])
        return feedback_progress.parse_time(entry.get("checked_at")) or now - timedelta(days=36500)
    due = [link for link in linked if now - checked_at(link) >= feedback_progress.REFRESH_INTERVAL]
    failed = False
    for feedback_id, issue_number, category in sorted(due, key=checked_at)[:15]:
        try:
            issue = github("GET", f"/repos/{REPO}/issues/{issue_number}")
            if not matches_issue(issue, feedback_id, category) or issue["number"] != issue_number:
                cache.pop(str(issue_number), None)
                raise ValueError("Unverified feedback issue link")
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
    cache = {str(number): cache[str(number)] for _, number, _ in linked if str(number) in cache}
    feedback_progress.save_cache(cache)
    return 1 if failed else 0


if __name__=="__main__":
    outbound = main()
    inbound = refresh_progress()
    raise SystemExit(outbound or inbound)
