"""Opt-in, authenticated single-host heartbeat gateway; no task execution."""
import hmac
import os
import re
import threading
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from . import auth
from .db import get_db

router = APIRouter(prefix="/api/hostops/v1")
_lock = threading.Lock()
_latest = None
_clock = time.monotonic
OPERATIONS = {"host.health", "service.inventory", "agent.snapshot"}
SERVICES = {"macci.runner", "macci.preview", "macci.publish", "macci.receipts", "panel.merge"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Disk(StrictModel):
    total_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)
    free_bytes: int = Field(ge=0)


class Service(StrictModel):
    service_id: str = Field(max_length=64)
    state: Literal["unsupported", "unknown", "not_visible", "running", "loaded_idle", "last_exit_failed"]
    last_exit_code: int | None = Field(default=None, ge=-2147483648, le=2147483647)


class Heartbeat(StrictModel):
    host_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}$")
    system: Literal["Darwin", "Linux"]
    architecture: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,32}$")
    disk: Disk | None
    services: list[Service] = Field(max_length=5)
    capabilities: list[str] = Field(max_length=3)


def settings():
    host = os.getenv("HOSTOPS_HOST_ID", "")
    secret = os.getenv("HOSTOPS_AGENT_TOKEN", "")
    if os.getenv("HOSTOPS_ENABLED") != "true" or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}", host) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", secret):
        raise HTTPException(503, "HostOps not configured")
    return host, secret


@router.post("/agent/heartbeat")
def heartbeat(payload: Heartbeat, request: Request, response: Response):
    global _latest
    host, secret = settings()
    if not hmac.compare_digest(request.headers.get("authorization", "").encode(), ("Bearer " + secret).encode()):
        raise HTTPException(401, "Agent authentication required")
    if payload.host_id != host:
        raise HTTPException(403, "Host identity mismatch")
    ids = [s.service_id for s in payload.services]
    if len(ids) != len(set(ids)) or not set(ids) <= SERVICES or not set(payload.capabilities) <= OPERATIONS:
        raise HTTPException(422, "Unsupported capability or service")
    if payload.disk and max(payload.disk.used_bytes, payload.disk.free_bytes) > payload.disk.total_bytes:
        raise HTTPException(422, "Invalid disk capacity")
    response.headers["Cache-Control"] = "no-store"
    with _lock:
        _latest = (host, _clock(), payload.model_dump())
    return {"accepted": True, "schema": 1}


@router.get("/hosts")
def hosts(request: Request, response: Response, db: Session = Depends(get_db)):
    auth.authorize_session(request, db)
    response.headers["Cache-Control"] = "no-store"
    host, _ = settings()
    with _lock:
        if _latest is None or _latest[0] != host:
            return {"hosts": [{"host_id": host, "state": "never_seen", "snapshot": None}]}
        _, received, data = _latest
        age = max(0, int(_clock() - received))
        return {"hosts": [{"host_id": host, "state": "online" if age <= 90 else "stale",
                           "age_seconds": age, "snapshot": data}]}
