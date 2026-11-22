from datetime import datetime, timezone
import os
from app import pr_tracking as p

H, B, M = "a"*40, "b"*40, "c"*40

def status(state, at, base=B, phase="passed", merge=M):
    return {"context": p.CONTEXT, "state": state, "created_at": at, "description": f"base={base} merge={merge} {phase}"}

def test_exact_base_and_attempt():
    statuses = [status("success", "2026-11-22T03:15:20Z"),
                status("pending", "2026-11-22T03:14:50Z", phase="pending", merge="unknown"),
                status("failure", "2026-11-22T03:10:00Z", phase="pytest-failed")]
    result = p.evidence(statuses, B)
    assert result["duration_seconds"] == 30 and result["state"] == "success"
    assert p.evidence(statuses, H)["state"] == "unknown"
    assert p.evidence(statuses[:1], B)["duration_seconds"] is None

def test_log_only_safe_exact_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "LOG_ROOT", tmp_path)
    path = tmp_path / f"layho--web-app-init-base-env-19-{H[:12]}-{B[:12]}.log"
    path.write_text("+ git init --bare repo\nsecret-example-never-return\n1 failed in 1.0s\n+ git init --bare repo\n45 passed, 2 skipped in 4.20s\nsecret-example-never-return")
    ts = datetime(2026,11,22,3,15,19,tzinfo=timezone.utc).timestamp()
    os.utime(path,(ts,ts))
    test = p.evidence([status("success","2026-11-22T03:15:20Z"), status("pending","2026-11-22T03:14:50Z",phase="pending",merge="unknown")],B)
    result = p.log_summary(19,H,B,test)
    assert result["pytest"] == {"passed":45,"skipped":2}
    assert "secret-example" not in str(result)
    assert p.log_summary(20,H,B,test) is None
    os.utime(path,(ts-1000,ts-1000))
    assert p.log_summary(19,H,B,test) is None
    path.unlink()
    other=tmp_path/"different"
    other.write_text("secret")
    path.symlink_to(other)
    assert p.log_summary(19,H,B,test) is None
