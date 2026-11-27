"""Isolated V0.6.0 W01 event engine. These tests drive the store, not the contract strings."""

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.control_authz import AuthnError, AuthzError, AuthzService
from app.control_authz.service import POLICY_REVISION, isolated_registry
from app.control_events.engine import Engine
from app.control_events.errors import BadRecord, ControlError, DeliveryUnknown
from app.control_events.identity import bound_token_for_directory
from app.control_events.legacy import KNOWN_AGENTS
from app.control_events.outbox import FAILURE_PHASES, OutboxLimits
from app.control_routes_w01 import bind_isolated_engine, clear_binding, router, set_isolated
from app.feedback_progress import STATUSES

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40
SECRET = "github_pat_SHOULDNOTLEAK"


def engine(tmp_path, token="generation-1", **kwargs):
    return Engine.open(tmp_path / "store", token, **kwargs)


def actor():
    return {"kind": "test", "id": "fixture", "proof_ref": "test-proof://w01/fixture"}


def body(eng, **fields):
    request = {
        "schema_version": 2,
        "actor": actor(),
        "scope": {"repo": "layho/web-app-init-base-env", "task_id": "tsk_w01"},
        "expected_revision": 1,
        "idempotency_key": "idem-w01",
        "repo_id": eng.repo_id,
    }
    request.update(fields)
    request["credential"] = eng.credential_for(request.get("producer_id", "fixture"))
    return request


def confirm(_channel=None, _payload=None):
    return {"confirmed": True}


def _bind(eng, payload):
    payload["credential"] = eng.credential_for(payload.get("producer_id", "fixture"))
    return payload


def task(eng, **fields):
    payload = body(
        eng,
        event_id="evt_task",
        event_type="run.started",
        event_class="task",
        producer_id="fixture",
        stream_id="fixture:task",
        stream_epoch=1,
        producer_seq=1,
        occurred_at="2026-11-27T10:00:00Z",
        app_id="base",
        host_id=None,
        task_id="tsk_w01",
        run_id="run_1",
        attempt=1,
        revision=1,
        stage="develop",
        state="PENDING",
        candidate_id="cand_w01",
    )
    payload.update(fields)
    return _bind(eng, payload)


def deployment(eng, **fields):
    payload = body(
        eng,
        event_id="evt_dep",
        event_type="deployment.commit",
        event_class="deployment",
        producer_id="host_deployer",
        stream_id="host_deployer:deployment",
        stream_epoch=1,
        producer_seq=1,
        occurred_at="2026-11-27T10:00:00Z",
        deployment_id="dep_1",
        app_id="base",
        host_id="host-test",
        candidate_id="cand_w01",
        head=SHA,
        base=SHA_B,
        merge=SHA_C,
        tree=SHA_D,
        running_sha=SHA,
        artifact_digest="sha256:" + "e" * 64,
        previous_healthy_sha=None,
        install_kind="first_install",
        stage="PLAN",
        state="PLAN",
        revision=1,
        trusted_source="host_deployer",
        idempotency_key="idem-dep",
    )
    payload.update(fields)
    return _bind(eng, payload)


def authorization(eng, **fields):
    payload = body(
        eng,
        event_id="evt_auth",
        event_type="authorization.granted",
        event_class="authorization",
        producer_id="policy_service",
        stream_id="policy_service:authorization",
        stream_epoch=1,
        producer_seq=1,
        occurred_at="2026-11-27T10:00:00Z",
        task_id="tsk_w01",
        candidate_id="cand_w01",
        head=SHA,
        base=SHA_B,
        merge=SHA_C,
        tree=SHA_D,
        operation="preview.merge",
        policy_revision=1,
        revision=1,
        stage="integrate",
        state="RECORDED",
        evidence_ref="policy://1",
        trusted_source="policy_service",
        idempotency_key="idem-auth",
    )
    payload.update(fields)
    return _bind(eng, payload)


def diagnostic(eng, **fields):
    payload = body(
        eng,
        event_id="evt_diag",
        event_type="diagnostic.observation",
        event_class="diagnostic",
        producer_id="hostops",
        stream_id="hostops:diagnostic",
        stream_epoch=1,
        producer_seq=1,
        occurred_at="2026-11-27T10:00:00Z",
        diagnostic_class="queue",
        subject_id="tsk_w01",
        severity="info",
        trusted_source="hostops_readonly",
        idempotency_key="idem-diag",
    )
    payload.update(fields)
    return _bind(eng, payload)


def report(run_id, *, outcome, phase, note=None, **fields):
    payload = {
        "repository": "layho/web-app-init-base-env",
        "pr": 206,
        "run_id": run_id,
        "head": SHA,
        "base": SHA_B,
        "merge": SHA_C,
        "phase": phase,
        "outcome": outcome,
        "tree": SHA_D,
        "executor_id": "macci-isolated",
        "evidence_ref": "evidence://macci/isolated",
    }
    if note:
        payload["note"] = note
    payload.update(fields)
    return payload


def test_repo_identity_is_stable_and_not_reusable_after_rebuild(tmp_path):
    first = engine(tmp_path, "generation-1")
    again = Engine.open(tmp_path / "store", "generation-1")
    assert again.repo_id == first.repo_id
    stored = json.loads((tmp_path / "store" / "repo-identity.json").read_text(encoding="utf-8"))
    with pytest.raises(ControlError) as rejected:
        Engine.open(tmp_path / "store", "generation-2")
    assert rejected.value.code == "CANDIDATE_STALE"
    assert "repo_" not in rejected.value.message
    assert json.loads((tmp_path / "store" / "repo-identity.json").read_text(encoding="utf-8"))["repo_id"] == stored["repo_id"]
    rebuilt = engine(tmp_path / "rebuilt", "generation-2")
    assert rebuilt.repo_id != first.repo_id
    copied = tmp_path / "copied"
    copied.mkdir()
    (copied / "repo-identity.json").write_text((tmp_path / "store" / "repo-identity.json").read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ControlError):
        Engine.open(copied, "generation-2")
    tampered = json.loads((tmp_path / "store" / "repo-identity.json").read_text(encoding="utf-8"))
    tampered["bound_token"] = "generation-9"
    (tmp_path / "store" / "repo-identity.json").write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ControlError) as sealed:
        Engine.open(tmp_path / "store", "generation-9")
    assert sealed.value.code == "CANDIDATE_STALE"
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    assert bound_token_for_directory(left) != bound_token_for_directory(right)


def test_schema_trust_and_identity_do_not_write_idempotency(tmp_path):
    eng = engine(tmp_path)
    missing = task(eng)
    missing.pop("run_id")
    stamped = task(eng, received_at="2026-11-27T10:00:01Z")
    foreign = task(eng, repo_id="layho/web-app-init-base-env")
    untrusted = deployment(eng, trusted_source="diagnostic")
    unknown = task(eng, state="UNKNOWN")
    upgrade = deployment(eng, install_kind="upgrade", previous_healthy_sha=None, event_id="evt_up", idempotency_key="idem-up")
    denied = engine(tmp_path / "deny", authorizer=lambda _event: False)
    for sample in (missing, stamped, foreign, untrusted, unknown, upgrade):
        before = eng.store.count("idempotency")
        with pytest.raises(ControlError):
            eng.append(sample)
        assert eng.store.count("idempotency") == before
        assert eng.store.count("events") == 0
    with pytest.raises(ControlError) as policy:
        denied.append(task(denied))
    assert policy.value.code == "POLICY_DENIED"
    assert denied.store.count("idempotency") == 0
    with pytest.raises(ControlError):
        eng.enable_production_protocol()
    assert eng.production_protocol_enabled is False


def test_reducer_rejects_illegal_transition_and_keeps_current_round(tmp_path):
    eng = engine(tmp_path)
    first = eng.append(task(eng, state="PENDING", run_id="run_old", progress_status="submitted", attempt=2))
    assert first["applied"] is True
    assert first["fact_state"] == "ACCEPTED"
    assert first["delivery_disposition"] == "not_delivered"
    illegal = eng.append(task(
        eng, event_id="evt_back", idempotency_key="idem-back", producer_seq=2, run_id="run_old",
        state="RETRY_WAIT", expected_revision=1, revision=2, progress_status="claimed",
    ))
    assert illegal["code"] == "ILLEGAL_TRANSITION"
    assert eng.fact("task:run_old")["state"] == "PENDING"
    assert eng.store.count("events") == 1
    current = eng.append(task(
        eng, event_id="evt_current", idempotency_key="idem-current", producer_id="fixture_b",
        stream_id="fixture_b:task", producer_seq=1, run_id="run_current", attempt=3,
        state="PENDING", progress_status="submitted",
    ))
    assert current["applied"] is True
    stale = eng.append(task(
        eng, event_id="evt_stale", idempotency_key="idem-stale", producer_id="fixture_c",
        stream_id="fixture_c:task", producer_seq=1, run_id="run_stale", attempt=1,
        state="PENDING", progress_status="claimed",
    ))
    assert stale["applied"] is True
    regress = eng.append(task(
        eng, event_id="evt_regress", idempotency_key="idem-regress", producer_id="fixture_b",
        stream_id="fixture_b:task", producer_seq=2, run_id="run_current", attempt=3,
        state="PENDING", expected_revision=1, revision=2, progress_status="claimed", guard="renew",
    ))
    assert regress["applied"] is True
    rounds = {row["run_id"]: row for row in eng.current_rounds()}
    assert rounds["run_current"]["status"] == "submitted"
    assert rounds["run_current"]["attempt"] == 3
    assert "development_submitted_at" in rounds["run_current"]["stage_times"]
    assert eng.fact("task:run_current")["state"] == "PENDING"
    assert eng.fact("task:run_stale")["state"] == "PENDING"


def test_revision_authority_across_producers(tmp_path):
    eng = engine(tmp_path)
    first = eng.append(deployment(eng, revision=4, expected_revision=4))
    assert first["applied"] is True
    side = eng.append(deployment(
        eng, event_id="evt_side", idempotency_key="idem-side", producer_seq=2,
        revision=4, expected_revision=4, running_sha=SHA_B,
    ))
    assert side["code"] == "CONFLICT_REVISION"
    assert eng.fact("deployment:dep_1")["event_id"] == "evt_dep"
    assert eng.fact("deployment:dep_1")["authoritative"] is True
    observed = eng.append(diagnostic(
        eng, event_id="evt_obs", idempotency_key="idem-obs", revision=9, expected_revision=1,
        subject_id="dep_1", observed_object_class="deployment", observed_object_id="dep_1",
    ))
    assert observed["applied"] is True
    assert eng.fact("deployment:dep_1")["revision"] == 4
    replaced = eng.append(deployment(
        eng, event_id="evt_dep_5", idempotency_key="idem-dep-5", producer_seq=2,
        revision=5, expected_revision=4,
    ))
    assert replaced["applied"] is True
    assert eng.fact("deployment:dep_1")["revision"] == 5
    assert eng.fact("deployment:dep_1")["event_id"] == "evt_dep_5"
    conflict = eng.append(deployment(
        eng, event_id="evt_conflict", idempotency_key="idem-conflict", producer_id="other_deployer",
        stream_id="other_deployer:deployment", producer_seq=1, revision=6, expected_revision=4,
    ))
    assert conflict["code"] == "CONFLICT_REVISION"
    assert eng.fact("deployment:dep_1")["revision"] == 5
    assert eng.store.count("events") == 3


def test_authorization_epoch_gaps_do_not_skip_and_diagnostics_stay_independent(tmp_path):
    eng = engine(tmp_path)
    diag = eng.append(diagnostic(eng))
    assert diag["applied"] is True
    held = eng.append(authorization(
        eng, event_id="auth_2", idempotency_key="idem-auth-2", producer_seq=2,
        event_type="authorization.granted", trusted_source=None, revision=2, expected_revision=1,
    ))
    assert held["applied"] is False
    assert held["hold_reason"] == "GAP"
    never = eng.append(authorization(
        eng, event_id="auth_missing", idempotency_key="idem-never", producer_seq=1,
        classification="never_existed",
    ))
    assert never["code"] == "EVIDENCE_MISSING"
    assert eng.watermarks().get("policy_service:authorization@1", {"seq": 0})["seq"] == 0
    assert eng.watermarks()["hostops:diagnostic@1"]["seq"] == 1
    assert eng.authorizations() == []
    later_epoch = eng.append(authorization(
        eng, event_id="auth_epoch2", idempotency_key="idem-epoch2", stream_epoch=2, producer_seq=1,
        event_type="authorization.granted", trusted_source=None,
        revision=3, expected_revision=2,
    ))
    assert later_epoch["applied"] is False
    assert later_epoch["hold_reason"] == "EPOCH_GATE"
    auth_query = eng.query(credential=eng.credential_for("policy_service"), stream_id="policy_service:authorization", stream_epoch=1, after_seq=0)
    diag_query = eng.query(credential=eng.credential_for("hostops"), stream_id="hostops:diagnostic", stream_epoch=1, after_seq=0)
    assert auth_query["gap"] is True and auth_query["watermark_seq"] == 0
    assert diag_query["gap"] is False and diag_query["watermark_seq"] == 1 and diag_query["side_effect"] is False
    filled = eng.append(authorization(eng, event_id="auth_1", idempotency_key="idem-auth-1"))
    assert filled["applied"] is True
    assert eng.watermarks()["policy_service:authorization@1"]["seq"] == 2
    assert eng.watermarks()["policy_service:authorization@1"]["closed"] is False
    assert eng.query(credential=eng.credential_for("policy_service"), stream_id="policy_service:authorization", stream_epoch=2, after_seq=0)["watermark_seq"] == 0
    closed = eng.append(authorization(
        eng, event_id="auth_close", idempotency_key="idem-close", producer_seq=3,
        event_type="authorization.epoch_closed", trusted_source=None, revision=3, expected_revision=2,
        epoch_closed=True, closed_through_seq=3,
    ))
    assert closed["applied"] is True
    assert eng.watermarks()["policy_service:authorization@1"]["closed"] is True
    assert eng.watermarks()["policy_service:authorization@2"]["seq"] == 1
    grants = eng.authorizations()
    assert len(grants) == 1 and grants[0]["source"] == "isolated" and grants[0]["production_effective"] is False
    skipping = eng.append(authorization(
        eng, event_id="auth_skip", idempotency_key="idem-skip", producer_id="policy_service_b",
        stream_id="policy_service_b:authorization", producer_seq=2,
        event_type="authorization.granted", trusted_source=None,
    ))
    assert skipping["applied"] is False
    close_over_gap = eng.append(authorization(
        eng, event_id="auth_false_close", idempotency_key="idem-false-close",
        producer_id="policy_service_b", stream_id="policy_service_b:authorization", producer_seq=1,
        event_type="authorization.epoch_closed", trusted_source=None,
        epoch_closed=True, closed_through_seq=1,
    ))
    assert close_over_gap["code"] == "EVIDENCE_MISSING"
    assert eng.watermarks().get("policy_service_b:authorization@1", {"seq": 0})["seq"] == 0


def test_counter_loss_seals_old_epoch_without_filling_the_gap(tmp_path):
    eng = engine(tmp_path)
    eng.append(authorization(
        eng, event_id="gap_2", idempotency_key="idem-gap-2", producer_seq=2,
        event_type="authorization.granted", trusted_source=None, revision=2,
    ))
    loss = eng.declare_counter_loss("policy_service:authorization")
    assert loss["event_written"] is False and loss["next_epoch"] == 2
    restart = eng.append(authorization(
        eng, event_id="restart_1", idempotency_key="idem-restart", producer_seq=1,
        event_type="authorization.granted", trusted_source=None,
    ))
    assert restart["code"] == "EVIDENCE_MISSING"
    new_epoch = eng.append(authorization(
        eng, event_id="new_epoch", idempotency_key="idem-new-epoch", stream_epoch=2, producer_seq=1,
        event_type="authorization.granted", trusted_source=None, revision=2,
    ))
    assert new_epoch["applied"] is False and new_epoch["hold_reason"] == "EPOCH_GATE"
    false_close = eng.append(authorization(
        eng, event_id="false_close", idempotency_key="idem-false", producer_seq=1,
        event_type="authorization.epoch_closed", trusted_source=None,
        epoch_closed=True, closed_through_seq=1,
    ))
    assert false_close["code"] == "EVIDENCE_MISSING"
    assert eng.watermarks().get("policy_service:authorization@1", {"seq": 0})["seq"] == 0
    eng.append(diagnostic(eng))
    eng.declare_counter_loss("hostops:diagnostic")
    moved = eng.append(diagnostic(
        eng, event_id="diag_epoch2", idempotency_key="idem-diag-2", stream_epoch=2, producer_seq=1,
    ))
    assert moved["applied"] is True
    assert eng.watermarks()["hostops:diagnostic@2"]["seq"] == 1
    reopened = Engine.open(tmp_path / "store", "generation-1")
    assert reopened.watermarks()["hostops:diagnostic@1"]["seq"] == 1
    assert reopened.watermarks().get("policy_service:authorization@2", {"seq": 0})["seq"] == 0


def test_diagnostic_tombstone_fills_only_its_stream(tmp_path):
    eng = engine(tmp_path)
    eng.append(diagnostic(eng, event_id="diag_2", idempotency_key="idem-d2", producer_seq=2))
    tomb = eng.append(diagnostic(
        eng, event_id="diag_tomb", idempotency_key="idem-tomb", producer_seq=1, classification="never_existed",
    ))
    assert tomb["applied"] is True
    assert eng.watermarks()["hostops:diagnostic@1"]["seq"] == 2
    eng.append(authorization(
        eng, event_id="auth_gap", idempotency_key="idem-agap", producer_seq=2,
        event_type="authorization.granted", trusted_source=None,
    ))
    assert eng.watermarks().get("policy_service:authorization@1", {"seq": 0})["seq"] == 0
    assert eng.authorizations() == []


def test_diagnostic_cursors_are_keyed_by_stream_and_epoch(tmp_path):
    eng = engine(tmp_path)
    eng.append(diagnostic(eng, subject_id="subj"))
    eng.append(diagnostic(
        eng, event_id="evt_b", idempotency_key="idem-b", producer_id="hostops_b",
        stream_id="hostops_b:diagnostic", subject_id="subj",
    ))
    host = eng.credential_for("hostops")
    other_cred = eng.credential_for("hostops_b")
    first = eng.poll_diagnostics(credential=host, subject="subj", stream_id="hostops:diagnostic", stream_epoch=1, after_seq=1)
    assert first["side_effect"] is False and first["cursor_persisted"] is False and first["gap"] is False
    assert len(first["items"]) == 0
    assert eng.cursor("hostops:diagnostic", 1) is None
    other = eng.poll_diagnostics(credential=other_cred, subject="subj", stream_id="hostops_b:diagnostic", stream_epoch=1, after_seq=0)
    assert len(other["items"]) == 1 and other["side_effect"] is False and eng.cursor("hostops_b:diagnostic", 1) is None
    saved = eng.commit_cursor(credential=host, subject="subj", stream_id="hostops:diagnostic", stream_epoch=1, after_seq=1)
    assert saved["side_effect"] is True and saved["cursor_after_seq"] == 1
    other_saved = eng.commit_cursor(credential=other_cred, subject="subj", stream_id="hostops_b:diagnostic", stream_epoch=1, after_seq=0)
    assert other_saved["cursor_after_seq"] == 0
    assert eng.cursor("hostops:diagnostic", 1, saved["principal_id"]) == 1
    rewind = eng.poll_diagnostics(credential=host, subject="subj", stream_id="hostops:diagnostic", stream_epoch=1, after_seq=0)
    assert len(rewind["items"]) == 1 and rewind["side_effect"] is False
    assert eng.cursor("hostops:diagnostic", 1, saved["principal_id"]) == 1
    skipped = eng.poll_diagnostics(credential=host, subject="subj", stream_id="hostops:diagnostic", stream_epoch=1, after_seq=5)
    assert skipped["gap"] is True and eng.cursor("hostops:diagnostic", 1, saved["principal_id"]) == 1
    with pytest.raises(ControlError) as ignored:
        eng.poll_diagnostics(credential=host, subject="subj", stream_id="policy_service:authorization", stream_epoch=1, after_seq=0)
    assert ignored.value.code == "NOT_AUTHORIZED"
    assert eng.cursor("policy_service:authorization", 1) is None
    assert eng.cursor("hostops:diagnostic", 2) is None
    reopened = Engine.open(tmp_path / "store", "generation-1")
    assert reopened.cursor("hostops:diagnostic", 1) == 1
    assert reopened.cursor("hostops_b:diagnostic", 1) == 0
    assert reopened.store.count("events") == eng.store.count("events")


def test_idempotency_replay_and_concurrency(tmp_path):
    eng = engine(tmp_path)
    payload = task(eng, note=SECRET)
    first = eng.append(payload)
    assert first["applied"] is True and first["lease_token"] is None
    assert SECRET not in json.dumps(first)
    replay = eng.append(payload)
    assert replay["code"] == "IDEMPOTENCY_REPLAY"
    assert replay["replayed"] is True and replay["lease_token"] is None
    assert replay["original_result"]["applied"] is True
    assert SECRET not in json.dumps(replay)
    assert eng.store.count("events") == 1
    mismatch = eng.append(task(eng, event_id="evt_other", note="different"))
    assert mismatch["code"] == "IDEMPOTENCY_PAYLOAD_MISMATCH"
    assert eng.store.count("events") == 1
    shared = engine(tmp_path / "shared")
    request = task(shared, idempotency_key="idem-race")

    def once():
        return shared.append(request)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [item.result() for item in [pool.submit(once) for _ in range(8)]]
    winners = [item for item in results if not item.get("replayed")]
    replays = [item for item in results if item.get("code") == "IDEMPOTENCY_REPLAY"]
    assert len(winners) == 1 and len(replays) == 7
    assert shared.store.count("events") == 1
    assert all(item["lease_token"] is None for item in results)
    stored = (tmp_path / "store" / "events.sqlite3").read_bytes()
    assert SECRET.encode() not in stored


def test_transaction_rolls_back_when_the_process_dies_before_commit(tmp_path):
    eng = engine(tmp_path)
    payload = task(eng)
    (tmp_path / "request.json").write_text(json.dumps(payload), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "app.control_events.crash_probe", "--root", str(tmp_path / "store"),
         "--token", "generation-1", "--mode", "before-commit", "--request", str(tmp_path / "request.json")],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 90, proc.stderr
    reopened = Engine.open(tmp_path / "store", "generation-1")
    assert reopened.store.count("events") == 0
    assert reopened.store.count("idempotency") == 0
    recovered = reopened.append(payload)
    assert recovered["applied"] is True and recovered.get("replayed") is not True
    assert reopened.store.count("events") == 1


def test_reports_hit_disk_before_remote_status_and_survive_restart(tmp_path):
    eng = engine(tmp_path)
    calls = []

    def remote(channel, payload):
        disk = json.loads((tmp_path / "store" / "report-outbox" / f"{payload['run_id']}.json").read_text(encoding="utf-8"))
        assert disk["outcome"] == payload["outcome"]
        assert disk["head"] == SHA and disk["phase"] == payload["phase"]
        calls.append((channel, disk["run_id"]))
        return confirm()

    success = report("1" * 32, outcome="success", phase="passed")
    failure = report("2" * 32, outcome="failure", phase="pytest-failed", note=SECRET)
    done = eng.submit_report(success, remote=remote)
    failed = eng.submit_report(failure, remote=remote)
    assert done["complete"] is True and failed["complete"] is True
    assert {item["state"] for item in done["receipts"]} == {"DELIVERED"}
    assert all(item["complete"] is True and item["lease_token"] is None and item["outcome"] == "failure" for item in failed["receipts"])
    archived_failure = tmp_path / "store" / "report-archive" / f"{failure['run_id']}.json"
    assert archived_failure.exists()
    assert SECRET not in archived_failure.read_text(encoding="utf-8")
    assert not (tmp_path / "store" / "report-outbox" / f"{failure['run_id']}.json").exists()
    assert len(calls) == 4
    incomplete = report("3" * 32, outcome="failure", phase="pytest-failed")
    del incomplete["head"]
    before = list((tmp_path / "store" / "report-outbox").glob("*.json"))
    with pytest.raises(ControlError):
        eng.submit_report(incomplete, remote=remote)
    assert list((tmp_path / "store" / "report-outbox").glob("*.json")) == before

    def fail_status(channel, _payload):
        if channel == "commit_status":
            raise BadRecord("status record is bad")
        return confirm()

    mixed = eng.submit_report(report("4" * 32, outcome="failure", phase="compile-failed"), remote=fail_status)
    by_channel = {item["channel"]: item["state"] for item in mixed["receipts"]}
    assert by_channel["github_comment"] == "DELIVERED"
    assert by_channel["commit_status"] == "QUARANTINED"
    health = eng.health()
    assert health["channels"]["github_comment"]["DELIVERED"] >= 1
    assert health["channels"]["commit_status"]["QUARANTINED"] == 1
    assert health["production_protocol_enabled"] is False

    limited = engine(tmp_path / "limited", limits=OutboxLimits(max_outbox_reports=1, max_quarantine_reports=1))
    limited.submit_report(report("5" * 32, outcome="success", phase="passed"), remote=lambda *_args: None)
    with pytest.raises(ControlError) as capacity:
        limited.submit_report(report("6" * 32, outcome="failure", phase="test-failed"), remote=lambda *_args: None)
    assert capacity.value.code == "CAPACITY_EXHAUSTED"
    assert limited.health()["outbox_remaining_reports"] == 0
    assert (tmp_path / "limited" / "store" / "report-outbox" / f"{'5' * 32}.json").exists()
    assert not (tmp_path / "limited" / "store" / "report-outbox" / f"{'6' * 32}.json").exists()

    overflowing = engine(tmp_path / "overflow", limits=OutboxLimits(max_quarantine_reports=1))

    def both_bad(_channel, _payload):
        raise BadRecord("bad")

    overflowing.submit_report(report("7" * 32, outcome="failure", phase="frontend-failed"), remote=both_bad)
    assert overflowing.health()["quarantine_overflow"] >= 1
    assert overflowing.health()["channels"]["github_comment"]["QUARANTINED"] == 1
    assert overflowing.health()["channels"]["commit_status"]["QUARANTINED"] == 1
    garbage = tmp_path / "store" / "report-outbox" / f"{'8' * 32}.json"
    garbage.write_text("{", encoding="utf-8")
    reopened = Engine.open(tmp_path / "store", "generation-1")
    assert not garbage.exists()
    assert (tmp_path / "store" / "report-archive" / f"{success['run_id']}.json").exists()
    assert reopened.health()["quarantine_reports"] >= 1
    assert reopened.health()["archive_reports"] >= 1


def test_process_crash_after_durable_report_keeps_the_run(tmp_path):
    root = tmp_path / "crash"
    root.mkdir()
    Engine.open(root, "generation-1")
    payload = {
        "report": report("9" * 32, outcome="failure", phase="setup-failed"),
        "channels": ["github_comment", "commit_status"],
    }
    request = tmp_path / "report.json"
    request.write_text(json.dumps(payload), encoding="utf-8")
    for mode, code in (("after-file", 92), ("before-remote", 91)):
        case = tmp_path / mode
        case.mkdir()
        Engine.open(case, "generation-1")
        proc = subprocess.run(
            [sys.executable, "-m", "app.control_events.crash_probe", "--root", str(case),
             "--token", "generation-1", "--mode", mode, "--request", str(request)],
            cwd=ROOT, capture_output=True, text=True,
        )
        assert proc.returncode == code, proc.stderr
        assert not (case / "remote-called.json").exists()
        assert (case / "report-outbox" / f"{'9' * 32}.json").exists()
        restored = Engine.open(case, "generation-1")
        assert restored.health()["backlog"] >= 1
        assert "DURABLE_LOCAL" in restored.health()["channels"].get("commit_status", {})
        calls = []
        receipts = restored.deliver_pending(lambda channel, _report: calls.append(channel) or confirm())
        assert set(calls) == {"github_comment", "commit_status"}
        assert {item["state"] for item in receipts} == {"DELIVERED"}
        assert all(item["complete"] and item["run_id"] == "9" * 32 for item in receipts)


def test_legacy_dual_read_is_unscoped_and_not_authorization(tmp_path):
    eng = engine(tmp_path)
    eng.append(task(eng, progress_status="submitted", attempt=2, run_id="run_current"))
    before = eng.watermarks()
    empty = tmp_path / "empty.tsv"
    empty.write_text("", encoding="utf-8")
    missing = tmp_path / "missing.tsv"
    folder = tmp_path / "dir.tsv"
    folder.mkdir()
    log = tmp_path / "operations.tsv"
    rows = [f"2026-11-27T10:00:{index:02d}+00:00\tstartup_ok\t{SHA}\n" for index in range(101)]
    rows.append(f"2026-11-27T11:00:00+00:00\tnot_a_real_code\t{SHA_B}\n")
    rows.append("broken-line\n")
    log.write_text("".join(rows), encoding="utf-8")
    created = "2026-11-27T10:00:00+00:00"
    comments = [
        {"id": 1, "author_association": "OWNER", "created_at": created, "updated_at": created, "user": {"login": "layho"},
         "body": f"[APP_PROGRESS] {json.dumps({'status': 'submitted', 'sha': SHA, 'pr': 7, 'agent': 'Grok'})}"},
        {"id": 2, "author_association": "OWNER", "created_at": created, "updated_at": created, "user": {"login": "layho"},
         "body": "[APP_PROGRESS] {not-json"},
        {"id": 3, "author_association": "OWNER", "created_at": created, "updated_at": created, "user": {"login": "layho"},
         "body": f"[APP_PROGRESS] {json.dumps({'status': 'accepted', 'sha': SHA, 'pr': 7, 'agent': 'ChatGPT', 'run_id': 'run_old'})}"},
        {"id": 4, "author_association": "NONE", "created_at": created, "updated_at": created, "user": {"login": "layho"},
         "body": "[APP_PROGRESS] {\"status\":\"accepted\"}"},
        {"id": 5, "author_association": "OWNER", "created_at": created, "updated_at": created, "user": {"login": "layho"},
         "body": "[MACCI_REPORT] " + json.dumps(report("a" * 32, outcome="success", phase="passed"))},
    ]
    empty_read = eng.dual_read(empty, [])
    failed_read = eng.dual_read(missing, [])
    directory_read = eng.dual_read(folder, [])
    combined = eng.dual_read(log, comments)
    assert empty_read["operations"]["kind"] == "empty"
    assert failed_read["operations"]["kind"] == "read_failed"
    assert directory_read["operations"]["kind"] == "read_failed"
    assert failed_read["operations"]["kind"] != empty_read["operations"]["kind"]
    assert combined["operations"]["total"] == 103
    assert combined["operations"]["truncated"] is True
    assert len(combined["operations"]["recent_view"]) == 100
    assert len(combined["operations"]["rows"]) == 103
    assert any(row["unrecognized"] and row["code"] == "not_a_real_code" for row in combined["operations"]["rows"])
    assert all(row["producer_seq"] is None and row["authorizes"] is False and row["legacy_unscoped"] is True for row in combined["operations"]["rows"])
    progress = [row for row in combined["comments"]["records"] if row.get("kind") == "app_progress"]
    assert any(row["status"] == "submitted" and row["agent"] == "Grok" for row in progress)
    chatgpt = next(row for row in progress if row.get("run_id") == "run_old")
    assert chatgpt["agent"] is None and chatgpt["authorizes"] is False and chatgpt["producer_seq"] is None
    assert combined["comments"]["format_errors"] >= 1
    assert combined["authorizations_from_legacy"] == []
    assert combined["legacy_in_watermark"] is False
    assert eng.watermarks() == before
    assert eng.authorizations() == []
    current = next(row for row in combined["schema2_rounds"] if row["run_id"] == "run_current")
    assert current["status"] == "submitted"
    assert "ChatGPT" not in KNOWN_AGENTS
    assert KNOWN_AGENTS <= set(re.findall(r'"(Codex|Grok|Claude|DeepSeek|Qwen|ChatGPT)"', (ROOT / "app" / "feedback_progress.py").read_text(encoding="utf-8")))
    assert "ChatGPT" not in re.search(r'event\.get\("agent"\) not in \(([^)]*)\)', (ROOT / "app" / "feedback_progress.py").read_text(encoding="utf-8")).group(1)
    assert "accepted" in STATUSES


def test_routes_reject_unbound_proofs_and_redact_receipts(tmp_path):
    eng = engine(tmp_path)
    application = FastAPI()
    application.include_router(router)
    client = TestClient(application)
    payload = task(eng, note=SECRET)
    unbound = client.post("/api/v1/control/events", json=payload)
    assert unbound.status_code == 403
    assert unbound.json()["code"] == "NOT_AUTHORIZED"
    assert eng.store.count("idempotency") == 0
    bind_isolated_engine(eng)
    set_isolated(False)
    hidden = client.post("/api/v1/control/events", json=payload)
    assert hidden.status_code == 403
    assert eng.store.count("events") == 0
    set_isolated(True)
    allowed = client.post("/api/v1/control/events", json=payload)
    assert allowed.status_code == 200
    assert allowed.json()["applied"] is True
    assert SECRET not in allowed.text
    replay = client.post("/api/v1/control/events", json=payload)
    assert replay.status_code == 200
    assert replay.json()["code"] == "IDEMPOTENCY_REPLAY"
    assert replay.json()["lease_token"] is None
    assert eng.store.count("events") == 1
    denied = client.get("/api/v1/control/diagnostics", params={"subject": "tsk_w01", "stream_id": "hostops:diagnostic", "stream_epoch": 1, "after_seq": 0})
    assert denied.status_code == 403 and denied.json()["code"] == "NOT_AUTHORIZED"
    denied_events = client.get("/api/v1/control/events", params={"stream_id": "fixture:task", "stream_epoch": 1, "after_seq": 0})
    assert denied_events.status_code == 403
    diag = client.get(
        "/api/v1/control/diagnostics",
        params={"subject": "tsk_w01", "stream_id": "hostops:diagnostic", "stream_epoch": 1, "after_seq": 0},
        headers={"X-Control-Credential": eng.credential_for("hostops")},
    )
    assert diag.status_code == 200 and diag.json()["side_effect"] is False and diag.json()["cursor_persisted"] is False
    assert eng.cursor("hostops:diagnostic", 1) is None
    clear_binding()
    proc = subprocess.run(
        [sys.executable, "-c",
         "import os,sys\n"
         f"os.environ['DATABASE_URL']='sqlite:///{tmp_path / 'app.db'}'\n"
         "from app.main import app\n"
         "paths=[getattr(route,'path','') for route in app.routes]\n"
         "assert not any(path.startswith('/api/v1/control') for path in paths)\n"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_runner_phase_set_is_not_extended():
    spec = importlib.util.spec_from_file_location("macci_runner", ROOT / "deploy" / "macci" / "runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert FAILURE_PHASES <= module.REPORT_PHASES
    assert "passed" in module.REPORT_PHASES
    assert "ChatGPT" not in FAILURE_PHASES


def test_delivery_unknown_is_not_success(tmp_path):
    eng = engine(tmp_path)

    def remote(channel, _payload):
        if channel == "commit_status":
            raise DeliveryUnknown("lost response")
        return confirm()

    result = eng.submit_report(report("b" * 32, outcome="success", phase="passed"), remote=remote)
    states = {item["channel"]: item["state"] for item in result["receipts"]}
    assert states["github_comment"] == "DELIVERED"
    assert states["commit_status"] == "DELIVERY_UNKNOWN"
    assert result["complete"] is False
    assert eng.health()["oldest_pending_at"]
    assert eng.health()["backlog"] >= 1


def test_client_claims_are_not_authority(tmp_path):
    eng = engine(tmp_path)
    forged = task(eng)
    forged["credential"] = "test-proof://w01/fixture"
    forged["trusted_source"] = "policy_service"
    forged["authorizer"] = "policy_service"
    before = eng.store.count("idempotency")
    with pytest.raises(ControlError) as denied:
        eng.append(forged)
    assert denied.value.code == "NOT_AUTHORIZED"
    assert eng.store.count("idempotency") == before
    empty = task(eng)
    empty["authorizer"] = ""
    with pytest.raises(ControlError) as blank:
        eng.append(empty)
    assert blank.value.code == "NOT_AUTHORIZED"
    cross = authorization(eng)
    cross["credential"] = eng.credential_for("fixture")
    with pytest.raises(ControlError):
        eng.append(cross)
    with pytest.raises(AuthzError):
        AuthzService.from_request({"actor": actor(), "trusted_source": "policy_service"})
    real = AuthzService(registry=isolated_registry(eng.repo_id), isolated=False, policy_revision=POLICY_REVISION)
    with pytest.raises(AuthnError):
        real.authenticate(eng.credential_for("fixture"))
    disabled = isolated_registry(eng.repo_id)
    disabled.allow_test_identities = False
    closed = AuthzService(registry=disabled, isolated=True, policy_revision=POLICY_REVISION)
    with pytest.raises(AuthnError):
        closed.authenticate(eng.credential_for("policy_service"))
    snooper = eng.credential_for("hostops")
    eng.append(task(eng))
    with pytest.raises(ControlError) as cross_task:
        eng.query(credential=snooper, stream_id="hostops:diagnostic", stream_epoch=1, task_id="tsk_other")
    assert cross_task.value.code == "NOT_AUTHORIZED"
    with pytest.raises(ControlError):
        eng.query(credential=eng.credential_for("fixture"), stream_id="hostops:diagnostic", stream_epoch=1, task_id="tsk_w01")


def test_replay_outoforder_revoke_and_append_after_close(tmp_path):
    eng = engine(tmp_path)
    grant = authorization(eng)
    assert eng.append(grant)["applied"] is True
    early = eng.append(authorization(
        eng, event_id="evt_gap", idempotency_key="idem-gap", producer_seq=3,
        event_type="authorization.revoked", revision=3, expected_revision=2,
    ))
    assert early["applied"] is False and early["hold_reason"] == "GAP"
    assert eng.authorizations()[0]["revoked"] is False
    revoke = eng.append(authorization(
        eng, event_id="evt_revoke", idempotency_key="idem-revoke", producer_seq=2,
        event_type="authorization.revoked", revision=2, expected_revision=1,
    ))
    assert revoke["applied"] is True
    assert eng.authorizations()[0]["revoked"] is True
    assert eng.watermarks()["policy_service:authorization@1"]["seq"] == 3
    replay = eng.append(grant)
    assert replay["replayed"] is True
    assert eng.authorizations()[0]["revoked"] is True
    old_grant = eng.append(authorization(
        eng, event_id="evt_old_grant", idempotency_key="idem-old-grant", producer_seq=4,
        revision=1, expected_revision=3,
    ))
    assert old_grant["applied"] is True and old_grant["projected"] is False
    assert eng.authorizations()[0]["revoked"] is True
    assert eng.fact("authorization:tsk_w01|cand_w01|preview.merge")["revision"] == 3
    clash = eng.append(authorization(
        eng, event_id="evt_same_rev", idempotency_key="idem-same-rev", producer_seq=5,
        revision=3, expected_revision=3,
    ))
    assert clash["code"] == "CONFLICT_REVISION"
    assert eng.authorizations()[0]["revoked"] is True
    forged_close = eng.append(authorization(
        eng, event_id="evt_forged_close", idempotency_key="idem-forged-close", producer_seq=5,
        event_type="authorization.epoch_closed", epoch_closed=True, closed_through_seq=9,
    ))
    assert forged_close["code"] == "EVIDENCE_MISSING"
    closed = eng.append(authorization(
        eng, event_id="evt_close", idempotency_key="idem-close-after", producer_seq=5,
        event_type="authorization.epoch_closed", epoch_closed=True, closed_through_seq=5,
    ))
    assert closed["applied"] is True
    assert eng.watermarks()["policy_service:authorization@1"]["closed"] is True
    appended = eng.append(authorization(
        eng, event_id="evt_after_close", idempotency_key="idem-after-close", producer_seq=6,
        revision=4, expected_revision=3,
    ))
    assert appended["code"] == "EVIDENCE_MISSING"
    assert eng.watermarks()["policy_service:authorization@1"]["seq"] == 5
    assert eng.authorizations()[0]["revoked"] is True
    with pytest.raises(ControlError):
        eng.append(task(eng, event_id="evt_unknown_type", idempotency_key="idem-unknown", event_type="deployment.note"))
    with pytest.raises(ControlError):
        eng.append(task(eng, event_id="evt_bool_seq", idempotency_key="idem-bool", producer_seq=True))


def test_unproven_guard_and_illegal_initial_state(tmp_path):
    eng = engine(tmp_path)
    assert eng.append(task(eng, state="RETRY_WAIT", event_id="evt_bad_initial", idempotency_key="idem-bad-initial"))["code"] == "ILLEGAL_TRANSITION"
    assert eng.store.count("events") == 0
    assert eng.append(task(eng, state="PENDING"))["applied"] is True
    denied = eng.append(task(
        eng, event_id="evt_lease", idempotency_key="idem-lease", producer_seq=2,
        state="LEASED", revision=2, expected_revision=1, guard="claim",
    ))
    assert denied["code"] == "EVIDENCE_MISSING"
    assert eng.fact("task:run_1")["state"] == "PENDING"
    proven = engine(tmp_path / "proven", proven_guards={"task:run_1": {"claim"}})
    assert proven.append(task(proven, state="PENDING"))["applied"] is True
    moved = proven.append(task(
        proven, event_id="evt_lease", idempotency_key="idem-lease", producer_seq=2,
        state="LEASED", revision=2, expected_revision=1,
    ))
    assert moved["applied"] is True
    assert proven.fact("task:run_1")["state"] == "LEASED"


def test_unconnected_stays_pending_until_confirmation(tmp_path):
    eng = engine(tmp_path)
    pending = eng.submit_report(report("c" * 32, outcome="success", phase="passed"), remote=None)
    assert pending["connected"] is False and pending["complete"] is False
    assert {item["state"] for item in pending["receipts"]} == {"DURABLE_LOCAL"}
    assert all(item["complete"] is False and item["exactly_once"] is False and item["tree"] == SHA_D for item in pending["receipts"])

    def silent(_channel, _payload):
        return None

    unconfirmed = eng.submit_report(report("d" * 32, outcome="failure", phase="fetch-failed"), remote=silent)
    assert unconfirmed["complete"] is False
    assert {item["state"] for item in unconfirmed["receipts"]} == {"DELIVERY_UNKNOWN"}
    with pytest.raises(ControlError) as executor:
        eng.submit_report(report("e" * 32, outcome="success", phase="passed", executor_id="caller-chosen"), remote=confirm)
    assert executor.value.code == "UNKNOWN_EXECUTOR"
    with pytest.raises(ControlError):
        eng.submit_report(report("f" * 32, outcome="success", phase="passed"), channels=["../escape"], remote=confirm)
    assert not (tmp_path / "escape").exists()


def test_concurrent_dispatch_sends_once(tmp_path):
    root = tmp_path / "race"
    root.mkdir()
    eng = Engine.open(root, "generation-1", abandon_claims=False)
    payload = report("a" * 32, outcome="success", phase="passed")
    pending = eng.submit_report(payload, remote=None)
    assert pending["complete"] is False
    request = tmp_path / "request.json"
    request.write_text("{}", encoding="utf-8")
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "app.control_events.crash_probe", "--root", str(root),
             "--token", "generation-1", "--mode", "dispatch", "--request", str(request)],
            cwd=ROOT,
        )
        for _ in range(2)
    ]
    codes = [proc.wait(timeout=30) for proc in procs]
    assert codes == [0, 0]
    lines = (root / "remote-calls.log").read_text(encoding="utf-8").splitlines()
    assert lines.count("github_comment") == 1
    assert lines.count("commit_status") == 1


def test_remote_success_survives_local_receipt_loss(tmp_path):
    root = tmp_path / "loss"
    root.mkdir()
    Engine.open(root, "generation-1")
    payload = {
        "report": report("b" * 32, outcome="failure", phase="setup-failed"),
        "channels": ["github_comment", "commit_status"],
    }
    request = tmp_path / "report.json"
    request.write_text(json.dumps(payload), encoding="utf-8")
    eng = Engine.open(root, "generation-1")
    eng.submit_report(payload["report"], channels=payload["channels"], remote=None)
    proc = subprocess.run(
        [sys.executable, "-m", "app.control_events.crash_probe", "--root", str(root),
         "--token", "generation-1", "--mode", "after-remote", "--request", str(request)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 93, proc.stderr
    marker = json.loads((root / "remote-ok.json").read_text(encoding="utf-8"))
    restored = Engine.open(root, "generation-1")
    sends = []

    class Remote:
        def __call__(self, channel, _report):
            sends.append(channel)
            return confirm()

        def reconcile(self, channel, _report):
            return "delivered" if channel == marker["channel"] else "absent"

    receipts = restored.deliver_pending(Remote())
    assert marker["channel"] not in sends
    by_channel = {item["channel"]: item["state"] for item in receipts}
    assert by_channel[marker["channel"]] == "DELIVERED"
    assert all(item["tree"] == SHA_D and item["candidate_bound"] is True for item in receipts)


def test_archive_capacity_oldest_pending_and_redaction_identity(tmp_path):
    eng = engine(tmp_path, limits=OutboxLimits(max_outbox_reports=1, max_quarantine_reports=0))
    first = report("1" * 32, outcome="success", phase="passed")
    second = report("2" * 32, outcome="failure", phase="test-failed")
    assert eng.submit_report(first, remote=confirm)["complete"] is True
    assert not (tmp_path / "store" / "report-outbox" / f"{first['run_id']}.json").exists()
    assert (tmp_path / "store" / "report-archive" / f"{first['run_id']}.json").exists()
    assert eng.submit_report(second, remote=confirm)["complete"] is True
    assert eng.health()["archive_reports"] == 2
    held = engine(tmp_path / "held", limits=OutboxLimits(max_quarantine_reports=0))
    garbage = tmp_path / "held" / "store" / "report-outbox" / ("ab" * 16 + ".json")
    garbage.parent.mkdir(parents=True, exist_ok=True)
    garbage.write_text("{", encoding="utf-8")
    reopened = Engine.open(tmp_path / "held" / "store", "generation-1", limits=OutboxLimits(max_quarantine_reports=0))
    assert not garbage.exists()
    assert list((tmp_path / "held" / "store" / "report-outbox" / "held").glob("*.json"))
    assert reopened.health()["quarantine_overflow"] >= 1
    stale = engine(tmp_path / "stale")

    def unknown(channel, _payload):
        if channel == "commit_status":
            raise DeliveryUnknown("lost")
        return confirm()

    stale.submit_report(report("3" * 32, outcome="success", phase="passed"), remote=unknown)
    oldest = stale.health()["oldest_pending_at"]
    time.sleep(0.05)
    stale.deliver_pending(unknown)
    assert stale.health()["oldest_pending_at"] == oldest
    redacted = task(eng, note="github_pat_AAAA", idempotency_key="idem-secret", event_id="evt_secret")
    other = task(eng, note="github_pat_BBBB", idempotency_key="idem-secret", event_id="evt_secret")
    assert eng.append(redacted)["applied"] is True
    mismatch = eng.append(other)
    assert mismatch["code"] == "IDEMPOTENCY_PAYLOAD_MISMATCH"
    assert b"github_pat_AAAA" not in (tmp_path / "store" / "events.sqlite3").read_bytes()
    page = eng.query(credential=eng.credential_for("fixture"), stream_id="fixture:task", stream_epoch=1, after_seq=0, limit=1)
    assert page["side_effect"] is False
    assert page["truncated"] is False or page["next_after_seq"]
