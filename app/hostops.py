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
from .hostops_tasks import TaskStore, TaskConflict, TaskCapacity
import sqlite3

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


class TaskRequest(StrictModel):
    host_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}$")
    operation: Literal["host.health", "service.inventory", "agent.snapshot"]
    parameters: dict = Field(default_factory=dict, max_length=0)
    timeout_seconds: int = Field(default=120, ge=15, le=300)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{16,96}$")
    reason: str = Field(min_length=1, max_length=240)


def task_store():
    directory = os.getenv("HOSTOPS_STATE_DIR", "")
    if not directory:
        raise HTTPException(503, "HostOps task storage not configured")
    try:
        return TaskStore(directory)
    except (OSError, ValueError, sqlite3.Error):
        raise HTTPException(503, "HostOps task storage unavailable") from None


@router.post("/tasks")
def create_task(payload: TaskRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    actor = auth.authorize_session(request, db)
    host, _ = settings()
    if payload.host_id != host:
        raise HTTPException(403, "Host identity mismatch")
    if not payload.reason.strip() or any(ord(c) < 32 for c in payload.reason):
        raise HTTPException(422, "Invalid reason")
    response.headers["Cache-Control"] = "no-store"
    try:
        task, created = task_store().enqueue(actor, host, payload.operation, payload.idempotency_key,
                                             payload.reason, payload.timeout_seconds)
    except TaskConflict:
        raise HTTPException(409, "Idempotency key conflict") from None
    except TaskCapacity:
        raise HTTPException(429, "HostOps task queue capacity reached") from None
    except sqlite3.Error:
        raise HTTPException(503, "HostOps task storage unavailable") from None
    response.status_code = 202 if created else 200
    return {"task": task, "created": created, "dispatch_enabled": False}


@router.get("/tasks")
def list_tasks(request: Request, response: Response, db: Session = Depends(get_db)):
    auth.authorize_session(request, db)
    host, _ = settings()
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"tasks": task_store().list(host), "dispatch_enabled": False}
    except sqlite3.Error:
        raise HTTPException(503, "HostOps task storage unavailable") from None
