"""Isolated V0.6.0 W01 event engine. These tests drive the store, not the contract strings."""

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.control_authz import AuthnError, AuthzError, AuthzService, MATRIX, Principal, PrincipalRegistry, REQUIRED_SCOPES, SOURCE_BY_KIND
from app.control_events.auth_bind import POLICY_REVISION, STREAM_REPAIR
from app.control_events.engine import FOLLOW_UPS, PACKAGE_COMPLETE, Engine
from app.control_events.errors import BadRecord, ControlError, DeliveryUnknown
from app.control_events.identity import bound_token_for_directory
from app.control_events.legacy import KNOWN_AGENTS
from app.control_events.event_outbox import confirmation_binds, exact_confirmation
from app.control_events.outbox import FAILURE_PHASES, OutboxLimits, bound_confirmation, confirmation_binds as report_binds
from app.control_events.store import Store
from app.control_routes_w01 import bind_isolated_engine, clear_binding, router, set_isolated
from app.feedback_progress import STATUSES
from tests.w01_isolated_principals import ensure_principals, sign_event, sign_read, sign_report

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40
SECRET = "github_pat_SHOULDNOTLEAK"


def engine(tmp_path, token="generation-1", **kwargs):
    opened = Engine.open(tmp_path / "store", token, **kwargs)
    ensure_principals(opened)
    return opened


def actor(producer="fixture"):
    return {"kind": "test", "id": producer, "proof_ref": f"test-proof://w01/{producer}"}


def body(eng, **fields):
    producer = fields.get("producer_id", "fixture")
    request = {
        "schema_version": 2,
        "actor": actor(producer),
        "scope": {"repo": "layho/web-app-init-base-env", "task_id": "tsk_w01"},
        "expected_revision": 1,
        "idempotency_key": "idem-w01",
        "repo_id": eng.repo_id,
        "authorizer": producer,
    }
    request.update(fields)
    request["authorizer"] = request.get("producer_id", producer)
    return sign_event(eng, request)


def confirm(channel, payload):
    return bound_confirmation(channel, payload)


def _bind(eng, payload):
    payload["authorizer"] = payload.get("producer_id", "fixture")
    return sign_event(eng, payload)


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
        task_id="tsk_w01",
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
        app_id="base",
        host_id=None,
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
        app_id="base",
        host_id=None,
        task_id="tsk_w01",
        diagnostic_class="queue",
        subject_id="tsk_w01",
        severity="info",
        trusted_source="hostops_readonly",
        idempotency_key="idem-diag",
    )
    payload.update(fields)
    return _bind(eng, payload)


def report(eng, run_id, *, outcome, phase, note=None, **fields):
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
        "repo_id": eng.repo_id,
        "app_id": "base",
        "task_id": "tsk_w01",
        "stream_id": "macci-isolated:evidence",
        "host_id": "",
        "producer_id": "macci-isolated",
    }
    if note:
        payload["note"] = note
    payload.update(fields)
    return sign_report(eng, payload)


def test_w05_intent_bridge_uses_signed_w01_evidence_without_business_commit(tmp_path):
    from app.control_joint.w05_issue import record_isolated_intent

    eng = engine(tmp_path)
    event = {
        "event_id": "evt_w05_intent", "repo_id": eng.repo_id, "app_id": "base",
        "task_id": "tsk_w01", "run_id": "run_w05_intent",
        "producer_id": "w05", "stream_id": "w05:task", "stream_epoch": 1,
        "producer_seq": 1, "occurred_at": 1700000000,
        "note": "w05-intent-v1:" + "a" * 64,
    }
    signer = lambda body: sign_event(eng, body, signer="w05")
    verified = record_isolated_intent(eng, event=event, sign_request=signer)
    assert verified["status"] == "intent_verified", verified
    assert verified["business_committed"] is False
    assert verified["production_effective"] is False
    assert verified["w01_receipt"]["evidence"] == "verified"
    assert eng.fact("task:run_w05_intent") is None
    assert eng.store.count("events") == 1
    changed = {**event, "note": "w05-intent-v1:" + "b" * 64}
    assert record_isolated_intent(eng, event=changed, sign_request=signer)["status"] == "unknown"
    assert record_isolated_intent(eng, event={**event, "event_id": "evt_foreign",
                                            "task_id": "tsk_foreign", "producer_seq": 2},
                                  sign_request=signer)["status"] == "unknown"
    assert eng.store.count("events") == 1
    assert record_isolated_intent(eng, event={**event, "event_id": "evt_wrong_role",
                                            "producer_seq": 2},
                                  sign_request=lambda body: sign_event(eng, body, signer="fixture"))["status"] == "unknown"
    assert eng.store.count("events") == 1


def test_w05_signed_intent_business_cas_recovers_without_fixture_fallback(tmp_path):
    from app.control_delivery.ports import (
        FixtureAuth, FixtureEvents, BlockedRemote, BlockedSchedule, BlockedDeploy, ServerClock,
    )
    from app.control_delivery.service import DeliveryService

    eng = engine(tmp_path)
    root = tmp_path / "delivery"
    fixture_events = FixtureEvents()
    options = dict(auth=FixtureAuth(), events=fixture_events, remote=BlockedRemote(),
                   schedule=BlockedSchedule(), deploy=BlockedDeploy(), clock=ServerClock())
    signer = lambda body: sign_event(eng, body, signer="w05")
    svc = DeliveryService(root, **options, intent_engine=eng, intent_signer=signer)
    request = dict(actor=actor(), scope={"repo": eng.repo_id, "task_id": "tsk_w01"},
                   author_id="fixture", feedback_id="fb_signed", category="bug",
                   body="signed transaction", idempotency_key="signed-submit")
    def crash(*args, **kwargs):
        raise RuntimeError("crash after W01 before W05 commit")

    svc._commit_local = crash
    with pytest.raises(RuntimeError, match="crash after W01"):
        svc.submit_feedback(**request)
    assert svc.store.read("feedback", "fb_signed") is None
    assert eng.store.count("events") == 1
    pending, = svc.store.list("outbox")
    assert pending["intent_mode"] == "signed-isolated-v1"
    assert pending["receipt"] is None
    fixture = DeliveryService(root, **options)
    assert not fixture.submit_feedback(**request).ok
    assert not fixture_events.events
    restarted = DeliveryService(root, **options, intent_engine=eng, intent_signer=signer)
    result = restarted.submit_feedback(**request)
    assert result.ok, result.as_dict()
    assert eng.store.count("events") == 1
    assert restarted.store.read("feedback", "fb_signed")["revision"] == 1
    outbox = restarted.store.read("outbox", pending["event_id"])
    assert outbox["receipt"]["isolated_signature_verified"] is True
    assert outbox["receipt"]["w01_proof"] is False
    witness = restarted.store.read("business-commit", pending["event_id"])
    assert witness["intent_mode"] == "signed-isolated-v1"
    assert witness["production_effective"] is False
    assert restarted.submit_feedback(**request).ok
    assert eng.store.count("events") == 1
    assert not fixture_events.events

    denied = DeliveryService(root, **options, intent_engine=eng,
                             intent_signer=lambda body: sign_event(eng, body, signer="fixture"))
    rejected = denied.submit_feedback(**{**request, "feedback_id": "fb_denied",
                                         "idempotency_key": "denied-submit"})
    assert not rejected.ok
    assert denied.store.read("feedback", "fb_denied") is None
    assert eng.store.count("events") == 1
    assert not fixture_events.events


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
    assert first["delivery_disposition"] == "DURABLE_LOCAL"
    assert first["outbox_consumer"] == "generic"
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
    auth_query = eng.query(request=sign_read(eng, "policy_service", "event.read", stream_id="policy_service:authorization"), stream_id="policy_service:authorization", stream_epoch=1, after_seq=0)
    diag_query = eng.query(request=sign_read(eng, "hostops", "event.read", stream_id="hostops:diagnostic"), stream_id="hostops:diagnostic", stream_epoch=1, after_seq=0)
    assert auth_query["gap"] is True and auth_query["watermark_seq"] == 0
    assert diag_query["gap"] is False and diag_query["watermark_seq"] == 1 and diag_query["side_effect"] is False
    filled = eng.append(authorization(eng, event_id="auth_1", idempotency_key="idem-auth-1"))
    assert filled["applied"] is True
    assert eng.watermarks()["policy_service:authorization@1"]["seq"] == 2
    assert eng.watermarks()["policy_service:authorization@1"]["closed"] is False
    assert eng.query(request=sign_read(eng, "policy_service", "event.read", stream_id="policy_service:authorization"), stream_id="policy_service:authorization", stream_epoch=2, after_seq=0)["watermark_seq"] == 0
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
    host = sign_read(eng, "hostops", "diagnostic.read", stream_id="hostops:diagnostic", object_id="subj")
    other_cred = sign_read(eng, "hostops_b", "diagnostic.read", stream_id="hostops_b:diagnostic", object_id="subj")
    first = eng.poll_diagnostics(request=host, subject="subj", stream_id="hostops:diagnostic", stream_epoch=1, after_seq=1)
    assert first["side_effect"] is False and first["cursor_persisted"] is False and first["gap"] is False
    assert len(first["items"]) == 0
    assert eng.cursor("hostops:diagnostic", 1) is None
    other = eng.poll_diagnostics(request=other_cred, subject="subj", stream_id="hostops_b:diagnostic", stream_epoch=1, after_seq=0)
    assert len(other["items"]) == 1 and other["side_effect"] is False and eng.cursor("hostops_b:diagnostic", 1) is None
    saved = eng.commit_cursor(request=sign_read(eng, "hostops", "diagnostic.cursor", stream_id="hostops:diagnostic", object_id="subj", method="POST", expected_revision=1), subject="subj", stream_id="hostops:diagnostic", stream_epoch=1, after_seq=1)
    assert saved["side_effect"] is True and saved["cursor_after_seq"] == 1
    other_saved = eng.commit_cursor(request=sign_read(eng, "hostops_b", "diagnostic.cursor", stream_id="hostops_b:diagnostic", object_id="subj", method="POST"), subject="subj", stream_id="hostops_b:diagnostic", stream_epoch=1, after_seq=0)
    assert other_saved["cursor_after_seq"] == 0
    assert eng.cursor("hostops:diagnostic", 1, saved["principal_id"]) == 1
    rewind = eng.poll_diagnostics(request=sign_read(eng, "hostops", "diagnostic.read", stream_id="hostops:diagnostic", object_id="subj"), subject="subj", stream_id="hostops:diagnostic", stream_epoch=1, after_seq=0)
    assert len(rewind["items"]) == 1 and rewind["side_effect"] is False
    assert eng.cursor("hostops:diagnostic", 1, saved["principal_id"]) == 1
    skipped = eng.poll_diagnostics(request=sign_read(eng, "hostops", "diagnostic.read", stream_id="hostops:diagnostic", object_id="subj"), subject="subj", stream_id="hostops:diagnostic", stream_epoch=1, after_seq=5)
    assert skipped["gap"] is True and eng.cursor("hostops:diagnostic", 1, saved["principal_id"]) == 1
    with pytest.raises(ControlError) as ignored:
        eng.poll_diagnostics(request=sign_read(eng, "hostops", "diagnostic.read", stream_id="policy_service:authorization"), subject="subj", stream_id="policy_service:authorization", stream_epoch=1, after_seq=0)
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
    assert reopened.store.count("consumed_nonces") == 0
    assert reopened.store.count("event_outbox") == 0
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
        return confirm(channel, payload)

    success = report(eng, "1" * 32, outcome="success", phase="passed")
    failure = report(eng, "2" * 32, outcome="failure", phase="pytest-failed", note=SECRET)
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
    incomplete = report(eng, "3" * 32, outcome="failure", phase="pytest-failed")
    del incomplete["head"]
    before = list((tmp_path / "store" / "report-outbox").glob("*.json"))
    with pytest.raises(ControlError):
        eng.submit_report(incomplete, remote=remote)
    assert list((tmp_path / "store" / "report-outbox").glob("*.json")) == before

    def fail_status(channel, _payload):
        if channel == "commit_status":
            raise BadRecord("status record is bad")
        return confirm(channel, _payload)

    mixed = eng.submit_report(report(eng, "4" * 32, outcome="failure", phase="compile-failed"), remote=fail_status)
    by_channel = {item["channel"]: item["state"] for item in mixed["receipts"]}
    assert by_channel["github_comment"] == "DELIVERED"
    assert by_channel["commit_status"] == "QUARANTINED"
    health = eng.health()
    assert health["channels"]["github_comment"]["DELIVERED"] >= 1
    assert health["channels"]["commit_status"]["QUARANTINED"] == 1
    assert health["production_protocol_enabled"] is False

    limited = engine(tmp_path / "limited", limits=OutboxLimits(max_outbox_reports=1, max_quarantine_reports=1))
    limited.submit_report(report(limited, "5" * 32, outcome="success", phase="passed"), remote=lambda *_args: None)
    with pytest.raises(ControlError) as capacity:
        limited.submit_report(report(limited, "6" * 32, outcome="failure", phase="test-failed"), remote=lambda *_args: None)
    assert capacity.value.code == "CAPACITY_EXHAUSTED"
    assert limited.health()["outbox_remaining_reports"] == 0
    assert (tmp_path / "limited" / "store" / "report-outbox" / f"{'5' * 32}.json").exists()
    assert not (tmp_path / "limited" / "store" / "report-outbox" / f"{'6' * 32}.json").exists()

    overflowing = engine(tmp_path / "overflow", limits=OutboxLimits(max_quarantine_reports=1))

    def both_bad(_channel, _payload):
        raise BadRecord("bad")

    overflowing.submit_report(report(overflowing, "7" * 32, outcome="failure", phase="frontend-failed"), remote=both_bad)
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
    opened = Engine.open(root, "generation-1")
    ensure_principals(opened)
    payload = {
        "report": report(opened, "9" * 32, outcome="failure", phase="setup-failed"),
        "channels": ["github_comment", "commit_status"],
    }
    request = tmp_path / "report.json"
    request.write_text(json.dumps(payload), encoding="utf-8")
    for mode, code in (("after-file", 92), ("before-remote", 91)):
        case = tmp_path / mode
        case.mkdir()
        case_engine = Engine.open(case, "generation-1")
        ensure_principals(case_engine)
        request.write_text(json.dumps({
            "report": report(case_engine, "9" * 32, outcome="failure", phase="setup-failed"),
            "channels": ["github_comment", "commit_status"],
        }), encoding="utf-8")
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
        receipts = restored.deliver_pending(lambda channel, payload: calls.append(channel) or confirm(channel, payload))
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
         "body": "[MACCI_REPORT] " + json.dumps(report(eng, "a" * 32, outcome="success", phase="passed"))},
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
        headers={"X-Control-Proof": json.dumps(sign_read(eng, "hostops", "diagnostic.read", stream_id="hostops:diagnostic", object_id="tsk_w01"))},
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
        return confirm(channel, _payload)

    result = eng.submit_report(report(eng, "b" * 32, outcome="success", phase="passed"), remote=remote)
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
    cross["authorizer"] = "fixture"
    sign_event(eng, cross, signer="fixture")
    with pytest.raises(ControlError):
        eng.append(cross)
    with pytest.raises(AuthzError):
        AuthzService.from_request({"actor": actor(), "trusted_source": "policy_service"})
    with pytest.raises(AuthzError):
        AuthzService(registry=PrincipalRegistry(isolated=True), isolated=True, policy_revision="w01-isolated-1")
    real = AuthzService(registry=PrincipalRegistry(isolated=False), isolated=False, policy_revision=POLICY_REVISION)
    with pytest.raises(AuthnError):
        real.authenticate(
            operation="event.append",
            body=task(eng, event_id="evt_real_route", idempotency_key="idem-real-route"),
            route_class="isolated",
            now=int(time.time()),
            state=eng.auth_state,
            write=False,
        )
    assert SOURCE_BY_KIND["policy_service"] == "policy_service"
    for operation in ("event.append", "event.read", "diagnostic.cursor", "report.submit", "report.read"):
        assert operation in MATRIX and operation in REQUIRED_SCOPES
    eng.append(task(eng))
    with pytest.raises(ControlError) as cross_task:
        eng.query(
            request=sign_read(eng, "hostops", "event.read", stream_id="hostops:diagnostic", task_id="tsk_other"),
            stream_id="hostops:diagnostic",
            stream_epoch=1,
            task_id="tsk_other",
        )
    assert cross_task.value.code == "NOT_AUTHORIZED"
    with pytest.raises(ControlError):
        eng.query(
            request=sign_read(eng, "fixture", "event.read", stream_id="hostops:diagnostic"),
            stream_id="hostops:diagnostic",
            stream_epoch=1,
            task_id="tsk_w01",
        )


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
    proven = engine(tmp_path / "proven", proven_guards={("task:run_1", 2, "run.started"): {"claim"}})
    assert proven.append(task(proven, state="PENDING"))["applied"] is True
    moved = proven.append(task(
        proven, event_id="evt_lease", idempotency_key="idem-lease", producer_seq=2,
        state="LEASED", revision=2, expected_revision=1,
    ))
    assert moved["applied"] is True
    assert proven.fact("task:run_1")["state"] == "LEASED"


def test_unconnected_stays_pending_until_confirmation(tmp_path):
    eng = engine(tmp_path)
    pending = eng.submit_report(report(eng, "c" * 32, outcome="success", phase="passed"), remote=None)
    assert pending["connected"] is False and pending["complete"] is False
    assert {item["state"] for item in pending["receipts"]} == {"DURABLE_LOCAL"}
    assert all(item["complete"] is False and item["exactly_once"] is False and item["tree"] == SHA_D for item in pending["receipts"])

    def silent(_channel, _payload):
        return None

    unconfirmed = eng.submit_report(report(eng, "d" * 32, outcome="failure", phase="fetch-failed"), remote=silent)
    assert unconfirmed["complete"] is False
    assert {item["state"] for item in unconfirmed["receipts"]} == {"DELIVERY_UNKNOWN"}
    with pytest.raises(ControlError) as executor:
        eng.submit_report(report(eng, "e" * 32, outcome="success", phase="passed", executor_id="caller-chosen"), remote=confirm)
    assert executor.value.code == "UNKNOWN_EXECUTOR"
    with pytest.raises(ControlError):
        eng.submit_report(report(eng, "f" * 32, outcome="success", phase="passed"), channels=["../escape"], remote=confirm)
    assert not (tmp_path / "escape").exists()


def test_concurrent_dispatch_sends_once(tmp_path):
    root = tmp_path / "race"
    root.mkdir()
    eng = Engine.open(root, "generation-1", abandon_claims=False)
    ensure_principals(eng)
    payload = report(eng, "a" * 32, outcome="success", phase="passed")
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
    opened = Engine.open(root, "generation-1")
    ensure_principals(opened)
    payload = {
        "report": report(opened, "b" * 32, outcome="failure", phase="setup-failed"),
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
            return confirm(channel, _report)

        def reconcile(self, channel, report):
            if channel == marker["channel"]:
                return bound_confirmation(channel, report)
            return "absent"

    receipts = restored.deliver_pending(Remote())
    assert marker["channel"] not in sends
    by_channel = {item["channel"]: item["state"] for item in receipts}
    assert by_channel[marker["channel"]] == "DELIVERED"
    assert all(item["tree"] == SHA_D and item["candidate_bound"] is True for item in receipts)


def test_archive_capacity_oldest_pending_and_redaction_identity(tmp_path):
    eng = engine(tmp_path, limits=OutboxLimits(max_outbox_reports=1, max_quarantine_reports=0))
    first = report(eng, "1" * 32, outcome="success", phase="passed")
    second = report(eng, "2" * 32, outcome="failure", phase="test-failed")
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
        return confirm(channel, _payload)

    stale.submit_report(report(stale, "3" * 32, outcome="success", phase="passed"), remote=unknown)
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
    page = eng.query(request=sign_read(eng, "fixture", "event.read", stream_id="fixture:task"), stream_id="fixture:task", stream_epoch=1, after_seq=0, limit=1)
    assert page["side_effect"] is False
    assert page["truncated"] is False or page["next_after_seq"]


def test_low_authority_does_not_own_or_block_the_fact(tmp_path):
    eng = engine(tmp_path)
    observed = eng.append(deployment(
        eng, producer_id="side_observer", stream_id="side_observer:deployment",
        event_id="evt_obs_dep", idempotency_key="idem-obs-dep", trusted_source=None,
    ))
    assert observed["applied"] is True and observed["projected"] is False
    assert eng.fact("deployment:dep_1") is None
    owned = eng.append(deployment(eng))
    assert owned["applied"] is True
    fact = eng.fact("deployment:dep_1")
    assert fact["authoritative"] is True and fact["owner_producer"] == "host_deployer"
    later = eng.append(deployment(
        eng, producer_id="side_observer", stream_id="side_observer:deployment",
        event_id="evt_obs_2", idempotency_key="idem-obs-2", producer_seq=2,
        revision=9, expected_revision=1, trusted_source=None,
    ))
    assert later["applied"] is True
    assert eng.fact("deployment:dep_1")["revision"] == 1
    advanced = eng.append(deployment(
        eng, event_id="evt_own_2", idempotency_key="idem-own-2", producer_seq=2,
        revision=2, expected_revision=1,
    ))
    assert advanced["applied"] is True
    assert eng.fact("deployment:dep_1")["revision"] == 2
    handed = eng.append(deployment(
        eng, event_id="evt_hand", idempotency_key="idem-hand", producer_seq=3,
        revision=3, expected_revision=2, successor_producer="other_deployer",
    ))
    assert handed["applied"] is True
    assert eng.fact("deployment:dep_1")["successor_producer"] == "other_deployer"
    moved = eng.append(deployment(
        eng, producer_id="other_deployer", stream_id="other_deployer:deployment",
        event_id="evt_next_owner", idempotency_key="idem-next-owner",
        revision=4, expected_revision=3,
    ))
    assert moved["applied"] is True
    assert eng.fact("deployment:dep_1")["owner_producer"] == "other_deployer"
    assert eng.fact("deployment:dep_1")["revision"] == 4
    with eng.store.transaction() as conn:
        recorded = conn.execute(
            "SELECT COUNT(*) AS n FROM fact_history WHERE object_key='deployment:dep_1'"
        ).fetchone()["n"]
    assert recorded >= 3


def test_historical_gap_unproven_close_and_late_revocation(tmp_path):
    eng = engine(tmp_path)
    assert eng.append(deployment(eng, revision=2, expected_revision=2))["applied"] is True
    held = eng.append(deployment(
        eng, event_id="evt_hist", idempotency_key="idem-hist", producer_seq=3,
        revision=1, expected_revision=2,
    ))
    assert held["applied"] is False and held["hold_reason"] == "GAP"
    assert eng.append(deployment(
        eng, event_id="evt_fill", idempotency_key="idem-fill", producer_seq=2,
        revision=3, expected_revision=2,
    ))["applied"] is True
    assert eng.fact("deployment:dep_1")["revision"] == 3
    assert eng.watermarks()["host_deployer:deployment@1"]["seq"] == 3
    with eng.store.transaction() as conn:
        historical = conn.execute(
            "SELECT authoritative FROM fact_history WHERE event_id='evt_hist'"
        ).fetchone()
    assert historical is not None and historical["authoritative"] == 1

    grant = eng.append(authorization(eng, event_id="gap_grant", idempotency_key="idem-gap-grant"))
    assert grant["applied"] is True
    parked = eng.append(authorization(
        eng, event_id="gap_old_grant", idempotency_key="idem-gap-old", producer_seq=3,
        event_type="authorization.granted", revision=1, expected_revision=1,
    ))
    assert parked["applied"] is False and parked["hold_reason"] == "GAP"
    revoke = eng.append(authorization(
        eng, event_id="gap_revoke", idempotency_key="idem-gap-revoke", producer_seq=2,
        event_type="authorization.revoked", revision=2, expected_revision=1,
    ))
    assert revoke["applied"] is True
    assert eng.authorizations()[0]["revoked"] is True
    assert eng.watermarks()["policy_service:authorization@1"]["seq"] == 3
    assert eng.fact("authorization:tsk_w01|cand_w01|preview.merge")["revision"] == 2

    bound = engine(tmp_path / "bound", evidence_bindings={
        ("authorization:tsk_w01|cand_w01|preview.merge", 1, "preview.merge"): "policy://server",
    })
    assert bound.append(authorization(bound, evidence_ref="policy://1"))["applied"] is True
    assert bound.authorizations() == []
    denied_close = bound.append(authorization(
        bound, event_id="evt_bad_close", idempotency_key="idem-bad-close", producer_seq=2,
        event_type="authorization.epoch_closed", epoch_closed=True, closed_through_seq=2,
    ))
    assert denied_close["code"] == "EVIDENCE_MISSING"
    assert bound.watermarks()["policy_service:authorization@1"]["closed"] is False
    assert bound.append(diagnostic(bound))["applied"] is True
    with pytest.raises(ControlError):
        bound.append(task(bound, state="UNKNOWN", event_id="evt_unknown_state", idempotency_key="idem-unknown-state"))
    repaired = bound.append(task(bound, event_id="evt_repaired", idempotency_key="idem-repaired"))
    assert repaired["applied"] is True
    assert bound.watermarks()["hostops:diagnostic@1"]["seq"] == 1
    assert "do not consume producer_seq" in bound.health()["stream_repair"]
    assert STREAM_REPAIR == bound.health()["stream_repair"]
    assert bound.health()["event_outbox_consumer"] == "generic"
    assert bound.health()["event_delivery_disposition"] == "DURABLE_LOCAL"
    assert bound.health()["event_backlog"] >= 1
    assert bound.health()["package_complete"] is True
    assert PACKAGE_COMPLETE is True
    assert FOLLOW_UPS == ()
    assert bound.health()["production_protocol_enabled"] is False


def test_claim_token_is_required_and_a_live_claim_survives_recovery(tmp_path):
    eng = engine(tmp_path)
    payload = report(eng, "ab" * 16, outcome="success", phase="passed")
    assert eng.submit_report(payload, remote=None)["complete"] is False
    event_id = "report-" + payload["run_id"]
    token = eng.outbox._claim(event_id, "github_comment", payload["run_id"], "DURABLE_LOCAL")
    assert isinstance(token, str) and len(token) == 32
    assert eng.outbox._claim(event_id, "github_comment", payload["run_id"], "DURABLE_LOCAL") is None
    assert eng.outbox._set_state(event_id, "github_comment", "DELIVERED", payload["run_id"], None, "forged") is False
    eng.outbox.recover(abandon_claims=True)
    assert eng.outbox._channel_state(event_id, "github_comment") == "OUTBOX"
    assert eng.outbox._set_state(event_id, "github_comment", "DELIVERED", payload["run_id"], None, token) is True
    other = eng.outbox._claim(event_id, "commit_status", payload["run_id"], "DURABLE_LOCAL")
    with eng.store.transaction() as conn:
        conn.execute(
            "UPDATE deliveries SET claim_pid=? WHERE event_id=? AND channel='commit_status'",
            (2**22, event_id),
        )
    eng.outbox.recover(abandon_claims=True)
    assert eng.outbox._channel_state(event_id, "commit_status") == "DELIVERY_UNKNOWN"
    bare = eng.submit_report(
        report(eng, "cd" * 16, outcome="failure", phase="context-failed"),
        remote=lambda _channel, _payload: {"confirmed": True},
    )
    assert {item["state"] for item in bare["receipts"]} == {"DELIVERY_UNKNOWN"}
    assert other


def _over_the_barrier(calls):
    barrier = threading.Barrier(len(calls))

    def run(fn):
        barrier.wait(timeout=5)
        try:
            return ("ok", fn())
        except ControlError as exc:
            return ("err", exc.code)

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(run, calls))


def test_concurrent_same_nonce_different_signed_payloads_commit_once(tmp_path):
    eng = engine(tmp_path)
    nonce = "cd" * 16
    first = sign_event(eng, task(eng, event_id="evt_race_a", idempotency_key="idem-race-a", note="alpha"), nonce=nonce)
    second = sign_event(eng, task(eng, event_id="evt_race_b", idempotency_key="idem-race-b", note="beta"), nonce=nonce)
    assert first["proof"]["nonce"] == second["proof"]["nonce"] == nonce
    assert first["proof"]["payload_hash"] != second["proof"]["payload_hash"]
    results = _over_the_barrier((lambda: eng.append(first), lambda: eng.append(second)))
    oks = [item for item in results if item[0] == "ok" and item[1].get("applied") is True]
    errs = [item for item in results if item[0] == "err"]
    assert len(oks) == 1 and len(errs) == 1
    assert errs[0][1] == "PROOF_REPLAY"
    assert eng.store.count("events") == 1
    assert eng.store.count("consumed_nonces") == 1
    assert eng.store.count("event_outbox") == 1
    assert json.loads((tmp_path / "store" / "auth-state.json").read_text(encoding="utf-8"))["nonces"] == {}


def test_crash_after_commit_keeps_nonce_event_and_authorization(tmp_path):
    eng = engine(tmp_path)
    payload = authorization(eng, event_id="evt_atomic", idempotency_key="idem-atomic")
    (tmp_path / "request.json").write_text(json.dumps(payload), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "app.control_events.crash_probe", "--root", str(tmp_path / "store"),
         "--token", "generation-1", "--mode", "after-commit", "--request", str(tmp_path / "request.json")],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 94, proc.stderr
    reopened = Engine.open(tmp_path / "store", "generation-1")
    assert reopened.store.count("events") == 1
    assert reopened.store.count("consumed_nonces") == 1
    assert reopened.store.count("event_outbox") == 1
    assert reopened.authorizations()[0]["event_id"] == "evt_atomic"
    assert reopened.authorizations()[0]["revoked"] is False
    assert json.loads((tmp_path / "store" / "auth-state.json").read_text(encoding="utf-8"))["nonces"] == {}
    replay = reopened.append(payload)
    assert replay["replayed"] is True
    assert reopened.store.count("events") == 1
    other = sign_event(
        reopened,
        authorization(reopened, event_id="evt_other_nonce", idempotency_key="idem-other-nonce", producer_seq=2),
        nonce=payload["proof"]["nonce"],
    )
    with pytest.raises(ControlError) as rejected:
        reopened.append(other)
    assert rejected.value.code == "PROOF_REPLAY"
    assert reopened.store.count("events") == 1
    assert reopened.authorizations()[0]["event_id"] == "evt_atomic"
    assert reopened.fact("authorization:tsk_w01|cand_w01|preview.merge")["event_id"] == "evt_atomic"


def test_corrupt_auth_state_fails_closed_and_does_not_drop_nonce(tmp_path):
    from app.control_events.auth_state import load_state

    eng = engine(tmp_path)
    payload = task(eng, event_id="evt_keep", idempotency_key="idem-keep")
    assert eng.append(payload)["applied"] is True
    path = tmp_path / "store" / "auth-state.json"
    original = path.read_text(encoding="utf-8")
    assert load_state(tmp_path / "missing-auth.json")["nonces"] == {}
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ControlError) as corrupt:
        load_state(path)
    assert corrupt.value.code == "AUTH_STATE_CORRUPT"
    with pytest.raises(ControlError) as opened:
        Engine.open(tmp_path / "store", "generation-1")
    assert opened.value.code == "AUTH_STATE_CORRUPT"
    path.write_text("null", encoding="utf-8")
    with pytest.raises(ControlError) as nulled:
        Engine.open(tmp_path / "store", "generation-1")
    assert nulled.value.code == "AUTH_STATE_CORRUPT"
    path.write_text(original, encoding="utf-8")
    reopened = Engine.open(tmp_path / "store", "generation-1")
    assert reopened.store.count("events") == 1
    assert reopened.store.count("consumed_nonces") == 1
    assert reopened.append(payload)["replayed"] is True
    stolen = sign_event(
        reopened,
        task(reopened, event_id="evt_stolen", idempotency_key="idem-stolen"),
        nonce=payload["proof"]["nonce"],
    )
    with pytest.raises(ControlError) as rejected:
        reopened.append(stolen)
    assert rejected.value.code == "PROOF_REPLAY"
    assert reopened.store.count("events") == 1


def test_report_and_cursor_writes_commit_the_nonce_with_the_row(tmp_path):
    eng = engine(tmp_path)
    nonce = "ef" * 16
    left = sign_report(eng, report(eng, "11" * 16, outcome="success", phase="passed"), nonce=nonce)
    right = sign_report(eng, report(eng, "22" * 16, outcome="failure", phase="test-failed"), nonce=nonce)
    assert left["proof"]["payload_hash"] != right["proof"]["payload_hash"]
    results = _over_the_barrier((
        lambda: eng.submit_report(left, channels=["github_comment"], remote=None),
        lambda: eng.submit_report(right, channels=["github_comment"], remote=None),
    ))
    oks = [item for item in results if item[0] == "ok"]
    errs = [item for item in results if item[0] == "err"]
    assert len(oks) == 1 and len(errs) == 1
    assert errs[0][1] == "PROOF_REPLAY"
    assert eng.store.count("reports") == 1

    root = tmp_path / "report-crash"
    root.mkdir()
    opened = Engine.open(root, "generation-1")
    ensure_principals(opened)
    submitted = report(opened, "33" * 16, outcome="failure", phase="setup-failed")
    request = tmp_path / "report-request.json"
    request.write_text(json.dumps({"report": submitted, "channels": ["github_comment"]}), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "app.control_events.crash_probe", "--root", str(root),
         "--token", "generation-1", "--mode", "after-report-commit", "--request", str(request)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 94, proc.stderr
    restored = Engine.open(root, "generation-1")
    assert restored.store.count("reports") == 1
    assert restored.store.count("consumed_nonces") == 1
    restored_file = json.loads((root / "report-outbox" / f"{'33' * 16}.json").read_text(encoding="utf-8"))
    assert restored_file["run_id"] == "33" * 16
    assert restored_file["outcome"] == "failure"
    assert restored_file["phase"] == "setup-failed"
    assert restored_file["channels"] == ["github_comment"]
    other = sign_report(
        restored,
        report(restored, "44" * 16, outcome="success", phase="passed"),
        nonce=submitted["proof"]["nonce"],
    )
    with pytest.raises(ControlError) as rejected:
        restored.submit_report(other, channels=["github_comment"], remote=None)
    assert rejected.value.code == "PROOF_REPLAY"
    assert restored.store.count("reports") == 1
    again = restored.submit_report(submitted, channels=["github_comment"], remote=None)
    assert again["run_id"] == "33" * 16
    assert restored.store.count("reports") == 1

    cursor_eng = engine(tmp_path / "cursor")
    cursor_eng.append(diagnostic(cursor_eng, subject_id="subj"))
    before = cursor_eng.store.count("consumed_nonces")
    shared = "ab" * 16
    low = sign_read(
        cursor_eng, "hostops", "diagnostic.cursor", stream_id="hostops:diagnostic",
        object_id="subj", method="POST", expected_revision=0, nonce=shared,
    )
    high = sign_read(
        cursor_eng, "hostops", "diagnostic.cursor", stream_id="hostops:diagnostic",
        object_id="subj", method="POST", expected_revision=1, nonce=shared,
    )
    assert low["proof"]["payload_hash"] != high["proof"]["payload_hash"]
    raced = _over_the_barrier((
        lambda: cursor_eng.commit_cursor(request=low, subject="subj", stream_id="hostops:diagnostic", stream_epoch=1, after_seq=0),
        lambda: cursor_eng.commit_cursor(request=high, subject="subj", stream_id="hostops:diagnostic", stream_epoch=1, after_seq=1),
    ))
    assert len([item for item in raced if item[0] == "ok"]) == 1
    assert [item[1] for item in raced if item[0] == "err"] == ["PROOF_REPLAY"]
    assert cursor_eng.store.count("consumed_nonces") == before + 1
    assert cursor_eng.cursor("hostops:diagnostic", 1) in {0, 1}


def test_generic_event_outbox_claims_exact_receipts_and_backpressure(tmp_path):
    eng = engine(tmp_path, limits=OutboxLimits(max_event_outbox=1))
    first = eng.append(task(eng, event_id="evt_box", idempotency_key="idem-box"))
    assert first["delivery_disposition"] == "DURABLE_LOCAL"
    assert eng.health()["event_backlog"] == 1
    assert eng.health()["event_oldest_pending_at"]
    blocked = task(
        eng, event_id="evt_over", idempotency_key="idem-over", producer_id="fixture_b",
        stream_id="fixture_b:task", producer_seq=1, run_id="run_over",
    )
    with pytest.raises(ControlError) as full:
        eng.append(blocked)
    assert full.value.code == "CAPACITY_EXHAUSTED"
    assert eng.store.count("events") == 1
    assert eng.store.count("consumed_nonces") == 1
    assert eng.store.count("event_outbox") == 1
    vague = eng.deliver_events(lambda _channel, _envelope: {"ok": True})
    assert vague[0]["state"] == "DELIVERY_UNKNOWN"
    assert eng.store.count("events") == 1
    sends = []

    class Remote:
        def __call__(self, channel, envelope):
            sends.append(channel)
            return exact_confirmation(channel, envelope)

        def reconcile(self, channel, envelope):
            return exact_confirmation(channel, envelope)

    exact = eng.deliver_events(Remote())
    assert sends == []
    assert exact[0]["state"] == "DELIVERED"
    assert exact[0]["exact"] is True
    assert confirmation_binds(exact[0], "event_sink", exact[0])
    assert list((tmp_path / "store" / "event-receipts").glob("*.json"))
    assert eng.health()["event_backlog"] == 0

    tiny = engine(tmp_path / "tiny", limits=OutboxLimits(max_event_outbox_bytes=1))
    with pytest.raises(ControlError) as too_small:
        tiny.append(task(tiny, event_id="evt_tiny", idempotency_key="idem-tiny"))
    assert too_small.value.code == "CAPACITY_EXHAUSTED"
    assert tiny.store.count("events") == 0
    assert tiny.store.count("consumed_nonces") == 0

    split = engine(tmp_path / "split", event_channels=("event_sink", "event_mirror"))
    assert split.append(task(split, event_id="evt_split", idempotency_key="idem-split"))["applied"] is True

    def one_bad(channel, envelope):
        if channel == "event_mirror":
            raise BadRecord("mirror record is bad")
        return exact_confirmation(channel, envelope)

    isolated = {item["channel"]: item["state"] for item in split.deliver_events(one_bad)}
    assert isolated == {"event_sink": "DELIVERED", "event_mirror": "QUARANTINED"}
    assert split.store.count("events") == 1
    assert (tmp_path / "split" / "store" / "event-quarantine" / "event_mirror" / "evt_split.json").exists()

    held = engine(tmp_path / "held-claim")
    accepted = held.append(task(held, event_id="evt_claim", idempotency_key="idem-claim"))
    assert accepted["applied"] is True
    token = held.events._claim("evt_claim", "event_sink", "DURABLE_LOCAL")
    assert isinstance(token, str) and len(token) == 32
    assert held.events._claim("evt_claim", "event_sink", "DURABLE_LOCAL") is None
    held.events.recover(abandon_claims=True)
    assert held.events._state("evt_claim", "event_sink") == "OUTBOX"
    assert held.events._set_state("evt_claim", "event_sink", "DELIVERED", None, "forged") is False
    with held.store.transaction() as conn:
        conn.execute(
            "UPDATE event_outbox SET claim_pid=? WHERE event_id='evt_claim' AND channel='event_sink'",
            (2**22,),
        )
    held.events.recover(abandon_claims=True)
    assert held.events._state("evt_claim", "event_sink") == "DELIVERY_UNKNOWN"
    assert held.store.count("events") == 1


def _mark_unknown(store, table: str, event_id: str, channel: str) -> None:
    if table not in {"deliveries", "event_outbox"}:
        raise AssertionError(table)
    with store.transaction() as conn:
        conn.execute(
            f"UPDATE {table} SET state='DELIVERY_UNKNOWN', claim_token=NULL, claim_pid=NULL, claim_expires_at=NULL "
            "WHERE event_id=? AND channel=?",
            (event_id, channel),
        )


def test_after_commit_crash_reopens_and_delivers_the_report(tmp_path):
    root = tmp_path / "gap"
    root.mkdir()
    opened = Engine.open(root, "generation-1")
    ensure_principals(opened)
    run_id = "55" * 16
    submitted = report(opened, run_id, outcome="failure", phase="dependencies-failed", note=SECRET)
    request = tmp_path / "gap-request.json"
    request.write_text(json.dumps({"report": submitted, "channels": ["github_comment", "commit_status"]}), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "app.control_events.crash_probe", "--root", str(root),
         "--token", "generation-1", "--mode", "after-report-commit", "--request", str(request)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 94, proc.stderr
    assert not (root / "report-outbox" / f"{run_id}.json").exists()
    assert not (root / "report-archive" / f"{run_id}.json").exists()
    stored = Store(root / "events.sqlite3")
    with stored.transaction() as conn:
        row = conn.execute(
            "SELECT payload_json, channels_json, payload_hash FROM reports WHERE run_id=?",
            (run_id,),
        ).fetchone()
        deliveries = conn.execute(
            "SELECT channel, state FROM deliveries WHERE run_id=? ORDER BY channel",
            (run_id,),
        ).fetchall()
    assert row is not None and row["payload_json"]
    payload = json.loads(row["payload_json"])
    assert payload["run_id"] == run_id
    assert payload["outcome"] == "failure"
    assert payload["phase"] == "dependencies-failed"
    assert payload["head"] == SHA and payload["base"] == SHA_B
    assert payload["merge"] == SHA_C and payload["tree"] == SHA_D
    assert payload["executor_id"] == "macci-isolated"
    assert json.loads(row["channels_json"]) == ["github_comment", "commit_status"]
    assert payload["channels"] == ["github_comment", "commit_status"]
    assert payload["payload_hash"] == row["payload_hash"]
    assert SECRET not in row["payload_json"]
    assert [(item["channel"], item["state"]) for item in deliveries] == [
        ("commit_status", "DURABLE_LOCAL"),
        ("github_comment", "DURABLE_LOCAL"),
    ]
    restored = Engine.open(root, "generation-1")
    disk = json.loads((root / "report-outbox" / f"{run_id}.json").read_text(encoding="utf-8"))
    assert disk == payload
    seen = []

    def remote(channel, body):
        seen.append((channel, body["run_id"], body["phase"], body["outcome"], body["head"], body["tree"]))
        return confirm(channel, body)

    receipts = restored.deliver_pending(remote)
    assert restored.store.count("reports") == 1
    assert {item[0] for item in seen} == {"commit_status", "github_comment"}
    assert len(seen) == 2
    assert all(item[1] == run_id and item[2] == "dependencies-failed" and item[3] == "failure" and item[4] == SHA and item[5] == SHA_D for item in seen)
    assert {item["state"] for item in receipts} == {"DELIVERED"}
    for item in receipts:
        assert report_binds(item, item["channel"], submitted)
        assert item["complete"] is True and item["persisted"] is True
        assert item["head"] == SHA and item["base"] == SHA_B and item["merge"] == SHA_C and item["tree"] == SHA_D
        assert item["executor_id"] == "macci-isolated" and item["run_id"] == run_id
    archived = json.loads((root / "report-archive" / f"{run_id}.json").read_text(encoding="utf-8"))
    assert archived["phase"] == "dependencies-failed" and archived["channels"] == ["github_comment", "commit_status"]
    assert SECRET not in json.dumps(archived)


def test_event_workers_cannot_claim_across_a_state_change(tmp_path):
    cas = engine(tmp_path / "cas")
    assert cas.append(task(cas, event_id="evt_cas", idempotency_key="idem-cas"))["applied"] is True
    _mark_unknown(cas.store, "event_outbox", "evt_cas", "event_sink")
    assert cas.events._claim("evt_cas", "event_sink", "DURABLE_LOCAL") is None
    assert cas.events._state("evt_cas", "event_sink") == "DELIVERY_UNKNOWN"

    delivered = engine(tmp_path / "delivered")
    assert delivered.append(task(delivered, event_id="evt_stale", idempotency_key="idem-stale"))["applied"] is True
    envelope = delivered.events._load("evt_stale", "event_sink")
    assert envelope["state"] == "DURABLE_LOCAL"
    _mark_unknown(delivered.store, "event_outbox", "evt_stale", "event_sink")
    order = []

    class Already:
        def __call__(self, channel, payload):
            order.append(("send", channel))
            return exact_confirmation(channel, payload)

        def reconcile(self, channel, payload):
            order.append(("reconcile", channel))
            return exact_confirmation(channel, payload)

    receipt = delivered.events._dispatch(envelope, "event_sink", Already(), crash=None)
    assert order == [("reconcile", "event_sink")]
    assert receipt["state"] == "DELIVERED" and receipt["exact"] is True
    assert confirmation_binds(receipt, "event_sink", envelope)
    assert delivered.events._state("evt_stale", "event_sink") == "DELIVERED"

    absent = engine(tmp_path / "absent")
    assert absent.append(task(absent, event_id="evt_absent", idempotency_key="idem-absent"))["applied"] is True
    missing = absent.events._load("evt_absent", "event_sink")
    _mark_unknown(absent.store, "event_outbox", "evt_absent", "event_sink")
    order = []

    class Missing:
        def __call__(self, channel, payload):
            order.append(("send", channel))
            return exact_confirmation(channel, payload)

        def reconcile(self, channel, payload):
            order.append(("reconcile", channel))
            return "absent"

    receipt = absent.events._dispatch(missing, "event_sink", Missing(), crash=None)
    assert order == [("reconcile", "event_sink"), ("send", "event_sink")]
    assert receipt["state"] == "DELIVERED"
    assert confirmation_binds(receipt, "event_sink", missing)
    assert absent.events._state("evt_absent", "event_sink") == "DELIVERED"

    fresh = engine(tmp_path / "fresh")
    assert fresh.append(task(fresh, event_id="evt_local", idempotency_key="idem-local"))["applied"] is True
    assert fresh.events._claim("evt_local", "event_sink", "DELIVERY_UNKNOWN") is None
    assert fresh.events._state("evt_local", "event_sink") == "DURABLE_LOCAL"
    snapshot = fresh.events._load("evt_local", "event_sink")
    snapshot["state"] = "DELIVERY_UNKNOWN"
    order = []

    class StillLocal:
        def __call__(self, channel, payload):
            order.append(("send", channel))
            return exact_confirmation(channel, payload)

        def reconcile(self, channel, payload):
            order.append(("reconcile", channel))
            return exact_confirmation(channel, payload)

    receipt = fresh.events._dispatch(snapshot, "event_sink", StillLocal(), crash=None)
    assert order == [("send", "event_sink")]
    assert receipt["state"] == "DELIVERED"
    assert confirmation_binds(receipt, "event_sink", snapshot)
    assert fresh.events._state("evt_local", "event_sink") == "DELIVERED"


def _lie_about_report_state(outbox, event_id: str, stale: str, mutate) -> None:
    real = outbox._channel_state
    used = {"n": 0}

    def wrapped(current_event: str, channel: str) -> str | None:
        current = real(current_event, channel)
        if used["n"] == 0 and current_event == event_id and current == "DURABLE_LOCAL":
            used["n"] = 1
            mutate(current_event, channel)
            return stale
        return current

    outbox._channel_state = wrapped


def test_report_workers_cannot_claim_across_a_state_change(tmp_path):
    cas = engine(tmp_path / "cas")
    payload = report(cas, "71" * 16, outcome="failure", phase="fetch-failed")
    assert cas.submit_report(payload, channels=["github_comment"], remote=None)["complete"] is False
    event_id = "report-" + payload["run_id"]
    _mark_unknown(cas.store, "deliveries", event_id, "github_comment")
    assert cas.outbox._claim(event_id, "github_comment", payload["run_id"], "DURABLE_LOCAL") is None
    assert cas.outbox._channel_state(event_id, "github_comment") == "DELIVERY_UNKNOWN"

    delivered = engine(tmp_path / "delivered")
    body = report(delivered, "72" * 16, outcome="failure", phase="context-failed")
    assert delivered.submit_report(body, channels=["github_comment"], remote=None)["complete"] is False
    delivered_id = "report-" + body["run_id"]
    _lie_about_report_state(
        delivered.outbox,
        delivered_id,
        "DURABLE_LOCAL",
        lambda current_event, channel: _mark_unknown(delivered.store, "deliveries", current_event, channel),
    )
    order = []

    class Already:
        def __call__(self, channel, payload):
            order.append(("send", channel, payload["phase"], payload["head"]))
            return confirm(channel, payload)

        def reconcile(self, channel, payload):
            order.append(("reconcile", channel, payload["phase"], payload["head"]))
            return bound_confirmation(channel, payload)

    receipts = delivered.deliver_pending(Already())
    assert order == [("reconcile", "github_comment", "context-failed", SHA)]
    assert len(receipts) == 1 and receipts[0]["state"] == "DELIVERED"
    assert report_binds(receipts[0], "github_comment", body)
    assert delivered.outbox._channel_state(delivered_id, "github_comment") == "DELIVERED"

    absent = engine(tmp_path / "absent")
    missing = report(absent, "74" * 16, outcome="success", phase="passed")
    assert absent.submit_report(missing, channels=["github_comment"], remote=None)["complete"] is False
    absent_id = "report-" + missing["run_id"]
    _lie_about_report_state(
        absent.outbox,
        absent_id,
        "DURABLE_LOCAL",
        lambda current_event, channel: _mark_unknown(absent.store, "deliveries", current_event, channel),
    )
    order = []

    class Missing:
        def __call__(self, channel, payload):
            order.append(("send", channel))
            return confirm(channel, payload)

        def reconcile(self, channel, payload):
            order.append(("reconcile", channel))
            return "absent"

    receipts = absent.deliver_pending(Missing())
    assert order == [("reconcile", "github_comment"), ("send", "github_comment")]
    assert len(receipts) == 1 and receipts[0]["state"] == "DELIVERED"
    assert report_binds(receipts[0], "github_comment", missing)

    fresh = engine(tmp_path / "fresh")
    local = report(fresh, "73" * 16, outcome="success", phase="passed")
    assert fresh.submit_report(local, channels=["github_comment"], remote=None)["complete"] is False
    local_id = "report-" + local["run_id"]
    assert fresh.outbox._claim(local_id, "github_comment", local["run_id"], "DELIVERY_UNKNOWN") is None
    assert fresh.outbox._channel_state(local_id, "github_comment") == "DURABLE_LOCAL"
    _lie_about_report_state(fresh.outbox, local_id, "DELIVERY_UNKNOWN", lambda _event, _channel: None)
    order = []

    class StillLocal:
        def __call__(self, channel, payload):
            order.append(("send", channel))
            return confirm(channel, payload)

        def reconcile(self, channel, payload):
            order.append(("reconcile", channel))
            return bound_confirmation(channel, payload)

    receipts = fresh.deliver_pending(StillLocal())
    assert order == [("send", "github_comment")]
    assert len(receipts) == 1 and receipts[0]["state"] == "DELIVERED"
    assert report_binds(receipts[0], "github_comment", local)
    assert fresh.outbox._channel_state(local_id, "github_comment") == "DELIVERED"
