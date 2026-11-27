import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.module_sdk import Module, mount_modules, require_module_session, table_name


def sample(module_id="notes", sdk_version=1):
    router=APIRouter()
    @router.get("/items")
    def items():
        return {"module":module_id}
    @router.post("/items")
    def create():
        return {"created":True}
    return Module(module_id,"1.0.0",sdk_version,router)


def test_namespaced_and_authenticated():
    app=FastAPI()
    mount_modules(app,[sample(),sample("orders")])
    client=TestClient(app)
    assert client.get("/api/modules/notes/items").status_code==401
    assert client.post("/api/modules/orders/items").status_code==401
    app.dependency_overrides[require_module_session]=lambda:"test-admin"
    assert client.get("/api/modules/notes/items").json()=={"module":"notes"}
    assert client.get("/api/modules/orders/items").json()=={"module":"orders"}
    assert client.get("/items").status_code==404


@pytest.mark.parametrize("modules",[[sample("core")],[sample(),sample()],[sample(sdk_version=2)]])
def test_invalid_batch_does_not_mount(modules):
    app=FastAPI()
    before=len(app.routes)
    with pytest.raises(ValueError):
        mount_modules(app,modules)
    assert len(app.routes)==before


def test_ambiguous_route_and_sealed_registry():
    app=FastAPI()
    module=sample()
    module.router.add_api_route("/{item}",lambda:None,methods=["GET"])
    with pytest.raises(ValueError):
        mount_modules(app,[module])
    mount_modules(app,[sample()])
    with pytest.raises(ValueError):
        mount_modules(app,[sample("orders")])


def test_table_namespace():
    assert table_name("notes","items")=="mod_notes__items"
    assert table_name("orders","items")!="mod_notes__items"
    with pytest.raises(ValueError):
        table_name("core","items")
    with pytest.raises(ValueError):
        table_name("notes","../items")
    with pytest.raises(ValueError):
        table_name("a"*39,"b"*39)
