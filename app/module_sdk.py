"""Trusted in-process application module SDK; not a security sandbox.

Only explicitly imported application code is registered. No manifest-controlled
dynamic imports, public registration endpoint or service-token bypass.
"""
from dataclasses import dataclass
from importlib.metadata import version as installed_version, PackageNotFoundError
import os
import re
from functools import lru_cache
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

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
    dependencies: tuple[tuple[str, str], ...] = ()


def require_module_session(request: Request, db: Session = Depends(get_db)):
    return auth.authorize_session(request, db)


def table_name(module_id: str, local_name: str) -> str:
    if not isinstance(module_id,str) or not isinstance(local_name,str) or not _ID.fullmatch(module_id) or module_id in _RESERVED or not _ID.fullmatch(local_name):
        raise ValueError("invalid module table identifier")
    name = f"mod_{module_id}__{local_name}"
    if len(name) > 63:
        raise ValueError("table identifier exceeds PostgreSQL limit")
    return name


_DIST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\\Z")
_PIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]{0,79}\\Z")


def validate_dependencies(dependencies):
    """Exact installed distribution pins; no runtime package installation."""
    if not isinstance(dependencies,tuple) or len(dependencies)>50:
        raise ValueError("invalid module dependency contract")
    seen=set()
    for item in dependencies:
        if not isinstance(item,tuple) or len(item)!=2:
            raise ValueError("invalid dependency pin")
        name,pin=item
        if not isinstance(name,str) or not isinstance(pin,str) or not _DIST.fullmatch(name) or not _PIN.fullmatch(pin):
            raise ValueError("invalid dependency pin")
        canonical=re.sub(r"[-_.]+","-",name).lower()
        if canonical in seen:
            raise ValueError("duplicate module dependency")
        seen.add(canonical)
        try:
            actual=installed_version(name)
        except PackageNotFoundError:
            raise ValueError("required module dependency not installed") from None
        if actual!=pin:
            raise ValueError("module dependency version mismatch")


@lru_cache(maxsize=100)
def _module_session_factory(module_id):
    """Separate explicit PostgreSQL credentials; never fall back to Core."""
    from .db import DATABASE_URL
    table_name(module_id,"items")
    key="APP_MODULE_"+module_id.upper()+"_DATABASE_URL"
    raw=os.environ.get(key)
    if not raw:
        raise ValueError("module database not configured")
    try:
        url=make_url(raw)
        core=make_url(DATABASE_URL)
        if url.get_backend_name()!="postgresql" or not url.username or not url.password or not url.database:
            raise ValueError("invalid module database")
        # Separate role is mandatory even when using a separate database.
        # PostgreSQL ACL provisioning is external; URL comparison is not
        # itself proof of role privilege isolation.
        if url.username==core.username:
            raise ValueError("module database role must differ from Core")
        engine=create_engine(url,pool_pre_ping=True,hide_parameters=True)
    except Exception:
        raise ValueError("invalid module database configuration") from None
    return sessionmaker(bind=engine,expire_on_commit=False)


def module_db(module_id):
    """Return a FastAPI dependency bound to one explicitly configured module.

    Core and other module credentials are never used as fallback. Module code
    is trusted in-process; administrators must enforce PostgreSQL ACLs.
    """
    table_name(module_id,"items")
    def dependency():
        with _module_session_factory(module_id)() as session:
            yield session
    return dependency


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
        validate_dependencies(module.dependencies)
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
