"""Core API for the empty App workbench."""
import json
import os
from pathlib import Path
import subprocess

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import auth, development_tasks, feedback_progress, foundation_version, operations, pr_tracking, release_acceptance
from .db import get_db
from .models import UserFeedback

RELEASE_NOTES = Path(__file__).with_name("release_notes.json")
app = FastAPI(title="Web App Foundation", version=json.loads(RELEASE_NOTES.read_text(encoding="utf-8"))[0]["version"], docs_url="/docs" if os.getenv("APP_ENABLE_DOCS") == "true" else None, openapi_url="/openapi.json" if os.getenv("APP_ENABLE_DOCS") == "true" else None)


def authorize(request: Request, db: Session = Depends(get_db)):
    return auth.authorize_request(request, db)

class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)

@app.post("/api/login")
def login(req: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return auth.login(request, response, db, req.username, req.password)

@app.get("/api/session")
def current_session(request: Request, response: Response, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return auth.session_info(request, db)

@app.post("/api/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return auth.logout(request, response, db)

@app.get("/api/health")
def health():
    return {"status": "ok"}

class FeedbackIn(BaseModel):
    category: str = Field(pattern="^(bug|suggestion|acceptance)$")
    message: str = Field(min_length=5, max_length=2000)

@app.post("/api/v1/feedback", dependencies=[Depends(authorize)])
def submit_feedback(req: FeedbackIn, db: Session = Depends(get_db)):
    row = UserFeedback(category=req.category, message=req.message.strip(), status="pending")
    if len(row.message) < 5:
        raise HTTPException(422, "Feedback too short")
    db.add(row)
    db.commit()
    return {"id": row.id, "status": row.status}

@app.get("/api/v1/feedback", dependencies=[Depends(authorize)])
def list_feedback(response: Response, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    rows = db.scalars(select(UserFeedback).order_by(UserFeedback.id.desc()).limit(100)).all()
    progress = feedback_progress.load_cache()
    return [{"id": x.id, "category": x.category, "message": x.message, "status": x.status,
             "github_issue_number": x.github_issue_number,
             "created_at": x.created_at.isoformat() if x.created_at else None,
             "synced_at": x.synced_at.isoformat() if x.synced_at else None,
             **feedback_progress.feedback_details(progress, x.id, x.github_issue_number)} for x in rows]

@app.get("/api/release-notes", dependencies=[Depends(authorize)])
def release_notes(response: Response):
    response.headers["Cache-Control"] = "no-store"
    notes_file = Path(__file__).with_name("release_notes.json")
    return {
        "version": app.version,
        "running_sha": os.getenv("APP_RUNNING_SHA", "unknown"),
        "releases": json.loads(notes_file.read_text(encoding="utf-8")),
        "deployments": operations.recent_operations(operations.OPERATIONS_LOG, deployments_only=True),
        "acceptance": release_acceptance.state(os.getenv("APP_RUNNING_SHA", "unknown")),
    }


class ReleaseAcceptanceIn(BaseModel):
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    version: str = Field(min_length=1, max_length=40)


@app.post("/api/v1/releases/accept")
def accept_release(req: ReleaseAcceptanceIn, request: Request, response: Response, db: Session = Depends(get_db)):
    auth.authorize_session(request, db)
    if request.headers.get("origin") != os.getenv("APP_PUBLIC_ORIGIN") or not request.headers.get("origin"):
        raise HTTPException(403, "请在基座验收站确认发布")
    if not release_acceptance.enabled():
        raise HTTPException(403, "仅基座验收站允许发起正式发布")
    response.headers["Cache-Control"] = "no-store"
    notes = json.loads(RELEASE_NOTES.read_text(encoding="utf-8"))
    if req.version != app.version or notes[0]["version"] != req.version or not notes[0].get("acceptance_required"):
        raise HTTPException(409, "当前版本不匹配或无需验收")
    deployments = operations.recent_operations(operations.OPERATIONS_LOG, deployments_only=True)
    deployed = bool(deployments and deployments[0]["sha"] == req.sha)
    try:
        return release_acceptance.submit(req.sha, req.version, deployed)
    except (release_acceptance.AcceptanceError, KeyError, TypeError, OSError) as exc:
        message = str(exc) if isinstance(exc, release_acceptance.AcceptanceError) else "验收请求失败，请检查本机发布日志"
        raise HTTPException(409, message) from None


@app.get("/api/v1/project-info", dependencies=[Depends(authorize)])
def project_info(response: Response, refresh: bool = False):
    response.headers["Cache-Control"] = "no-store"
    info_file = Path(__file__).with_name("project_info.json")
    data = json.loads(info_file.read_text(encoding="utf-8"))
    data["foundation_version"] = foundation_version.snapshot(force=refresh)
    return data


@app.get("/api/v1/development-tasks", dependencies=[Depends(authorize)])
def list_development_tasks(response: Response):
    response.headers["Cache-Control"] = "no-store"
    result = development_tasks.snapshot()
    if pr_tracking.enabled():
        result["pr_tracking"] = pr_tracking.snapshot()
    return result


@app.get("/api/v1/system-logs", dependencies=[Depends(authorize)])
def system_logs(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"events": operations.recent_operations(operations.OPERATIONS_LOG)}


@app.post("/api/v1/deploy/update", status_code=202)
def trigger_update(request: Request, response: Response, db: Session = Depends(get_db)):
    auth.authorize_session(request, db)
    response.headers["Cache-Control"] = "no-store"
    target = operations.updater_target()
    if not target:
        raise HTTPException(503, "当前环境未配置手动部署")
    try:
        operations.request_update(target)
    except (OSError, subprocess.SubprocessError):
        raise HTTPException(503, "无法启动更新任务，请查看 Mac 上的更新服务") from None
    return {"accepted": True}


@app.get("/api/version")
def version(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {
        "version": app.version,
        "running_sha": os.getenv("APP_RUNNING_SHA", "unknown"),
        "manual_update_available": operations.updater_target() is not None,
    }


# Cloudflare forwards the API and the React build to the same local service.
frontend_dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
if frontend_dist.joinpath("index.html").is_file():
    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")
