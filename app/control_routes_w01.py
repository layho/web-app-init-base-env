"""Isolated W01 routes.

app.main does not include this router. Test proofs are rejected unless an
isolated engine has been bound in-process. The schema 2 protocol stays off
in production.
"""

import json

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from app.control_events.errors import ControlError
from app.control_events.redact import redact

PRODUCTION_PROTOCOL_ENABLED = False
router = APIRouter()
_state = {"engine": None, "isolated": False}


def bind_isolated_engine(engine) -> None:
    if PRODUCTION_PROTOCOL_ENABLED or engine.production_protocol_enabled:
        raise ControlError("NOT_AUTHORIZED", 403, "unverified schema 2 protocol cannot be enabled in production")
    _state["engine"] = engine
    _state["isolated"] = True


def clear_binding() -> None:
    _state["engine"] = None
    _state["isolated"] = False


def set_isolated(value: bool) -> None:
    _state["isolated"] = bool(value)


def _response(result: dict) -> JSONResponse:
    body = redact(result)
    return JSONResponse(body, status_code=body.get("http", 200))


def _engine():
    if not _state["isolated"] or _state["engine"] is None or PRODUCTION_PROTOCOL_ENABLED:
        raise ControlError("NOT_AUTHORIZED", 403, "isolated test identity is required")
    return _state["engine"]


def _signed_request(raw: str | None) -> dict:
    if not isinstance(raw, str) or raw.strip() == "":
        raise ControlError("NOT_AUTHORIZED", 403, "reader is not authenticated")
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise ControlError("NOT_AUTHORIZED", 403, "reader is not authenticated") from exc
    if not isinstance(body, dict):
        raise ControlError("NOT_AUTHORIZED", 403, "reader is not authenticated")
    return body


@router.post("/api/v1/control/events")
async def append_event(request: Request):
    try:
        engine = _engine()
        body = await request.json()
        result = engine.append(body)
    except ControlError as exc:
        result = exc.public()
    return _response(result)


@router.get("/api/v1/control/events")
def query_events(
    stream_id: str,
    stream_epoch: int,
    after_seq: int = 0,
    task_id: str | None = None,
    limit: int = 50,
    x_control_proof: str | None = Header(default=None),
):
    try:
        engine = _engine()
        result = engine.query(
            request=_signed_request(x_control_proof),
            stream_id=stream_id,
            stream_epoch=stream_epoch,
            after_seq=after_seq,
            task_id=task_id,
            limit=limit,
        )
    except ControlError as exc:
        result = exc.public()
    return _response(result)


@router.get("/api/v1/control/diagnostics")
def poll_diagnostics(
    subject: str,
    stream_id: str,
    stream_epoch: int,
    after_seq: int = 0,
    task_id: str | None = None,
    limit: int = 50,
    x_control_proof: str | None = Header(default=None),
):
    try:
        engine = _engine()
        result = engine.poll_diagnostics(
            request=_signed_request(x_control_proof),
            subject=subject,
            stream_id=stream_id,
            stream_epoch=stream_epoch,
            after_seq=after_seq,
            task_id=task_id,
            limit=limit,
        )
    except ControlError as exc:
        result = exc.public()
    return _response(result)


@router.post("/api/v1/control/diagnostics/cursor")
async def commit_cursor(request: Request, x_control_proof: str | None = Header(default=None)):
    try:
        engine = _engine()
        body = await request.json()
        signed = _signed_request(x_control_proof) if x_control_proof else body
        result = engine.commit_cursor(
            request=signed,
            subject=body.get("subject") or "",
            stream_id=body.get("stream_id") or signed.get("stream_id") or "",
            stream_epoch=body.get("stream_epoch"),
            after_seq=body.get("after_seq"),
            task_id=body.get("task_id"),
        )
    except ControlError as exc:
        result = exc.public()
    except TypeError:
        result = ControlError("SCHEMA_UNSUPPORTED", 422, "diagnostic cursor is invalid").public()
    return _response(result)
