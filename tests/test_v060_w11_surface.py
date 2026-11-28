"""Executable joint checks for the W11 control surface.

Temporary directories and isolated services only. These tests do not install
a host service, open a production database, or mint a MacCI receipt.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import COOKIE_NAME, hash_password
from app.control_authz.principal import Principal
from app.control_board.projection import authoritative
from app.control_delivery.ports import (
    BlockedDeploy,
    BlockedEvents,
    BlockedRemote,
    BlockedSchedule,
    FixtureAuth,
    FixtureEvents,
    ServerClock as DeliveryClock,
)
from app.control_delivery.service import DeliveryService
from app.control_diagnostics import (
    BlockedDeploymentPort,
    BlockedEventPort,
    BlockedSchedulePort,
    DiagnosticService,
    FixtureAuthorizationPort,
    Grant,
    Limits,
    ServerClock,
    SideEffectProbe,
)
from app.control_diagnostics.safe_io import proc_fd_path
from app.control_joint.actions import ManualActions, execute_isolated
from app.control_upgrade.service import UpgradeService
from app.db import Base, get_db
from app.main import app
from tests.test_v060_w01_events import engine as open_engine
from tests.test_v060_w01_events import task as task_event

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
SHA_B = "b" * 40


def _client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def db_override():
        with Session() as session:
            yield session

    app.dependency_overrides[get_db] = db_override
    os.environ["APP_ADMIN_USERNAME"] = "admin"
    os.environ["APP_ADMIN_PASSWORD_HASH"] = hash_password("A-very-strong-passphrase-123")
    os.environ["APP_PUBLIC_ORIGIN"] = "https://webapp.example"
    os.environ["APP_COOKIE_SECURE"] = "true"
    os.environ.pop("APP_API_TOKEN", None)
    return TestClient(app, base_url="https://webapp.example"), engine


def _login(client):
    response = client.post(
        "/api/login",
        json={"username": "admin", "password": "A-very-strong-passphrase-123"},
        headers={"Origin": "https://webapp.example"},
    )
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


class DeliveryReadTests(unittest.TestCase):
    def test_task_identity_and_invalid_shape_are_not_projected(self):
        from app.control_delivery.store import Journal
        from app.control_joint.delivery_read import query_tasks

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            journal = Journal(root / "delivery")
            base = {"repo_id": "repo", "app_id": "base", "phase": "claim", "revision": 1}
            long_id = "tsk_" + "a" * 100
            journal.write("task", long_id, {**base, "task_id": long_id})
            journal.write("task", "spoof", {**base, "task_id": long_id})
            journal.write("task", "invalid", {**base, "task_id": ["invalid"]})
            journal.write("task", "bad_revision", {**base, "task_id": "bad_revision", "revision": False})
            page = query_tasks(root, repo_id="repo", app_id="base",
                               task_ids={long_id, "bad_revision"})
            self.assertEqual(set(page["items"]), {long_id})
            self.assertEqual(page["bad"], 3)
            self.assertFalse(page["items"][long_id]["authoritative"])

    def test_malformed_long_name_does_not_break_continuation(self):
        from app.control_joint.delivery_read import query_tasks, DeliveryReadError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            delivery = root / "delivery"
            folder = delivery / "task"
            folder.mkdir(parents=True)
            (delivery / ".journal.lock").touch(mode=0o600)
            for index in range(199):
                (folder / f"a{index:03}.json").write_text("{}", encoding="utf-8")
            invalid = "b" * 210 + ".json"
            (folder / invalid).write_text("{}", encoding="utf-8")
            (folder / "z.json").write_text(json.dumps({
                "repo_id": "repo", "app_id": "base", "task_id": "z",
                "revision": 1, "phase": "claim",
            }), encoding="utf-8")
            args = dict(repo_id="repo", app_id="base", task_ids={"z"})
            first = query_tasks(root, **args)
            self.assertEqual(first["next_cursor"], invalid)
            self.assertEqual(first["bad"], 1)
            second = query_tasks(root, after=first["next_cursor"], **args)
            self.assertEqual(set(second["items"]), {"z"})
            self.assertFalse(second["truncated"])
            for cursor in ("../z.json", "a\\x00.json", "x" * 251 + ".json"):
                with self.subTest(cursor=cursor), self.assertRaises(DeliveryReadError):
                    query_tasks(root, after=cursor, **args)

    def test_board_delivery_rejects_dangling_symlink(self):
        from app.control_joint.surface import _delivery

        principal = Principal(principal_id="reader", kind="agent", repo_id="repo",
                              app_id="base", producer_id="reader", stream_id="reader:task",
                              scopes=frozenset({"event.read", "task:z"}), trusted_source="agent")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(_delivery(root, principal, {"z"}), ({}, "missing"))
            (root / "delivery").symlink_to(root / "missing")
            self.assertEqual(_delivery(root, principal, {"z"}), ({}, "blocked"))

    def test_sparse_page_has_continuation_even_without_visible_rows(self):
        from app.control_delivery.store import Journal
        from app.control_joint.delivery_read import query_tasks

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            journal = Journal(root / "delivery")
            for index in range(201):
                key = f"tsk_{index:03}"
                journal.write("task", key, {"task_id": key, "repo_id": "repo",
                                          "app_id": "base", "phase": "claim", "revision": 1})
            args = dict(repo_id="repo", app_id="base", task_ids={"tsk_200"})
            first = query_tasks(root, **args)
            self.assertEqual(first["items"], {})
            self.assertTrue(first["truncated"])
            second = query_tasks(root, after=first["next_cursor"], **args)
            self.assertEqual(set(second["items"]), {"tsk_200"})
            self.assertFalse(second["truncated"])


class SurfaceTests(unittest.TestCase):
    def tearDown(self):
        app.dependency_overrides.clear()
        os.environ.pop("V060_CONTROL_ROOT", None)
        os.environ.pop("V060_JOINT_EXECUTE", None)

    def test_route_requires_a_session_and_projects_only_the_grant(self):
        client, database = _client()
        try:
            denied = client.get("/api/v1/control/board")
            self.assertEqual(denied.status_code, 401)
            csrf = _login(client)
            missing = client.get("/api/v1/control/board")
            self.assertEqual(missing.status_code, 200, missing.text)
            self.assertEqual(missing.json()["state"], "not_configured")
            self.assertEqual(missing.json()["items"], [])
            self.assertEqual(missing.headers["cache-control"], "no-store")

            with tempfile.TemporaryDirectory() as directory:
                os.chmod(directory, 0o700)
                opened = open_engine(Path(directory))
                root = Path(opened.root)
                os.chmod(root, 0o700)
                appended = opened.append(task_event(opened))
                self.assertEqual(appended.get("http"), 200, appended)
                self._insert_foreign_event(root / "events.sqlite3")
                self._schedule(root, opened.repo_id)
                self._diagnostic(root, opened.repo_id)
                (root / "authorization.json").write_text(json.dumps({
                    "reader_username": "admin",
                    "principal_id": "fixture",
                    "kind": "agent",
                    "repo_id": opened.repo_id,
                    "app_id": "base",
                    "producer_id": "fixture",
                    "stream_id": "fixture:task",
                    "scopes": ["event.read", "run", "task:tsk_w01"],
                }), encoding="utf-8")
                os.environ["V060_CONTROL_ROOT"] = str(root)
                body = client.get("/api/v1/control/board")
                self.assertEqual(body.status_code, 200, body.text)
                payload = body.json()
                self.assertEqual(payload["state"], "available")
                self.assertFalse(payload["side_effect"])
                self.assertEqual([item["task_id"] for item in payload["items"]], ["tsk_w01"])
                self.assertEqual(payload["items"][0]["owner"]["evidence"], "missing")
                self.assertEqual(payload["items"][0]["recorded_at"], "2026-11-27T10:00:00Z")
                self.assertTrue(payload["history"][0]["authoritative"])
                self.assertNotIn("evt_foreign", [row["event_id"] for row in payload["history"]])
                self.assertTrue(any(item["display"] == "compile failed" for item in payload["failures"]))
                enabled = [item["id"] for item in payload["actions"] if item["enabled"]]
                connected = [item["id"] for item in payload["actions"] if item["connected_executor"]]
                self.assertIn("unblock", enabled)
                self.assertIn("unblock", connected)
                self.assertNotIn("merge", enabled)
                self.assertNotIn("nudge", enabled)
                self.assertTrue(all(item["connected_executor"] for item in payload["actions"] if item["enabled"]))

                blocked = client.post(
                    "/api/v1/control/board/actions",
                    json={"action": "merge", "task_id": "tsk_w01", "idempotency_key": "merge-1", "expected_revision": 0},
                    headers={"Origin": "https://webapp.example", "X-CSRF-Token": csrf},
                )
                self.assertEqual(blocked.status_code, 403, blocked.text)
                self.assertFalse(blocked.json()["accepted_as_success"])
                self.assertFalse(blocked.json()["side_effect"])

                unblock = next(item for item in payload["actions"] if item["id"] == "unblock")
                first = client.post(
                    "/api/v1/control/board/actions",
                    json={
                        "action": "unblock",
                        "task_id": "tsk_w01",
                        "idempotency_key": "route-secret-key",
                        "expected_revision": unblock["revision"],
                        "chain_id": unblock["chain_id"],
                        "candidate_id": unblock["candidate_id"],
                        "digest": unblock["digest"],
                    },
                    headers={"Origin": "https://webapp.example", "X-CSRF-Token": csrf},
                )
                self.assertNotEqual(first.json().get("code"), "IDEMPOTENCY_PAYLOAD_MISMATCH")
                self.assertFalse(first.json()["accepted_as_success"])
                second = client.post(
                    "/api/v1/control/board/actions",
                    json={
                        "action": "unblock",
                        "task_id": "tsk_w01",
                        "idempotency_key": "route-secret-key",
                        "expected_revision": unblock["revision"] + 4,
                        "chain_id": unblock["chain_id"],
                        "candidate_id": unblock["candidate_id"],
                        "digest": unblock["digest"],
                    },
                    headers={"Origin": "https://webapp.example", "X-CSRF-Token": csrf},
                )
                self.assertEqual(second.status_code, 409, second.text)
                self.assertEqual(second.json()["code"], "IDEMPOTENCY_PAYLOAD_MISMATCH")
                self.assertNotIn(b"route-secret-key", (root / "manual-actions.sqlite3").read_bytes())
        finally:
            client.close()
            database.dispose()

    def test_unknown_side_effect_is_not_success_and_is_not_retried(self):
        calls = []

        def executor(root, principal, payload):
            del root, principal
            calls.append(payload["action"])
            return {"http": 200, "side_effect": True, "state": "SUCCEEDED", "code": None}

        with tempfile.TemporaryDirectory() as directory:
            actions = ManualActions(Path(directory), executor=executor)
            principal = _agent()
            body = {"action": "nudge", "task_id": "tsk_w01", "idempotency_key": "unknown-key", "expected_revision": 1}
            first = actions.submit(principal, body)
            self.assertEqual(first["code"], "SIDE_EFFECT_UNKNOWN")
            self.assertFalse(first["accepted_as_success"])
            replay = actions.submit(principal, body)
            self.assertTrue(replay["replayed"])
            self.assertEqual(calls, ["nudge"])
            other_key = dict(body)
            other_key["idempotency_key"] = "another-key"
            blocked = actions.submit(principal, other_key)
            self.assertEqual(blocked["code"], "SIDE_EFFECT_UNKNOWN")
            self.assertEqual(calls, ["nudge"])

    def test_closed_actions_do_not_switch_a_service(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            result = execute_isolated(Path(directory), _agent(), {
                "action": "deploy",
                "task_id": "tsk_w01",
                "expected_revision": 0,
                "evidence_ref": "",
            })
            self.assertEqual(result["code"], "PRODUCTION_CLOSED")
            self.assertFalse(result["side_effect"])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_multi_object_cas_keeps_both_objects_on_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            service = DeliveryService(
                directory,
                auth=FixtureAuth(),
                events=FixtureEvents(),
                schedule=BlockedSchedule(),
                deploy=BlockedDeploy(),
                remote=BlockedRemote(),
                clock=DeliveryClock(),
            )
            actor = {"kind": "test", "id": "fixture", "proof_ref": "test-proof://w05/fixture"}
            scope = {"repo": "layho/web-app-init-base-env", "task_id": "tsk_w05"}
            claimed = service.claim_task(
                actor=actor,
                scope=scope,
                task_id="tsk_w05",
                feedback_ids=[],
                chains=[_chain("ch_a", "a"), _chain("ch_b", "c")],
                idempotency_key="claim-two",
                expected_revision=0,
            )
            self.assertTrue(claimed.ok, claimed.as_dict())
            task = service.store.read("task", "tsk_w05")
            first = service.store.read("chain", "tsk_w05__ch_a")
            second = service.store.read("chain", "tsk_w05__ch_b")
            changed_task = dict(task)
            changed_task["revision"] = task["revision"] + 1
            changed_chain = dict(first)
            changed_chain["revision"] = first["revision"] + 1
            conflict = service._commit_local(
                {"event_id": None, "idem_key": None, "payload_hash": "digest"},
                [
                    ("task", "tsk_w05", changed_task, task["revision"]),
                    ("chain", "tsk_w05__ch_a", changed_chain, 0),
                ],
                {"task_id": "tsk_w05"},
                "claim",
            )
            self.assertIsNotNone(conflict)
            self.assertEqual(conflict.code, "CONFLICT_REVISION")
            self.assertEqual(service.store.read("task", "tsk_w05")["revision"], task["revision"])
            self.assertEqual(service.store.read("chain", "tsk_w05__ch_a")["revision"], first["revision"])
            self.assertEqual(service.store.read("chain", "tsk_w05__ch_b")["revision"], second["revision"])
            committed = service._commit_local(
                {"event_id": None, "idem_key": None, "payload_hash": "digest-2"},
                [
                    ("task", "tsk_w05", changed_task, task["revision"]),
                    ("chain", "tsk_w05__ch_a", changed_chain, first["revision"]),
                ],
                {"task_id": "tsk_w05"},
                "claim",
            )
            self.assertIsNone(committed)
            self.assertEqual(service.store.read("task", "tsk_w05")["revision"], task["revision"] + 1)
            self.assertEqual(service.store.read("chain", "tsk_w05__ch_a")["revision"], first["revision"] + 1)
            self.assertEqual(service.store.read("chain", "tsk_w05__ch_b")["revision"], second["revision"])

    def test_diagnostic_idempotency_is_a_service_counterexample(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "journal"
            clock = ServerClock(datetime(2026, 11, 28, 1, tzinfo=timezone.utc))
            authz = FixtureAuthorizationPort([
                Grant("fixture-base", "base", "diagnostics.ingest"),
            ], clock)
            service = DiagnosticService.open(
                root,
                clock=clock,
                authz=authz,
                events=BlockedEventPort(),
                schedule=BlockedSchedulePort(),
                deployment=BlockedDeploymentPort(),
                probe=SideEffectProbe(),
                limits=Limits(),
            )
            principal = {"kind": "test", "id": "fixture-base", "proof_ref": "test-proof://w06/base", "app_id": "base"}
            event = _diagnostic_event("visible-idempotency-key", "first")
            stored = service.ingest_event(principal, "base", event)
            self.assertEqual(stored["http"], 200, stored)
            changed = _diagnostic_event("visible-idempotency-key", "second")
            mismatch = service.ingest_event(principal, "base", changed)
            self.assertEqual(mismatch["code"], "IDEMPOTENCY_PAYLOAD_MISMATCH")
            text = "\n".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in root.rglob("*")
                if path.is_file() and not path.is_symlink()
            )
            self.assertNotIn("visible-idempotency-key", text)
            self.assertEqual(text.count("first"), 1)
            self.assertNotIn("second", text)

    def test_fresh_source_uses_observed_at_and_darwin_fd_path_is_distinct(self):
        service = UpgradeService.__new__(UpgradeService)
        service.clock = ServerClock(datetime(2026, 11, 28, tzinfo=timezone.utc))
        fresh = {"source": "server_manifest", "observed_at": "2026-11-28T00:00:00+00:00", "stale": True}
        stale = {"source": "server_manifest", "observed_at": "2020-01-01T00:00:00+00:00", "stale": False}
        missing = {"source": "server_manifest", "observed_at": None, "stale": False}
        service._mark_freshness(fresh)
        service._mark_freshness(stale)
        service._mark_freshness(missing)
        self.assertFalse(fresh["stale"])
        self.assertTrue(stale["stale"])
        self.assertTrue(missing["stale"])
        self.assertEqual(proc_fd_path(4, "darwin"), "/dev/fd/4")
        self.assertEqual(proc_fd_path(4, "linux"), "/proc/self/fd/4")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note"
            path.write_text("darwin-path", encoding="utf-8")
            descriptor = os.open(path, os.O_RDONLY)
            try:
                linked = os.stat(proc_fd_path(descriptor))
                self.assertEqual(linked.st_ino, os.fstat(descriptor).st_ino)
            finally:
                os.close(descriptor)
        receipt = {
            "kind": "read",
            "authority": "W01",
            "event_id": "evt-auth",
            "payload_digest": "abc",
            "stream_id": "policy_service:authorization",
            "stream_epoch": 1,
            "producer_seq": 1,
            "server_source": "policy_service",
            "fact_state": "ACCEPTED",
            "evidence": "verified",
            "received_at": "2026-11-28T01:00:00Z",
        }
        event = {
            "schema_version": 2,
            "event_id": "evt-auth",
            "event_type": "authorization.granted",
            "event_class": "authorization",
            "payload_digest": "abc",
            "stream_id": "policy_service:authorization",
            "stream_epoch": 1,
            "producer_seq": 1,
            "trusted_source": "policy_service",
            "read_receipt": receipt,
        }
        self.assertTrue(authoritative(event))
        event["read_receipt"] = {key: value for key, value in receipt.items() if key != "payload_digest"}
        self.assertFalse(authoritative(event))

    def test_adapters_execute_only_inside_an_isolated_temporary_root(self):
        env = os.environ.copy()
        env.pop("V060_JOINT_EXECUTE", None)
        env.pop("V060_CONTROL_ROOT", None)
        denied = subprocess.run(
            [sys.executable, "deploy/app-init/app_init.py", "--control-adapter", "status"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(denied.returncode, 2, denied.stderr)
        self.assertIn("BLOCKED", denied.stderr)
        shell_denied = subprocess.run(
            ["bash", "deploy/macmini/manage.sh", "control-journal", "record"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(shell_denied.returncode, 2, shell_denied.stderr)
        self.assertIn("BLOCKED", shell_denied.stderr)
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            allowed = env.copy()
            allowed["V060_JOINT_EXECUTE"] = "isolated"
            allowed["V060_CONTROL_ROOT"] = directory
            planned = subprocess.run(
                [sys.executable, "deploy/app-init/app_init.py", "--control-adapter", "plan"],
                cwd=ROOT, env=allowed, capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)
            plan = json.loads((Path(directory) / "lifecycle-plan.json").read_text(encoding="utf-8"))
            self.assertFalse(plan["install"])
            self.assertFalse(plan["service_switch"])
            recorded = subprocess.run(
                ["bash", "deploy/macmini/manage.sh", "update"],
                cwd=ROOT, env=allowed, capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(recorded.returncode, 0, recorded.stderr)
            journal = (Path(directory) / "control-journal.tsv").read_text(encoding="utf-8")
            self.assertIn("record", journal)
            self.assertIn("manage.sh", journal)
            self.assertFalse((ROOT / ".webapp-deploy.lock").exists())

    def _insert_foreign_event(self, path):
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM events WHERE applied=1 LIMIT 1").fetchone()
        payload = json.loads(row["payload_json"])
        payload["event_id"] = "evt_foreign"
        payload["app_id"] = "other"
        payload["producer_seq"] = 2
        from app.control_events.engine import _identity
        sealed = {key: value for key, value in payload.items() if key != "received_at"}
        connection.execute(
            """
            INSERT INTO events(
                event_id, stream_id, stream_epoch, producer_seq, payload_hash, payload_json,
                received_at, applied, hold_reason, epoch_closed
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "evt_foreign", row["stream_id"], row["stream_epoch"], 2, _identity(sealed),
                json.dumps(payload), row["received_at"], 1, None, 0,
            ),
        )
        connection.commit()
        connection.close()

    def _schedule(self, root, repo_id):
        from app.control_scheduler.scheduler import Scheduler

        schedule = root / "scheduler"
        schedule.mkdir(mode=0o700)
        os.chmod(schedule, 0o700)
        scheduler = Scheduler(schedule, object(), object(), repo_id=repo_id)
        scheduler.open_version("v060", baseline=SHA)
        scheduler.open_task(task_id="tsk_w01", version="v060", app_id="base", worktree="work/tree")
        with scheduler.store.transaction() as db:
            db.execute("UPDATE tasks SET acceptance_signer=? WHERE task_id=?", ("alice", "tsk_w01"))
            db.execute(
                """
                INSERT INTO chains(
                    chain_id, task_id, version, pr_line, worktree, files_json, dependencies_json,
                    priority, created, state, queue, candidate_id, head, base, merge_sha, tree,
                    revision, evidence_json, remote_status, remote_seq, spec_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "ch_w01", "tsk_w01", "v060", "pr_w01", "work/tree", "[]", "[]",
                    0, 1, "OPEN", "HUMAN_HOLD", "cand_w01", SHA, SHA_B, "c" * 40, "d" * 40,
                    0, "{}", "NONE", 0, "{}",
                ),
            )
            db.execute(
                """
                INSERT INTO rounds(
                    round_id, version, chain_id, task_id, phase, candidate_id, idempotency_key,
                    intent_json, dispatch, run_id, attempt, generation, revision, auth_state,
                    evidence_ref, created
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "rnd_w01", "v060", "ch_w01", "tsk_w01", "develop", "cand_w01", "idem-round",
                    "{}", "CONFIRMED", "run_1", 1, 1, 0, "BLOCKED", "", 1,
                ),
            )

    def _diagnostic(self, root, repo_id):
        clock = ServerClock(datetime(2026, 11, 28, 1, tzinfo=timezone.utc))
        authz = FixtureAuthorizationPort([
            Grant("fixture-base", "base", "diagnostics.ingest"),
        ], clock)
        service = DiagnosticService.open(
            root / "diagnostics",
            clock=clock,
            authz=authz,
            events=BlockedEventPort(),
            schedule=BlockedSchedulePort(),
            deployment=BlockedDeploymentPort(),
            probe=SideEffectProbe(),
            limits=Limits(),
        )
        stored = service.ingest_event(
            {"kind": "test", "id": "fixture-base", "proof_ref": "test-proof://w06/base", "app_id": "base"},
            "base",
            _diagnostic_event("diag-surface", "compile failed", severity="error", repo_id=repo_id),
        )
        self.assertEqual(stored["http"], 200, stored)


def _agent():
    return Principal(
        principal_id="fixture",
        kind="agent",
        repo_id="repo_joint",
        app_id="base",
        producer_id="fixture",
        stream_id="fixture:task",
        scopes=frozenset({"run", "task:tsk_w01"}),
        trusted_source="agent",
    )


def _chain(chain_id, mark):
    return {
        "chain_id": chain_id,
        "pr_number": 11,
        "required": True,
        "head": mark * 40,
        "base": SHA_B,
        "merge": "c" * 40,
        "tree": "d" * 40,
    }


def _diagnostic_event(key, summary, severity="info", repo_id=None):
    event = {
        "schema_version": 2,
        "event_class": "diagnostic",
        "channel": "diagnostic",
        "stream_id": "fixture:diagnostic",
        "stream_epoch": 1,
        "producer_seq": 1,
        "occurred_at": "2026-11-28T01:00:00+00:00",
        "idempotency_key": key,
        "app_id": "base",
        "subject_id": "tsk_w01",
        "task_id": "tsk_w01",
        "run_id": "run_1",
        "candidate_id": "cand_1",
        "severity": severity,
        "diagnostic_class": "log",
        "body": {"summary": summary},
    }
    if repo_id is not None:
        event["repo_id"] = repo_id
    return event


if __name__ == "__main__":
    unittest.main()
