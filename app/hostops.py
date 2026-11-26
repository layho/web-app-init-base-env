"""Opt-in single-host heartbeat and read-only diagnostic task gateway."""
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
_wall_clock = time.time
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
    runtime_digest: str | None = Field(default=None,pattern=r"^[0-9a-f]{64}$")
    dispatch_active: bool = False


def dispatch_ready():
    key = os.getenv("HOSTOPS_JOB_SIGNING_KEY","")
    secret = os.getenv("HOSTOPS_AGENT_TOKEN","")
    return (os.getenv("HOSTOPS_DISPATCH_ENABLED") == "true" and bool(re.fullmatch(r"[0-9a-f]{64}",key))
            and not hmac.compare_digest(key,secret))


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
    data=payload.model_dump()
    if os.getenv("HOSTOPS_STATE_DIR"):
        try:
            task_store().save_snapshot(host,data,now=int(_wall_clock()))
        except (sqlite3.Error,ValueError):
            raise HTTPException(503,"HostOps snapshot storage unavailable") from None
    with _lock:
        _latest = (host, _clock(), data)
    return {"accepted": True, "schema": 1}


def host_evidence(host):
    # Configured persistent storage is authoritative across worker processes.
    if os.getenv("HOSTOPS_STATE_DIR"):
        try:
            evidence=task_store().snapshot(host,now=int(_wall_clock()))
        except (sqlite3.Error,ValueError):
            raise HTTPException(503,"HostOps snapshot storage unavailable") from None
        if evidence is None:
            return None,None
        return evidence
    with _lock:
        if _latest is None or _latest[0]!=host:
            return None,None
        _,received,data=_latest
        elapsed=_clock()-received
        return int(elapsed) if elapsed>=0 else 91,data


@router.get("/hosts")
def hosts(request: Request, response: Response, db: Session = Depends(get_db)):
    auth.authorize_session(request, db)
    response.headers["Cache-Control"] = "no-store"
    host, _ = settings()
    age,data=host_evidence(host)
    state="never_seen" if age is None else "online" if age<=90 else "stale"
    return {"hosts":[{"host_id":host,"state":state,"age_seconds":age,"snapshot":data}]}


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
    return {"task": task, "created": created, "dispatch_enabled": dispatch_ready()}


@router.get("/tasks")
def list_tasks(request: Request, response: Response, db: Session = Depends(get_db)):
    auth.authorize_session(request, db)
    host, _ = settings()
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"tasks": task_store().list(host), "dispatch_enabled": dispatch_ready()}
    except sqlite3.Error:
        raise HTTPException(503, "HostOps task storage unavailable") from None


class ClaimRequest(StrictModel):
    host_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}$")


@router.post("/agent/claim")
def claim_task(payload: ClaimRequest, request: Request, response: Response):
    host, secret = settings()
    if not hmac.compare_digest(request.headers.get("authorization","").encode(),
                               ("Bearer "+secret).encode()):
        raise HTTPException(401, "Agent authentication required")
    if payload.host_id != host:
        raise HTTPException(403, "Host identity mismatch")
    key = os.getenv("HOSTOPS_JOB_SIGNING_KEY","")
    if not dispatch_ready():
        raise HTTPException(503, "HostOps dispatch not configured")
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"claim":task_store().claim(host, bytes.fromhex(key))}
    except sqlite3.Error:
        raise HTTPException(503, "HostOps task storage unavailable") from None


class HealthResult(StrictModel):
    system: Literal["Darwin","Linux"]
    architecture: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,32}$")
    disk: Disk | None
    cpu_percent: None = None
    memory_percent: None = None


class SnapshotResult(StrictModel):
    health: HealthResult
    services: list[Service] = Field(max_length=5)
    capabilities: list[Literal["host.health","service.inventory","agent.snapshot"]] = Field(max_length=3)
    transport: Literal["local_only"]


class DiagnosticResult(StrictModel):
    schema: Literal[1]
    operation: Literal["host.health","service.inventory","agent.snapshot"]
    observed_at: str = Field(pattern=r"^[0-9T:+.Z-]{20,40}$")
    result: HealthResult | list[Service] | SnapshotResult


class ReportRequest(ClaimRequest):
    task_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    lease_token: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    outcome: Literal["SUCCESS","FAILED"]
    result: DiagnosticResult | None = None


@router.post("/agent/report")
def report_task(payload: ReportRequest, request: Request, response: Response):
    host,secret=settings()
    if not hmac.compare_digest(request.headers.get("authorization","").encode(),("Bearer "+secret).encode()):
        raise HTTPException(401,"Agent authentication required")
    if payload.host_id != host:
        raise HTTPException(403,"Host identity mismatch")
    if (payload.outcome == "SUCCESS") != (payload.result is not None):
        raise HTTPException(422,"Invalid receipt")
    result=payload.result
    if result:
        expected={"host.health":HealthResult,"service.inventory":list,"agent.snapshot":SnapshotResult}
        if not isinstance(result.result,expected[result.operation]):
            raise HTTPException(422,"Invalid diagnostic result")
        services=result.result if isinstance(result.result,list) else result.result.services if isinstance(result.result,SnapshotResult) else []
        ids=[s.service_id for s in services]
        if len(ids)!=len(set(ids)) or not set(ids)<=SERVICES:
            raise HTTPException(422,"Invalid service result")
        health=result.result if isinstance(result.result,HealthResult) else result.result.health if isinstance(result.result,SnapshotResult) else None
        if health and health.disk and max(health.disk.used_bytes,health.disk.free_bytes)>health.disk.total_bytes:
            raise HTTPException(422,"Invalid disk result")
    response.headers["Cache-Control"]="no-store"
    try:
        created=task_store().report(host,payload.task_id,payload.lease_token,payload.outcome,result.model_dump() if result else None)
        return {"accepted":True,"created":created}
    except TaskConflict:
        raise HTTPException(409,"Invalid or expired lease") from None
    except ValueError:
        raise HTTPException(422,"Invalid receipt") from None
    except sqlite3.Error:
        raise HTTPException(503,"HostOps task storage unavailable") from None


@router.get("/tasks/{task_id}")
def task_detail(task_id: str, request: Request, response: Response, db: Session=Depends(get_db)):
    auth.authorize_session(request,db)
    host,_=settings()
    response.headers["Cache-Control"]="no-store"
    try:
        task=task_store().detail(host,task_id)
    except sqlite3.Error:
        raise HTTPException(503,"HostOps task storage unavailable") from None
    if task is None:
        raise HTTPException(404,"Task not found")
    return task


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, request: Request, response: Response, db: Session=Depends(get_db)):
    actor=auth.authorize_session(request,db)
    host,_=settings()
    response.headers["Cache-Control"]="no-store"
    try:
        task=task_store().cancel(host,task_id,actor)
    except TaskConflict:
        raise HTTPException(409,"Task already claimed or completed; cancellation is not guaranteed") from None
    except sqlite3.Error:
        raise HTTPException(503,"HostOps task storage unavailable") from None
    if task is None:
        raise HTTPException(404,"Task not found")
    return {"task":task}


@router.get("/audit")
def audit(request: Request, response: Response, after: int=0, db: Session=Depends(get_db)):
    auth.authorize_session(request,db)
    host,_=settings()
    if after<0 or after>9223372036854775807:
        raise HTTPException(422,"Invalid cursor")
    response.headers["Cache-Control"]="no-store"
    try:
        events=task_store().audit(host,after)
    except sqlite3.Error:
        raise HTTPException(503,"HostOps task storage unavailable") from None
    return {"events":events,"next_cursor":events[-1]["seq"] if events else after}


@router.get("/policy")
def policy(request: Request, response: Response, db: Session=Depends(get_db)):
    auth.authorize_session(request,db)
    host,_=settings()
    response.headers["Cache-Control"]="no-store"
    age,snapshot=host_evidence(host)
    active=bool(age is not None and age<=90 and snapshot and snapshot.get("dispatch_active"))
    return {"host_id":host,"policy_version":"readonly-v1","dispatch_configured":dispatch_ready(),
            "agent_dispatch_active":active,"operations":[{"operation":op,"risk":"L1","allowed":True}
            for op in sorted(OPERATIONS)],"service_mutation_allowed":False,
            "remote_upgrade_allowed":False,"arbitrary_shell_allowed":False}


@router.get("/modules")
def modules(request: Request, response: Response, db: Session=Depends(get_db)):
    auth.authorize_session(request,db)
    host,_=settings()
    response.headers["Cache-Control"]="no-store"
    target=os.getenv("HOSTOPS_EXPECTED_RUNTIME_DIGEST","")
    target=target if re.fullmatch(r"[0-9a-f]{64}",target) else None
    age,snapshot=host_evidence(host)
    observed=snapshot.get("runtime_digest") if snapshot else None
    fresh=age is not None and age<=90
    state=("unknown" if not fresh or not observed or not target else
           "matching" if observed==target else "different")
    return {"modules":[{"module_id":"hostops.agent","observed_digest":observed,
                        "expected_digest":target,"state":state,"fresh":fresh}],
            "remote_upgrade_allowed":False}


# Read-only external integration: independent credential and fixed tool schemas.
# This stateless MCP endpoint deliberately grants no execution authority.
MCP_TOOLS = [
    {"name":"hostops_get_health","description":"Read last authenticated host heartbeat; check state before interpreting telemetry.",
     "inputSchema":{"type":"object","properties":{},"additionalProperties":False},
     "annotations":{"readOnlyHint":True,"destructiveHint":False,"openWorldHint":False}},
    {"name":"hostops_list_services","description":"Read allowlisted services from the last heartbeat, not a live command.",
     "inputSchema":{"type":"object","properties":{},"additionalProperties":False},
     "annotations":{"readOnlyHint":True,"destructiveHint":False,"openWorldHint":False}},
    {"name":"hostops_get_module_versions","description":"Compare observed agent digest with configured target; freshness matters.",
     "inputSchema":{"type":"object","properties":{},"additionalProperties":False},
     "annotations":{"readOnlyHint":True,"destructiveHint":False,"openWorldHint":False}},
]


def integration_identity(request):
    host,agent_secret=settings()
    token=os.getenv("HOSTOPS_MCP_READ_TOKEN","")
    signing=os.getenv("HOSTOPS_JOB_SIGNING_KEY","")
    if (os.getenv("HOSTOPS_MCP_ENABLED")!="true" or not re.fullmatch(r"[A-Za-z0-9_-]{43,256}",token)
            or hmac.compare_digest(token,agent_secret) or hmac.compare_digest(token,signing)):
        raise HTTPException(503,"HostOps read-only integration not configured")
    # This bearer-only endpoint is not a browser cookie or agent authentication audience.
    if not hmac.compare_digest(request.headers.get("authorization","").encode(),("Bearer "+token).encode()):
        raise HTTPException(401,"Integration authentication required",headers={"WWW-Authenticate":"Bearer"})
    if request.headers.get("origin"):
        raise HTTPException(403,"Browser origin not allowed on integration endpoint")
    return host


class RpcRequest(StrictModel):
    jsonrpc: Literal["2.0"]
    id: str | int | None = None
    method: str = Field(max_length=80)
    params: dict = Field(default_factory=dict)


@router.post("/mcp")
def mcp_rpc(payload: RpcRequest, request: Request, response: Response):
    import json
    host=integration_identity(request)
    response.headers["Cache-Control"]="no-store"
    response.headers["X-Content-Type-Options"]="nosniff"
    def error(code,message):
        return {"jsonrpc":"2.0","id":payload.id,"error":{"code":code,"message":message}}
    def ok(result):
        return {"jsonrpc":"2.0","id":payload.id,"result":result}
    if payload.method=="notifications/initialized" and payload.id is None:
        return Response(status_code=202,headers={"Cache-Control":"no-store"})
    if payload.id is None:
        return error(-32600,"Request id required")
    if payload.method=="initialize":
        version=payload.params.get("protocolVersion")
        if version not in ("2025-03-26","2025-06-18"):
            version="2025-06-18"
        return ok({"protocolVersion":version,"capabilities":{"tools":{}},"serverInfo":{"name":"hostops-readonly","version":"0.3.0"}})
    if payload.method=="ping":
        return ok({})
    if payload.method=="tools/list":
        return ok({"tools":MCP_TOOLS})
    if payload.method!="tools/call":
        return error(-32601,"Method not supported")
    name=payload.params.get("name")
    arguments=payload.params.get("arguments",{})
    if type(name) is not str or name not in {t["name"] for t in MCP_TOOLS} or type(arguments) is not dict or arguments:
        return error(-32602,"Unknown tool or invalid arguments")
    age,snapshot=host_evidence(host)
    snapshot=snapshot or {}
    state="never_seen" if age is None else "online" if age<=90 else "stale"
    result={"host_id":host,"state":state,"age_seconds":age}
    if name=="hostops_get_health":
        result["health"]={k:snapshot.get(k) for k in ("system","architecture","disk")} if snapshot else None
    elif name=="hostops_list_services":
        result["services"]=snapshot.get("services",[])
    else:
        target=os.getenv("HOSTOPS_EXPECTED_RUNTIME_DIGEST","")
        target=target if re.fullmatch(r"[0-9a-f]{64}",target) else None
        observed=snapshot.get("runtime_digest")
        result.update(observed_digest=observed,expected_digest=target,
                      comparison="unknown" if state!="online" or not observed or not target else
                      "matching" if observed==target else "different")
    # Fail closed: do not return external telemetry without a durable audit record.
    try:
        event_id=task_store().integration_event(host,name,state)
    except (sqlite3.Error,TaskCapacity):
        raise HTTPException(503,"HostOps integration audit unavailable") from None
    result["audit_event_id"]=event_id
    return ok({"content":[{"type":"text","text":json.dumps(result,ensure_ascii=True)}]})


@router.get("/integration-audit")
def integration_audit(request: Request,response: Response,after: int=0,db: Session=Depends(get_db)):
    auth.authorize_session(request,db)
    host,_=settings()
    if after<0 or after>9223372036854775807:
        raise HTTPException(422,"Invalid cursor")
    response.headers["Cache-Control"]="no-store"
    try:
        events=task_store().integration_audit(host,after)
    except sqlite3.Error:
        raise HTTPException(503,"HostOps integration audit unavailable") from None
    return {"events":events,"next_cursor":events[-1]["seq"] if events else after}

