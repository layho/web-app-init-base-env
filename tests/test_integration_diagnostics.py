import sqlite3
from app.integration_diagnostics import read_queue
from app.integration_queue import IntegrationQueue

def test_missing_does_not_create_database(tmp_path):
    p=tmp_path/"queue.sqlite3"
    assert read_queue("")["state"]=="not_configured"
    assert read_queue(str(p))["state"]=="unavailable"
    assert not p.exists()

def test_real_queue_read_only_and_redacted(tmp_path):
    tmp_path.chmod(0o700)
    p=tmp_path/"queue.sqlite3"
    q=IntegrationQueue(p)
    q.activate("0.5.0")
    q.register("task-1","0.5.0","a"*40,[],["private/path.py"],now=1)
    claim=q.claim("0.5.0","b"*40,ttl=15,now=2)
    q.claim("0.5.0","b"*40,now=17)
    q.reconcile_unknown("task-1","a"*40,"b"*40,evidence_id="private-receipt",
                        verify_stopped=lambda *a:True,now=18)
    before=p.read_bytes()
    result=read_queue(str(p))
    assert result["state"]=="available"
    assert result["jobs"][0]["state"]=="QUEUED"
    assert result["events"][0]["event"]=="RECONCILED"
    assert claim["lease"] not in str(result)
    assert "private/path" not in str(result) and "private-receipt" not in str(result)
    assert p.read_bytes()==before

def test_unsafe_file_and_invalid_schema(tmp_path):
    tmp_path.chmod(0o700)
    p=tmp_path/"queue.sqlite3"
    with sqlite3.connect(p) as db:
        db.execute("CREATE TABLE unrelated(id INTEGER)")
    p.chmod(0o600)
    assert read_queue(str(p))["state"]=="unavailable"
    link=tmp_path/"link"
    link.symlink_to(p)
    assert read_queue(str(link))["state"]=="unavailable"
    p.chmod(0o644)
    assert read_queue(str(p))["state"]=="unavailable"
