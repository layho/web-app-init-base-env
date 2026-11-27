"""Isolated W01 routes.

app.main does not include this router. Test proofs are rejected unless an
isolated engine has been bound in-process. The schema 2 protocol stays off
in production.
"""

from fastapi import APIRouter, Request
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
def query_events(stream_id: str, stream_epoch: int, after_seq: int = 0):
    try:
        engine = _engine()
        result = engine.query(stream_id=stream_id, stream_epoch=stream_epoch, after_seq=after_seq)
    except ControlError as exc:
        result = exc.public()
    return _response(result)


@router.get("/api/v1/control/diagnostics")
def poll_diagnostics(subject: str, stream_id: str, stream_epoch: int, after_seq: int = 0):
    try:
        engine = _engine()
        result = engine.poll_diagnostics(
            subject=subject, stream_id=stream_id, stream_epoch=stream_epoch, after_seq=after_seq
        )
    except ControlError as exc:
        result = exc.public()
    return _response(result)
