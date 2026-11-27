import concurrent.futures
import pytest
from app.integration_queue import IntegrationQueue, QueueConflict

HEAD="a"*40
BASE="b"*40

@pytest.fixture
def queue(tmp_path):
    tmp_path.chmod(0o700)
    q=IntegrationQueue(tmp_path/"queue.sqlite3")
    q.activate("0.5.0")
    return q

def test_version_dependency_and_idempotency(queue):
    assert queue.register("one","0.5.0",HEAD,[],["app/core"],now=1)
    assert not queue.register("one","0.5.0",HEAD,[],["app/core"],now=1)
    with pytest.raises(QueueConflict):
        queue.register("one","0.5.0",BASE,[],["app/core"])
    with pytest.raises(QueueConflict):
        queue.register("two","0.5.0",HEAD,["missing"],["app/other"])
    queue.register("two","0.5.0",HEAD,["one"],["app/other"],priority=100)
    with pytest.raises(QueueConflict):
        queue.activate("0.6.0")
    assert queue.claim("0.5.0",BASE,now=10)["id"]=="one"

def test_atomic_claim_and_expiration(queue):
    queue.register("one","0.5.0",HEAD,[],["app/core"])
    queue.register("two","0.5.0",HEAD,[],["app/other"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:queue.claim("0.5.0",BASE,ttl=15,now=100),range(4)))
    assert sum(x is not None for x in results)==1
    assert queue.claim("0.5.0",BASE,now=115) is None
    assert any(x["state"]=="UNKNOWN" for x in queue.snapshot("0.5.0"))
    assert queue.claim("0.5.0",BASE,now=200) is None

def test_reservation_and_exact_base(queue):
    queue.register("one","0.5.0",HEAD,[],["App/Core"])
    assert queue.reservation_conflicts("0.5.0",["app/core/file.py"])==["one"]
    assert queue.reservation_conflicts("0.5.0",["app/core2"])==[]
    claim=queue.claim("0.5.0",BASE,now=100)
    with pytest.raises(QueueConflict):
        queue.finish("one",claim["lease"],HEAD,["app/core/file.py"],now=101)
    with pytest.raises(QueueConflict):
        queue.finish("one",claim["lease"],BASE,["app/other.py"],now=101)
    queue.finish("one",claim["lease"],BASE,["app/core/file.py"],now=101)
    assert queue.snapshot("0.5.0")[0]["state"]=="PREPARED"
    assert "lease" not in queue.snapshot("0.5.0")[0]

@pytest.mark.parametrize("path",["/absolute","app/../x","app//x","app\\x",".","app/\nfile"])
def test_invalid_path(queue,path):
    with pytest.raises(ValueError):
        queue.register("one","0.5.0",HEAD,[],[path])

def test_reservation_enforced_and_prepared_blocks(queue):
    queue.register("one","0.5.0",HEAD,[],["app/core"])
    with pytest.raises(QueueConflict):
        queue.register("overlap","0.5.0",HEAD,[],["APP/core/sub.py"])
    queue.register("other","0.5.0",HEAD,[],["app/other"])
    c=queue.claim("0.5.0",BASE,now=100)
    queue.finish(c["id"],c["lease"],BASE,c["files"],now=101)
    assert queue.claim("0.5.0",BASE,now=102) is None
    queue.retry_preparation(c["id"],HEAD,BASE,now=103)
    assert queue.claim("0.5.0",BASE,now=104) is not None

def test_renewal_and_audit(queue):
    queue.register("one","0.5.0",HEAD,[],["app/core"])
    c=queue.claim("0.5.0",BASE,ttl=15,now=100)
    assert queue.renew("one",c["lease"],BASE,ttl=30,now=110)==140
    with pytest.raises(QueueConflict):
        queue.renew("one",c["lease"],BASE,now=140)
    assert queue.claim("0.5.0",BASE,now=140) is None
    events=queue.events("0.5.0")
    assert [x["event"] for x in events]==["QUEUED","CLAIMED","RENEWED","LEASE_EXPIRED"]
    assert queue.events("0.5.0",events[-1]["seq"])==[]

def test_private_storage(tmp_path):
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError):
        IntegrationQueue(tmp_path/"queue.sqlite3")
    tmp_path.chmod(0o700)
    p=tmp_path/"target"
    p.touch(mode=0o600)
    link=tmp_path/"link"
    link.symlink_to(p)
    with pytest.raises(OSError):
        IntegrationQueue(link)

def test_unknown_reconciliation_requires_fencing_and_fresh_evidence(queue):
    queue.register("one","0.5.0",HEAD,[],["app/core"])
    old=queue.claim("0.5.0",BASE,ttl=15,now=100)
    assert queue.claim("0.5.0",BASE,now=115) is None
    with pytest.raises(QueueConflict):
        queue.reconcile_unknown("one",HEAD,BASE,evidence_id="receipt-1",verify_stopped=lambda *a:False,now=116)
    assert queue.snapshot("0.5.0")[0]["state"]=="UNKNOWN"
    calls=[]
    def verified(*args):
        calls.append(args)
        return True
    queue.reconcile_unknown("one",HEAD,BASE,evidence_id="receipt-1",verify_stopped=verified,now=117)
    assert calls[0][:3]==("one",HEAD,BASE)
    fresh=queue.claim("0.5.0",BASE,ttl=15,now=118)
    assert fresh["lease"]!=old["lease"]
    with pytest.raises(QueueConflict):
        queue.finish("one",old["lease"],BASE,["app/core"],now=119)
    assert queue.claim("0.5.0",BASE,now=133) is None
    with pytest.raises(QueueConflict):
        queue.reconcile_unknown("one",HEAD,BASE,evidence_id="receipt-1",verify_stopped=verified,now=134)
    assert len(calls)==1
    assert queue.snapshot("0.5.0")[0]["state"]=="UNKNOWN"

def test_unknown_reconciliation_rejects_stale_candidate(queue):
    queue.register("one","0.5.0",HEAD,[],["app/core"])
    queue.claim("0.5.0",BASE,ttl=15,now=100)
    queue.claim("0.5.0",BASE,now=115)
    with pytest.raises(QueueConflict):
        queue.reconcile_unknown("one",BASE,BASE,evidence_id="receipt-2",verify_stopped=lambda *a:True)

def test_integration_requires_independent_observation(queue):
    queue.register("one","0.5.0",HEAD,[],["app/core"])
    queue.register("two","0.5.0",HEAD,["one"],["app/other"])
    c=queue.claim("0.5.0",BASE,now=100)
    queue.finish("one",c["lease"],BASE,["app/core"],now=101)
    with pytest.raises(QueueConflict):
        queue.record_integrated("one",HEAD,BASE,candidate="c"*40,verify=lambda *a:False,now=102)
    assert queue.claim("0.5.0",BASE,now=103) is None
    queue.record_integrated("one",HEAD,BASE,candidate="c"*40,verify=lambda *a:True,now=104)
    assert queue.claim("0.5.0","c"*40,now=105)["id"]=="two"


def test_actual_preview_journal_consumes_reservation_atomically(queue):
    from deploy.macci.preview_journal import PreviewJournal
    queue.register("pr-196","0.5.0",HEAD,[],["app/core"])
    c=queue.claim("0.5.0",BASE,now=100)
    queue.finish(c["id"],c["lease"],BASE,["app/core"],now=101)
    journal=PreviewJournal(queue.db_path)
    with pytest.raises(ValueError):
        journal.prepare(197,HEAD,BASE,"c"*40,changed_files=["app/core"])
    with pytest.raises(ValueError):
        journal.prepare(196,HEAD,BASE,"c"*40,changed_files=["app/other"])
    assert queue.snapshot("0.5.0")[0]["state"]=="PREPARED"
    journal.prepare(196,HEAD,BASE,"c"*40,changed_files=["app/core"])
    assert queue.snapshot("0.5.0")[0]["state"]=="UNKNOWN"
    assert not journal.reconcile(lambda:BASE)
    assert queue.claim("0.5.0",BASE) is None
    assert journal.reconcile(lambda:"c"*40)
    assert queue.snapshot("0.5.0")[0]["state"]=="INTEGRATED"
    assert journal.history()[0]["state"]=="INTEGRATED"
