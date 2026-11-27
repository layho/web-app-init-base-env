"""Isolated W02 routes.

``app.main`` does not include this router. Production verification stays off.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

import base64
import json

from app.control_authz.canonical import canonical_query

from .control_identity.boundary import CSP, HSTS, redact
from .control_identity.errors import ControlError
from .control_identity.service import ControlIdentityService

router = APIRouter()


def _error(exc: ControlError) -> JSONResponse:
    return JSONResponse(exc.public_body(), status_code=exc.status)


def _schema(detail: str) -> JSONResponse:
    return JSONResponse(
        {"schema_version": 2, "http": 422, "code": "SCHEMA_UNSUPPORTED", "detail": detail, "side_effect": False},
        status_code=422,
    )


async def _invoke(request: Request, operation: str, match: dict | None = None, document: dict | None = None) -> JSONResponse:
    if document is None:
        try:
            body = await request.json()
        except Exception:
            return _schema("invalid json")
    else:
        body = document
    if not isinstance(body, dict):
        return _schema("json object required")
    for key, value in (match or {}).items():
        if str(body.get(key) or "") != str(value):
            return _schema(f"{key} does not match the path")
    service: ControlIdentityService = request.app.state.identity
    try:
        result = service.execute(operation, body, route_class=request.app.state.route_class)
    except ControlError as exc:
        return _error(exc)
    return JSONResponse(result.body, status_code=result.status)


def build_app(service: ControlIdentityService, *, route_class: str = "isolated") -> FastAPI:
    app = FastAPI()
    app.state.identity = service
    app.state.route_class = route_class

    @app.middleware("http")
    async def security_boundary(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            response = JSONResponse(
                {"schema_version": 2, "http": 500, "code": "NOT_AUTHORIZED", "detail": "request failed", "side_effect": False},
                status_code=500,
            )
        payload = b""
        iterator = getattr(response, "body_iterator", None)
        if iterator is not None:
            async for chunk in iterator:
                payload += chunk
        else:
            payload = getattr(response, "body", b"") or b""
        text = redact(payload.decode("utf-8", "replace"))
        headers = {key: value for key, value in response.headers.items() if key.lower() != "content-length"}
        headers["content-security-policy"] = CSP
        headers["x-content-type-options"] = "nosniff"
        headers["cache-control"] = "no-store"
        if request.url.scheme == "https":
            headers["strict-transport-security"] = HSTS
        else:
            headers.pop("strict-transport-security", None)
        return Response(content=text, status_code=response.status_code, media_type="application/json", headers=headers)

    app.include_router(router)
    return app


@router.get("/api/v1/control/branch-protection")
async def branch_protection(request: Request):
    encoded = request.headers.get("x-control-proof")
    if not encoded:
        return JSONResponse(
            {
                "schema_version": 2,
                "http": 401,
                "code": "NOT_AUTHORIZED",
                "detail": "authentication required",
                "reason": "unauthenticated",
                "side_effect": False,
                "evidence_source": "server_readonly_observation",
                "permission": "branch.read",
            },
            status_code=401,
        )
    try:
        document = json.loads(base64.b64decode(encoded, validate=True))
        query = canonical_query(request.url.query)
    except Exception:
        return _schema("branch protection proof rejected")
    if not isinstance(document, dict) or document.get("query") != query or document.get("method") != "GET":
        return _schema("branch protection proof rejected")
    return await _invoke(request, "branch.read", document=document)


@router.post("/api/v1/control/policy:evaluate")
async def policy_evaluate(request: Request):
    return await _invoke(request, "policy.evaluate")


@router.post("/api/v1/control/tasks/{task_id}:claim")
async def claim(task_id: str, request: Request):
    return await _invoke(request, "run.claim", {"task_id": task_id})


@router.post("/api/v1/control/tasks/{task_id}:renew")
async def renew(task_id: str, request: Request):
    return await _invoke(request, "run.renew", {"task_id": task_id})


@router.post("/api/v1/control/runs/{run_id}:start")
async def start(run_id: str, request: Request):
    return await _invoke(request, "run.start", {"run_id": run_id})


@router.post("/api/v1/control/runs/{run_id}:complete")
async def complete(run_id: str, request: Request):
    return await _invoke(request, "run.complete", {"run_id": run_id})


@router.post("/api/v1/control/runs/{run_id}:release")
async def release(run_id: str, request: Request):
    return await _invoke(request, "run.release", {"run_id": run_id})


@router.post("/api/v1/control/runs/{run_id}:heartbeat")
async def heartbeat(run_id: str, request: Request):
    return await _invoke(request, "run.heartbeat", {"run_id": run_id})


@router.post("/api/v1/control/runs/{run_id}:reconcile")
async def reconcile(run_id: str, request: Request):
    return await _invoke(request, "run.reconcile", {"run_id": run_id})


@router.post("/api/v1/control/identity/proxy:authorize")
async def proxy_authorize(request: Request):
    return await _invoke(request, "proxy.authorize")


@router.post("/api/v1/control/identity/proxy:rotate")
async def proxy_rotate(request: Request):
    return await _invoke(request, "proxy.rotate")


@router.post("/api/v1/control/ci:status")
async def ci_status(request: Request):
    return await _invoke(request, "ci.status.write")


@router.post("/api/v1/control/releases/{version}:accept")
async def accept(version: str, request: Request):
    return await _invoke(request, "release.accept", {"release_version": version})


@router.post("/api/v1/control/releases/{version}:reject")
async def reject(version: str, request: Request):
    return await _invoke(request, "release.reject", {"release_version": version})


@router.post("/api/v1/control/releases/{version}:publish")
async def publish(version: str, request: Request):
    return await _invoke(request, "release.publish", {"release_version": version})


@router.post("/api/v1/control/feedback:write")
async def feedback_write(request: Request):
    return await _invoke(request, "feedback.write")


@router.post("/api/v1/control/pull:read")
async def pull_read(request: Request):
    return await _invoke(request, "pull.read")


@router.post("/api/v1/control/preview:merge")
async def preview_merge(request: Request):
    return await _invoke(request, "preview.merge")


@router.post("/api/v1/control/diagnostics:read")
async def diagnostics(request: Request):
    return await _invoke(request, "diagnostic.read")


@router.post("/api/v1/control/sessions:issue")
async def session_issue(request: Request):
    return await _invoke(request, "session.issue")


@router.post("/api/v1/control/sessions:revoke")
async def session_revoke(request: Request):
    return await _invoke(request, "session.revoke")


@router.post("/api/v1/control/sessions:check")
async def session_check(request: Request):
    return await _invoke(request, "session.check")


@router.post("/api/v1/control/limits:consume")
async def limits(request: Request):
    return await _invoke(request, "limit.consume")


@router.post("/api/v1/control/github-effects:submit")
async def effects(request: Request):
    return await _invoke(request, "github.effect")


@router.post("/api/v1/control/host:privilege")
async def privilege(request: Request):
    return await _invoke(request, "host.privilege")


@router.post("/api/v1/control/identity/signers:revoke")
async def revoke_signer(request: Request):
    return await _invoke(request, "signer.revoke")
