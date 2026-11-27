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


def test_exact_dependency_contract(monkeypatch):
    from app import module_sdk as sdk
    monkeypatch.setattr(sdk,"installed_version",lambda name:"1.2.3")
    sdk.validate_dependencies((("example-lib","1.2.3"),))
    for invalid in ((("example-lib","2.0"),), (("example-lib",">=1"),),
                    (("example-lib","1.2.3"),("example_lib","1.2.3"))):
        with pytest.raises(ValueError):
            sdk.validate_dependencies(invalid)

def test_module_database_requires_explicit_distinct_role(monkeypatch):
    from app import module_sdk as sdk, db
    sdk._module_session_factory.cache_clear()
    monkeypatch.setattr(db,"DATABASE_URL","postgresql://core:secret@localhost/core")
    monkeypatch.delenv("APP_MODULE_NOTES_DATABASE_URL",raising=False)
    with pytest.raises(ValueError,match="not configured"):
        sdk._module_session_factory("notes")
    for url in ("sqlite:///test.db","postgresql://core:other@localhost/notes"):
        monkeypatch.setenv("APP_MODULE_NOTES_DATABASE_URL",url)
        with pytest.raises(ValueError,match="invalid module database"):
            sdk._module_session_factory("notes")
    calls=[]
    monkeypatch.setattr(sdk,"create_engine",lambda url,**kwargs:calls.append(url) or object())
    monkeypatch.setattr(sdk,"sessionmaker",lambda **kwargs:"module-session")
    monkeypatch.setenv("APP_MODULE_NOTES_DATABASE_URL","postgresql://notes:secret@localhost/notes")
    assert sdk._module_session_factory("notes")=="module-session"
    assert calls[0].username=="notes"
    sdk._module_session_factory.cache_clear()


def test_module_database_roles_cannot_be_shared(monkeypatch):
    from app import module_sdk as sdk, db
    monkeypatch.setattr(db,"DATABASE_URL","postgresql://core:secret@localhost/core")
    monkeypatch.setenv("APP_MODULE_NOTES_DATABASE_URL","postgresql://shared:secret@localhost/notes")
    monkeypatch.setenv("APP_MODULE_ORDERS_DATABASE_URL","postgresql://shared:other@localhost/orders")
    with pytest.raises(ValueError,match="invalid module database"):
        sdk._module_database_url("notes")
    monkeypatch.setenv("APP_MODULE_ORDERS_DATABASE_URL","postgresql://orders:other@localhost/orders")
    assert sdk._module_database_url("notes").username=="shared"
