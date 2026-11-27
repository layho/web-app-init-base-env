"""Trusted in-process application module SDK; not a security sandbox.

Only explicitly imported application code is registered. No manifest-controlled
dynamic imports, public registration endpoint or service-token bypass.
"""
from dataclasses import dataclass
import re

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from . import auth
from .db import get_db

SDK_VERSION = 1
_RESERVED = {"core", "foundation", "admin", "auth", "hostops", "system", "modules"}
_ID = re.compile(r"[a-z][a-z0-9_]{1,39}\Z")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
_SEGMENT = re.compile(r"(?:[a-zA-Z0-9_-]+|\{[a-zA-Z_][a-zA-Z0-9_]*\})\Z")


@dataclass(frozen=True)
class Module:
    id: str
    version: str
    sdk_version: int
    router: APIRouter


def require_module_session(request: Request, db: Session = Depends(get_db)):
    return auth.authorize_session(request, db)


def table_name(module_id: str, local_name: str) -> str:
    if not _ID.fullmatch(module_id) or module_id in _RESERVED or not _ID.fullmatch(local_name):
        raise ValueError("invalid module table identifier")
    name = f"mod_{module_id}__{local_name}"
    if len(name) > 63:
        raise ValueError("table identifier exceeds PostgreSQL limit")
    return name


def _route_shape(path):
    if not isinstance(path, str) or not path.startswith("/") or len(path)>240:
        raise ValueError("invalid module route")
    if path == "/":
        return "/"
    parts=path[1:].split("/")
    if not all(_SEGMENT.fullmatch(p) for p in parts):
        raise ValueError("invalid module route segment")
    return "/"+"/".join("{}" if p.startswith("{") else p for p in parts)


def _ambiguous(a,b):
    aa=a.split("/")
    bb=b.split("/")
    return len(aa)==len(bb) and all(x==y or x=="{}" or y=="{}" for x,y in zip(aa,bb))


def mount_modules(app: FastAPI, modules):
    """Validate complete batch before registering any route; fail closed."""
    if getattr(app.state,"module_sdk_registered",False):
        raise ValueError("module registration already sealed")
    modules=tuple(modules)
    if len(modules)>100:
        raise ValueError("too many modules")
    ids=set()
    planned=[]
    existing={getattr(route,"path","") for route in app.routes}
    for module in modules:
        if not isinstance(module,Module) or not isinstance(module.id,str) or not _ID.fullmatch(module.id) or module.id in _RESERVED or module.id in ids:
            raise ValueError("invalid or duplicate module ID")
        if type(module.sdk_version) is not int or module.sdk_version!=SDK_VERSION or not isinstance(module.version,str) or not _VERSION.fullmatch(module.version):
            raise ValueError("incompatible module version")
        if not isinstance(module.router,APIRouter) or not 1<=len(module.router.routes)<=100:
            raise ValueError("invalid module router")
        ids.add(module.id)
        prefix="/api/modules/"+module.id
        if any(path==prefix or path.startswith(prefix+"/") for path in existing):
            raise ValueError("module namespace already occupied")
        shapes=[]
        for route in module.router.routes:
            if not isinstance(route,APIRoute):
                raise ValueError("only HTTP API routes are supported")
            shape=_route_shape(route.path)
            methods=set(route.methods or ())
            if not methods or not methods <= {"GET","POST","PUT","PATCH","DELETE","HEAD"}:
                raise ValueError("unsupported module method")
            if any(methods & prior_methods and _ambiguous(shape,prior_shape) for prior_shape,prior_methods in shapes):
                raise ValueError("ambiguous module routes")
            shapes.append((shape,methods))
        planned.append((prefix,module))
    for prefix,module in planned:
        app.include_router(module.router,prefix=prefix,dependencies=[Depends(require_module_session)])
    app.state.module_sdk_registered=True
    app.state.module_sdk_inventory=tuple({"id":m.id,"version":m.version,"sdk_version":m.sdk_version} for _,m in planned)
    return app.state.module_sdk_inventory
