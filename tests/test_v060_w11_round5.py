"""Fifth-round counterexamples for the isolated runtime loop.

Temporary directories only. These tests do not open a production database,
install a host service, or claim a MacCI receipt.
"""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.control_events.historical_auth import historical_authorization
from app.control_joint.actions import ManualActions, execute_isolated
from app.control_joint.isolated_loop import run_isolated_loop
from app.control_lifecycle.flags import HOST_MUTATION, REAL_INSTALL, REAL_UNINSTALL
from app.control_scheduler.scheduler import Scheduler

from tests.test_v060_w11_p0_counterexamples import _body, _principal


class IssuanceAndLoopTests(unittest.TestCase):
    def test_isolated_loop_issues_a_receipt_and_keeps_production_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            report = run_isolated_loop(Path(directory))
            self.assertTrue(report["ok"], report)
            self.assertEqual(report["stages"]["issuance"]["queried"], "applied")
            self.assertEqual(report["stages"]["issuance"]["conflict"], "conflict")
            self.assertFalse(report["production_enabled"])
            self.assertFalse(report["real_install"])
            self.assertFalse(report["real_uninstall"])
            self.assertFalse(report["host_mutation"])
            self.assertFalse(report["macci_receipt"])
            self.assertIsNone(report["public_running_sha"])
            self.assertFalse(report["branch_protection_verified"])
            matrix = report["evidence"]
            self.assertEqual(matrix["counts"]["items"], 74)
            self.assertEqual(matrix["counts"]["evidence"] + matrix["counts"]["blocked"], 74)
            by_id = {item["id"]: item for item in matrix["items"]}
            self.assertEqual(by_id["A11"]["status"], "blocked")
            self.assertIsNone(by_id["A11"]["public_running_sha"])
            self.assertFalse(by_id["A11"]["macci_receipt"])
            for item in matrix["items"]:
                self.assertIn(item["status"], {"evidence", "blocked"})
                self.assertFalse(item["macci_receipt"])
                self.assertIsNone(item["public_running_sha"])
                self.assertFalse(item["production_enabled"])
            for package in ("W04", "W05", "W07", "W09", "W10"):
                self.assertIn(package, report["exercised"])
            self.assertIn("W01", matrix["unconnected_packages"])
            self.assertIn("W02", matrix["unconnected_packages"])
            self.assertIn("GitHub branch protection for main and preview", matrix["unconnected_entries"])
            self.assertIn("execute_isolated deploy", matrix["unconnected_entries"])


class FenceRecoveryTests(unittest.TestCase):
    def test_reserved_in_flight_can_retry_and_entered_without_proof_cannot(self):
        calls = []

        def executor(root, principal, payload):
            del root, principal
            calls.append(payload["operation_id"])
            return {"http": 500, "code": "BOOM", "side_effect": False}

        with tempfile.TemporaryDirectory() as directory:
            actions = ManualActions(Path(directory), executor=executor)
            submitted = actions.submit(_principal(), _body(chain_id="ch"))
            self.assertEqual(submitted["code"], "SIDE_EFFECT_UNKNOWN")
            self.assertIsNone(submitted["side_effect"])
            key_hash = __import__("hashlib").sha256(b"idem-p0").hexdigest()
            connection = sqlite3.connect(actions.path)
            connection.execute(
                "UPDATE manual_actions SET state='IN_FLIGHT', fence_phase='RESERVED', result_json=NULL WHERE key_hash=?",
                (key_hash,),
            )
            connection.commit()
            connection.close()
            recovered = actions.submit(_principal(), _body(chain_id="ch"))
            self.assertEqual(len(calls), 2, recovered)
            connection = sqlite3.connect(actions.path)
            connection.execute(
                "UPDATE manual_actions SET state='IN_FLIGHT', fence_phase='ENTERED', result_json=NULL WHERE key_hash=?",
                (key_hash,),
            )
            connection.commit()
            connection.close()
            blocked = actions.submit(_principal(), _body(chain_id="ch"))
            self.assertEqual(blocked["code"], "SIDE_EFFECT_UNKNOWN")
            self.assertEqual(len(calls), 2)

    def test_isolated_absence_is_recoverable_and_a_blocked_snapshot_is_not(self):
        import app.control_joint.actions as actions_module
        from app.control_scheduler.digest import candidate_digest

        binding = {
            "repo_id": "repo_joint",
            "app_id": "base",
            "task_id": "tsk_w01",
            "chain_id": "ch",
            "candidate_id": "cand",
            "revision": 1,
            "head": "a" * 40,
            "base": "b" * 40,
            "merge": "c" * 40,
            "tree": "d" * 40,
            "round_id": "round-1",
        }
        binding["digest"] = candidate_digest(binding)
        body = _body(chain_id="ch", candidate_id="cand", digest=binding["digest"])

        def fake_binding(action, board):
            del board
            if action == "unblock":
                return dict(binding)
            return None

        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            schedule = Path(directory) / "scheduler"
            schedule.mkdir()
            os.chmod(schedule, 0o700)
            scheduler = Scheduler(schedule, object(), object(), repo_id="repo_joint")
            scheduler.open_version("v060", baseline="a" * 40)
            scheduler.open_task(task_id="tsk_w01", version="v060", app_id="base", worktree="work/tree")
            actions = ManualActions(Path(directory))
            calls = []
            original = Scheduler.intervene
            original_binding = actions_module._binding_for

            def intervene(self, **kwargs):
                calls.append(kwargs.get("action"))
                from app.control_scheduler.errors import SchedulerError
                raise SchedulerError("NOT_CONNECTED", "stopped before a receipt", status=409, extra={"changed": False})

            Scheduler.intervene = intervene
            actions_module._binding_for = fake_binding
            try:
                key_hash = __import__("hashlib").sha256(b"idem-p0").hexdigest()
                first = actions.submit(_principal(), body)
                self.assertEqual(first.get("non_execution_proof"), "w04_absent", first)
                self.assertFalse(first["http_proves_no_side_effect"])
                connection = sqlite3.connect(actions.path)
                connection.execute(
                    "UPDATE manual_actions SET state='IN_FLIGHT', fence_phase='ENTERED', result_json=NULL WHERE key_hash=?",
                    (key_hash,),
                )
                connection.commit()
                connection.close()
                again = actions.submit(_principal(), body)
                self.assertGreaterEqual(len(calls), 2, again)
                os.chmod(schedule, 0o755)
                connection = sqlite3.connect(actions.path)
                connection.execute(
                    "UPDATE manual_actions SET state='IN_FLIGHT', fence_phase='ENTERED', result_json=NULL WHERE key_hash=?",
                    (key_hash,),
                )
                connection.commit()
                connection.close()
                before = len(calls)
                blocked = actions.submit(_principal(), body)
                self.assertIn(blocked["code"], {"SIDE_EFFECT_UNKNOWN", "BLOCKED"})
                self.assertIsNone(blocked.get("non_execution_proof"))
                self.assertEqual(len(calls), before)
            finally:
                Scheduler.intervene = original
                actions_module._binding_for = original_binding
            self.assertIs(actions.executor, execute_isolated)


class SnapshotBudgetTests(unittest.TestCase):
    def test_timeout_page_budget_and_sidecar_are_blocked_not_absence(self):
        import app.control_diagnostics.sqlite_fd as sqlite_fd
        from app.control_diagnostics.safe_io import UnsafePath
        from app.control_diagnostics.sqlite_fd import verified_sqlite_copy
        from app.control_joint.receipts import query_operation_receipt

        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            schedule = Path(directory) / "scheduler"
            schedule.mkdir()
            os.chmod(schedule, 0o700)
            path = schedule / "scheduler.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE rows(id INTEGER PRIMARY KEY, part TEXT)")
            connection.execute("INSERT INTO rows(part) VALUES('kept')")
            connection.commit()
            connection.close()
            wal = Path(str(path) + "-wal")
            wal.write_bytes(b"")
            os.chmod(wal, 0o666)
            with self.assertRaises(UnsafePath) as raised:
                with verified_sqlite_copy(schedule, "scheduler.sqlite3"):
                    pass
            self.assertEqual(raised.exception.kind, "blocked")
            wal.unlink()
            original_pages = sqlite_fd._MAX_SNAPSHOT_PAGES
            original_seconds = sqlite_fd._MAX_SNAPSHOT_SECONDS
            try:
                sqlite_fd._MAX_SNAPSHOT_PAGES = 0
                with self.assertRaises(UnsafePath) as raised:
                    with verified_sqlite_copy(schedule, "scheduler.sqlite3"):
                        pass
                self.assertEqual(raised.exception.kind, "bound")
                sqlite_fd._MAX_SNAPSHOT_PAGES = original_pages
                sqlite_fd._MAX_SNAPSHOT_SECONDS = 0
                with self.assertRaises(UnsafePath) as raised:
                    with verified_sqlite_copy(schedule, "scheduler.sqlite3"):
                        pass
                self.assertEqual(raised.exception.kind, "blocked")
            finally:
                sqlite_fd._MAX_SNAPSHOT_PAGES = original_pages
                sqlite_fd._MAX_SNAPSHOT_SECONDS = original_seconds
            unknown = query_operation_receipt(
                Path(directory), operation_id="op-budget", expected_revision=1, candidate_digest="ab" * 32,
                repo_id="repo_joint", app_id="base", authority="W04",
            )
            self.assertIn(unknown["status"], {"unknown", "absent"})
            self.assertNotEqual(unknown["status"], "applied")


class HistoricalDecisionTests(unittest.TestCase):
    def test_flag_and_stale_signature_do_not_authorize(self):
        scope = dict(repo_id="repo", app_id="base", task_id="task",
                     candidate_id="candidate", operation="preview.merge",
                     stream_id="policy:authorization", stream_epoch=1)
        grant = dict(scope, event_type="authorization.granted", revision=1,
                     producer_seq=1, event_id="grant", _authoritative=True, signature="sig-a")
        unsigned = dict(grant)
        unsigned.pop("signature")
        self.assertEqual(historical_authorization([unsigned])["decision"], "unknown")
        unverified = dict(grant)
        unverified.pop("_authoritative")
        self.assertEqual(historical_authorization([unverified])["decision"], "unknown")
        self.assertEqual(historical_authorization([grant])["decision"], "authorized")
        revoke = dict(grant, event_type="authorization.revoked", event_id="revoke",
                      signature="sig-b", producer_seq=2)
        later_grant = dict(grant, event_id="late", signature="sig-c", producer_seq=3)
        self.assertEqual(historical_authorization([grant, revoke, later_grant])["decision"], "revoked")
        other = dict(later_grant, app_id="other")
        self.assertEqual(historical_authorization([grant, other])["reason"], "mixed_scope")
        reused = dict(grant, revision=2, producer_seq=4, event_id="reused")
        self.assertEqual(historical_authorization([grant, reused])["reason"], "stale_signature")

    def test_signed_event_cannot_change_decision_after_signing(self):
        from tests.test_v060_w01_events import authorization, engine
        from app.control_events.errors import ControlError

        with tempfile.TemporaryDirectory() as directory:
            opened = engine(Path(directory))
            payload = authorization(opened)
            payload["event_type"] = "authorization.revoked"
            with self.assertRaises(ControlError):
                opened.append(payload)
            self.assertEqual(opened.authorizations(), [])

    def test_tampered_persisted_authorization_proof_fails_closed(self):
        from tests.test_v060_w01_events import authorization, engine

        with tempfile.TemporaryDirectory() as directory:
            opened = engine(Path(directory))
            self.assertTrue(opened.append(authorization(opened)).get("applied"))
            self.assertEqual(opened.authorizations()[0]["historical_decision"], "authorized")
            with opened.store.transaction() as connection:
                connection.execute("UPDATE events SET authority_json='{}' WHERE event_id='evt_auth'")
            result = opened.authorizations()[0]
            self.assertEqual(result["historical_decision"], "unknown")
            self.assertTrue(result["revoked"])

    def test_stored_revoked_column_does_not_override_the_event_sequence(self):
        from tests.test_v060_w01_events import authorization, engine

        with tempfile.TemporaryDirectory() as directory:
            opened = engine(Path(directory))
            self.assertEqual(opened.append(authorization(opened)).get("applied"), True)
            with opened.store.transaction() as connection:
                connection.execute("UPDATE authorizations SET revoked=1")
            self.assertFalse(opened.authorizations()[0]["revoked"])
            self.assertEqual(opened.authorizations()[0]["historical_decision"], "authorized")
            revoked = opened.append(authorization(
                opened, event_id="evt_revoke", idempotency_key="idem-round5-revoke", producer_seq=2,
                event_type="authorization.revoked", revision=2, expected_revision=1,
            ))
            self.assertEqual(revoked.get("applied"), True)
            with opened.store.transaction() as connection:
                connection.execute("UPDATE authorizations SET revoked=0")
            self.assertTrue(opened.authorizations()[0]["revoked"])
            self.assertEqual(opened.authorizations()[0]["historical_decision"], "revoked")


class CleanupBoundaryTests(unittest.TestCase):
    def test_synthetic_cleanup_refuses_a_directory_that_is_not_exclusive(self):
        from app.control_joint.w09_driver import APP, SCOPES, _policy, _probe
        from app.control_lifecycle.grant import W02AuthorizationPort
        from app.control_lifecycle.host import SyntheticHost
        from app.control_lifecycle.platform import DIGEST
        from app.control_lifecycle.service import LifecycleService

        self.assertFalse(REAL_UNINSTALL)
        self.assertFalse(REAL_INSTALL)
        self.assertFalse(HOST_MUTATION)
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            os.chmod(workspace, 0o700)
            (workspace / "home").mkdir()
            inventory = workspace / "inventory"
            inventory.mkdir()
            os.chmod(inventory, 0o755)
            owned = inventory / "owned.txt"
            owned.write_text("keep", encoding="utf-8")
            service = LifecycleService(
                workspace, host=SyntheticHost(_probe()), authorizer=W02AuthorizationPort(_policy()),
            )
            grant = service.issue_grant("installer", SCOPES, APP)
            payload = {
                "run_id": "run-boundary",
                "app_id": APP,
                "repo": "acme/app",
                "domain": "app.example.com",
                "port": 18080,
                "track": "main",
                "title": "app",
                "foundation_digest": DIGEST,
                "parameters": {"install_deps": False},
                "resources": [{"resource_id": "owned", "kind": "file", "owner": APP, "relative": "owned.txt"}],
                "resource_ids": ["owned"],
            }
            observed = service.call(grant, "resource.observe", payload, idempotency_key="observe", expected_revision=0)
            self.assertEqual(observed.code, "OK")
            cleaned = service.call(
                grant, "cleanup.synthetic", payload, idempotency_key="cleanup", expected_revision=observed.revision,
            )
            self.assertNotEqual(cleaned.code, "OK")
            self.assertTrue(owned.exists())
            self.assertEqual(owned.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
