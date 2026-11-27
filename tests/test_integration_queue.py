import concurrent.futures
import pytest
from app.integration_queue import IntegrationQueue, QueueConflict

HEAD="a"*40
BASE="b"*40

@pytest.fixture
def queue(tmp_path):
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
