from fastapi.testclient import TestClient
import pytest

from app.main import app
from app import hostops, auth


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("HOSTOPS_ENABLED", "true")
    monkeypatch.setenv("HOSTOPS_HOST_ID", "macmini-01")
    monkeypatch.setenv("HOSTOPS_AGENT_TOKEN", "a" * 40)
    monkeypatch.setattr(hostops, "_latest", None)
    monkeypatch.delenv("HOSTOPS_STATE_DIR",raising=False)
    return TestClient(app)


def payload():
    return {"host_id": "macmini-01", "system": "Darwin", "architecture": "arm64",
            "disk": None, "services": [], "capabilities": ["host.health"]}


def test_agent_authentication_and_identity(client):
    url = "/api/hostops/v1/agent/heartbeat"
    assert client.post(url, json=payload()).status_code == 401
    headers = {"Authorization": "Bearer " + "a" * 40}
    assert client.post(url, json={**payload(), "host_id": "other"}, headers=headers).status_code == 403
    assert client.post(url, json={**payload(), "capabilities": ["shell"]}, headers=headers).status_code == 422
    assert client.post(url, json=payload(), headers=headers).status_code == 200


def test_admin_view_and_staleness(client, monkeypatch):
    monkeypatch.setattr(auth, "authorize_session", lambda *args: "admin")
    url = "/api/hostops/v1/hosts"
    assert client.get(url).json()["hosts"][0]["state"] == "never_seen"
    monkeypatch.setattr(hostops, "_clock", lambda: 1000)
    assert client.post("/api/hostops/v1/agent/heartbeat", json=payload(),
                       headers={"Authorization": "Bearer " + "a" * 40}).status_code == 200
    assert client.get(url).json()["hosts"][0]["state"] == "online"
    monkeypatch.setattr(hostops, "_clock", lambda: 1091)
    assert client.get(url).json()["hosts"][0]["state"] == "stale"


def test_disabled_gateway(client, monkeypatch):
    monkeypatch.delenv("HOSTOPS_ENABLED")
    assert client.post("/api/hostops/v1/agent/heartbeat", json=payload()).status_code == 503


def test_policy_and_module_evidence(client,monkeypatch):
    monkeypatch.setattr(auth,"authorize_session",lambda *args:"admin")
    monkeypatch.setenv("HOSTOPS_EXPECTED_RUNTIME_DIGEST","c"*64)
    monkeypatch.setenv("HOSTOPS_DISPATCH_ENABLED","true")
    monkeypatch.setenv("HOSTOPS_JOB_SIGNING_KEY","b"*64)
    monkeypatch.setattr(hostops,"_clock",lambda:1000)
    prefix="/api/hostops/v1"
    assert client.get(prefix+"/modules").json()["modules"][0]["state"]=="unknown"
    headers={"Authorization":"Bearer "+"a"*40}
    body={**payload(),"runtime_digest":"c"*64,"dispatch_active":True}
    assert client.post(prefix+"/agent/heartbeat",json=body,headers=headers).status_code==200
    assert client.get(prefix+"/modules").json()["modules"][0]["state"]=="matching"
    policy=client.get(prefix+"/policy").json()
    assert policy["agent_dispatch_active"] and policy["dispatch_configured"]
    assert not policy["service_mutation_allowed"] and not policy["remote_upgrade_allowed"]
    monkeypatch.setattr(hostops,"_clock",lambda:1091)
    assert client.get(prefix+"/modules").json()["modules"][0]["state"]=="unknown"
    assert not client.get(prefix+"/policy").json()["agent_dispatch_active"]


def test_mcp_readonly_audience_and_protocol(client,monkeypatch,tmp_path):
    import json
    tmp_path.chmod(0o700)
    monkeypatch.setenv("HOSTOPS_STATE_DIR",str(tmp_path))
    monkeypatch.setenv("HOSTOPS_MCP_ENABLED","true")
    monkeypatch.setenv("HOSTOPS_MCP_READ_TOKEN","m"*48)
    url="/api/hostops/v1/mcp"
    headers={"Authorization":"Bearer "+"m"*48}
    def rpc(method,params=None,**kwargs):
        return client.post(url,json={"jsonrpc":"2.0","id":1,"method":method,"params":params or {}},**kwargs)
    assert rpc("tools/list").status_code==401
    assert rpc("tools/list",headers={"Authorization":"Bearer "+"a"*40}).status_code==401
    assert rpc("tools/list",headers={**headers,"Origin":"https://other.example"}).status_code==403
    assert rpc("initialize",{"protocolVersion":"2025-06-18"},headers=headers).json()["result"]["protocolVersion"]=="2025-06-18"
    names={t["name"] for t in rpc("tools/list",headers=headers).json()["result"]["tools"]}
    assert names=={"hostops_get_health","hostops_list_services","hostops_get_module_versions"}
    assert rpc("tools/call",{"name":"hostops_submit_task"},headers=headers).json()["error"]["code"]==-32602
    assert rpc("tools/call",{"name":"hostops_get_health","arguments":{"command":"anything"}},headers=headers).json()["error"]["code"]==-32602
    data=rpc("tools/call",{"name":"hostops_get_health"},headers=headers).json()["result"]
    assert json.loads(data["content"][0]["text"])["state"]=="never_seen"
    assert client.post("/api/hostops/v1/agent/heartbeat",json=payload(),headers=headers).status_code==401
    assert client.get("/api/hostops/v1/tasks",headers=headers).status_code==401
    monkeypatch.setenv("HOSTOPS_MCP_READ_TOKEN","a"*40)
    assert rpc("tools/list",headers=headers).status_code==503


def test_persistent_snapshot_restart_and_clock_rollback(client,monkeypatch,tmp_path):
    tmp_path.chmod(0o700)
    monkeypatch.setenv("HOSTOPS_STATE_DIR",str(tmp_path))
    monkeypatch.setattr(auth,"authorize_session",lambda *args:"admin")
    monkeypatch.setattr(hostops,"_wall_clock",lambda:1000)
    prefix="/api/hostops/v1"
    assert client.post(prefix+"/agent/heartbeat",json=payload(),headers={"Authorization":"Bearer "+"a"*40}).status_code==200
    monkeypatch.setattr(hostops,"_latest",None)
    assert client.get(prefix+"/hosts").json()["hosts"][0]["state"]=="online"
    monkeypatch.setattr(hostops,"_wall_clock",lambda:1091)
    assert client.get(prefix+"/hosts").json()["hosts"][0]["state"]=="stale"
    monkeypatch.setattr(hostops,"_wall_clock",lambda:999)
    assert client.get(prefix+"/hosts").json()["hosts"][0]["state"]=="stale"


def test_mcp_audit_fail_closed_and_admin_visibility(client,monkeypatch,tmp_path):
    import json
    monkeypatch.setenv("HOSTOPS_MCP_ENABLED","true")
    monkeypatch.setenv("HOSTOPS_MCP_READ_TOKEN","m"*48)
    prefix="/api/hostops/v1"
    args={"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"hostops_get_health"}}
    headers={"Authorization":"Bearer "+"m"*48}
    assert client.post(prefix+"/mcp",json=args,headers=headers).status_code==503
    tmp_path.chmod(0o700)
    monkeypatch.setenv("HOSTOPS_STATE_DIR",str(tmp_path))
    result=client.post(prefix+"/mcp",json=args,headers=headers).json()["result"]
    audit_id=json.loads(result["content"][0]["text"])["audit_event_id"]
    assert client.get(prefix+"/integration-audit",headers=headers).status_code==401
    monkeypatch.setattr(auth,"authorize_session",lambda *args:"admin")
    event=client.get(prefix+"/integration-audit").json()["events"][0]
    assert event["seq"]==audit_id and event["actor"]=="mcp:readonly"
    assert event["operation"]=="hostops_get_health"
    assert "token" not in event
    assert client.get(prefix+"/integration-audit?after="+str(audit_id)).json()["events"]==[]
