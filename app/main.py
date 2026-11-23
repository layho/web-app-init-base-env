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

from . import admin_settings, auth, development_tasks, feedback_progress, feedback_delivery, foundation_version, operations, pr_tracking, pr_conflict_controls, manual_pr_actions, release_acceptance, pr_draft_controls
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

class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class RenameWorkspaceIn(BaseModel):
    workspace_name: str = Field(min_length=1, max_length=80)
    reference_name: str = Field(min_length=1, max_length=80)


@app.get("/api/branding")
def public_branding(response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return admin_settings.public()
    except (OSError, ValueError):
        raise HTTPException(503, "应用名称暂不可用") from None


@app.get("/api/v1/account/settings", dependencies=[Depends(authorize)])
def account_settings(response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return admin_settings.public()
    except (OSError, ValueError):
        raise HTTPException(503, "读取应用设置失败") from None


@app.post("/api/v1/account/settings")
def rename_workspace(req: RenameWorkspaceIn, request: Request, response: Response, db: Session = Depends(get_db)):
    auth.authorize_session(request, db)
    response.headers["Cache-Control"] = "no-store"
    try:
        names = {"workspace_name": admin_settings.validate_name(req.workspace_name),
                 "reference_name": admin_settings.validate_name(req.reference_name)}
        obj = admin_settings.read()
        admin_settings.save({**obj, **names})
        return names
    except (OSError, ValueError) as exc:
        raise HTTPException(422 if isinstance(exc, ValueError) else 503,
                            str(exc) if isinstance(exc, ValueError) else "设置保存失败") from None


@app.post("/api/v1/account/password")
def update_password(req: ChangePasswordIn, request: Request, response: Response, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return auth.change_password(request, db, req.current_password, req.new_password)


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
    pr_cache = pr_tracking.snapshot() if pr_tracking.enabled() else {"prs": []}
    prs = pr_cache.get("prs", []) if not pr_cache.get("stale") else []
    return [{"delivery": feedback_delivery.timeline({
                 "created_at": x.created_at.isoformat() if x.created_at else None,
                 "synced_at": x.synced_at.isoformat() if x.synced_at else None,
                 "github_issue_number": x.github_issue_number,
                 **feedback_progress.feedback_details(progress, x.id, x.github_issue_number)
             }, prs),
             "id": x.id, "category": x.category, "message": x.message, "status": x.status,
             "github_issue_number": x.github_issue_number,
             "created_at": x.created_at.isoformat() if x.created_at else None,
             "synced_at": x.synced_at.isoformat() if x.synced_at else None,
             **feedback_progress.feedback_details(progress, x.id, x.github_issue_number)} for x in rows]

def deployment_block_reason(sha):
    status = operations.deploy_status(sha)
    if status["locked"] or status["state"] in {"request_waiting", "request_unacknowledged"}:
        return "部署请求尚未完成，请等待本机健康检查后刷新"
    if status["rollback_failed"] or status["state"] not in {"update_ok", "update_no_change", "unknown"}:
        return "更新器尚无有效完成回执，请检查系统日志并刷新"
    return ""


@app.get("/api/release-notes", dependencies=[Depends(authorize)])
def release_notes(response: Response):
    response.headers["Cache-Control"] = "no-store"
    notes_file = Path(__file__).with_name("release_notes.json")
    releases = json.loads(notes_file.read_text(encoding="utf-8"))
    running_sha = os.getenv("APP_RUNNING_SHA", "unknown")
    deployments = operations.recent_operations(operations.OPERATIONS_LOG, deployments_only=True)
    acceptance = release_acceptance.state(running_sha)
    # Fail closed if the browser has an outdated preview or GitHub cannot be reached.
    readiness = release_acceptance.acceptance_readiness(
        running_sha, app.version, releases[0]["version"], deployments
    ) if acceptance.get("enabled") else {
        "ready": False, "reason": "当前状态不允许提交验收"
    }
    if acceptance.get("enabled"):
        block_reason = deployment_block_reason(running_sha)
        if block_reason:
            readiness.update(ready=False, reason=block_reason)
    return {
        "version": app.version,
        "running_sha": running_sha,
        "releases": releases,
        "deployments": deployments,
        "acceptance": {**acceptance, **readiness, "ready": readiness["ready"] and acceptance.get("state") == "not_requested"},
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
    block_reason = deployment_block_reason(req.sha)
    if block_reason:
        raise HTTPException(409, block_reason)
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
def list_development_tasks(response: Response, refresh: bool = False):
    response.headers["Cache-Control"] = "no-store"
    if refresh and pr_tracking.enabled():
        # Explicit UI refresh bypasses the five-minute local snapshot, but
        # uses the trusted instance's repository and only GitHub GET reads.
        # Never show an old snapshot as if the manual refresh succeeded.
        try:
            pr_tracking.sync(lambda method, path: release_acceptance.github(path),
                             force=True)
        except (release_acceptance.AcceptanceError, OSError, ValueError,
                TypeError, KeyError):
            raise HTTPException(503, "GitHub 实时同步失败；仍可查看上次同步记录") from None
    result = development_tasks.snapshot()
    if pr_tracking.enabled():
        result["pr_tracking"] = pr_tracking.snapshot()
        # Draft authority differs by repository, but the shared manual
        # MacCI toolbar is available to explicitly allowlisted app workers.
        result["pr_tracking"]["manual_actions_available"] = manual_pr_actions.configured()
        result["tasks"] = development_tasks.unified_tasks(
            result.get("tasks", []), result["pr_tracking"])
        result["unified"] = True
        result["stale"] = result["stale"] or result["pr_tracking"].get("stale", True)
    return result


class DraftReleaseIn(BaseModel):
    number: int = Field(ge=1, le=1000000)
    head: str = Field(pattern=r"^[0-9a-f]{40}$")


@app.post("/api/v1/pr-drafts/release")
def release_draft_pr(req: DraftReleaseIn, request: Request, response: Response,
                     db: Session = Depends(get_db)):
    # Admin session + CSRF + same-origin are mandatory; service API tokens
    # must never authorize GitHub PR mutations.
    auth.authorize_session(request, db)
    response.headers["Cache-Control"] = "no-store"
    try:
        return pr_draft_controls.release(req.number, req.head)
    except (pr_draft_controls.DraftReleaseError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(409, str(exc) if isinstance(exc, pr_draft_controls.DraftReleaseError)
                            else "PR 状态已变化，请刷新后重试") from None


class ConflictRepairIn(BaseModel):
    number: int = Field(ge=1, le=1000000)
    head: str = Field(pattern=r"^[0-9a-f]{40}$")
    base: str = Field(pattern=r"^[0-9a-f]{40}$")


@app.post("/api/v1/pr-conflicts/repair")
def request_conflict_repair(req: ConflictRepairIn, request: Request, response: Response,
                            db: Session = Depends(get_db)):
    # A logged-in administrator must explicitly confirm the current HEAD and
    # base. Never accept a browser-specified repository or GitHub token.
    auth.authorize_session(request, db)
    response.headers["Cache-Control"] = "no-store"
    try:
        return pr_conflict_controls.request_repair(req.number, req.head, req.base)
    except (pr_conflict_controls.ConflictError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(409, str(exc) if isinstance(exc, pr_conflict_controls.ConflictError)
                            else "PR 已变化，请刷新后重试") from None


class ManualPRActionIn(BaseModel):
    number: int = Field(ge=1, le=1000000)
    head: str = Field(pattern=r"^[0-9a-f]{40}$")
    base: str = Field(pattern=r"^[0-9a-f]{40}$")


@app.post("/api/v1/pr-actions/test", status_code=202)
def manual_test_pr(req: ManualPRActionIn, request: Request, response: Response,
                   db: Session = Depends(get_db)):
    auth.authorize_session(request, db)
    response.headers["Cache-Control"] = "no-store"
    try:
        return manual_pr_actions.request_test(req.number, req.head, req.base)
    except manual_pr_actions.ManualActionError as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/api/v1/pr-actions/merge-check", status_code=202)
def manual_merge_pr(req: ManualPRActionIn, request: Request, response: Response,
                    db: Session = Depends(get_db)):
    auth.authorize_session(request, db)
    response.headers["Cache-Control"] = "no-store"
    try:
        return manual_pr_actions.request_merge(req.number, req.head, req.base)
    except manual_pr_actions.ManualActionError as exc:
        raise HTTPException(409, str(exc)) from None


@app.get("/api/v1/system-logs", dependencies=[Depends(authorize)])
def system_logs(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"events": operations.recent_operations(operations.OPERATIONS_LOG)}


@app.get("/api/v1/deploy/status", dependencies=[Depends(authorize)])
def deployment_status(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return operations.deploy_status(os.getenv("APP_RUNNING_SHA", "unknown"))


@app.get("/api/v1/deploy/check", dependencies=[Depends(authorize)])
def check_deploy_update(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return operations.check_available_update(os.getenv("APP_RUNNING_SHA", "unknown"))


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
