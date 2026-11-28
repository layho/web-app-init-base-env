"""Counterexamples for the remaining W11 P0 review.

These tests use temporary directories. They do not open a production database,
install a host service, or claim a MacCI receipt.
"""

import json
import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from app.control_authz.principal import Principal
from app.control_diagnostics.safe_io import UnsafePath, open_root, replace_bytes
from app.control_joint.actions import ManualActions, _named
from app.control_scheduler.scheduler import Scheduler


def _principal(principal_id="fixture"):
    return Principal(
        principal_id=principal_id,
        kind="agent",
        repo_id="repo_joint",
        app_id="base",
        producer_id="fixture",
        stream_id="fixture:task",
        scopes=frozenset({"run", "task:tsk_w01"}),
        trusted_source="agent",
    )


def _body(**extra):
    payload = {
        "action": "unblock",
        "task_id": "tsk_w01",
        "idempotency_key": "idem-p0",
        "expected_revision": 1,
    }
    payload.update(extra)
    return payload


class ManualFenceTests(unittest.TestCase):
    def test_named_fields_are_required(self):
        binding = {"chain_id": "ch", "candidate_id": "cand", "digest": "ab" * 32, "revision": 1}
        missing = _named({"chain_id": None, "candidate_id": "cand", "digest": "ab" * 32, "revision": 1, "successor_id": None}, binding, revision=False)
        self.assertEqual(missing["code"], "OBJECT_MISMATCH")

    def test_another_principal_cannot_bypass_an_unresolved_intent(self):
        calls = []

        def executor(root, principal, payload):
            del root, principal
            calls.append(payload["operation_id"])
            return {"http": 200, "side_effect": True}

        with tempfile.TemporaryDirectory() as directory:
            actions = ManualActions(Path(directory), executor=executor)
            first = actions.submit(_principal("one"), _body())
            self.assertEqual(first["code"], "SIDE_EFFECT_UNKNOWN", first)
            other = actions.submit(_principal("two"), _body(idempotency_key="other-key", expected_revision=9))
            self.assertEqual(other["code"], "SIDE_EFFECT_UNKNOWN", other)
            self.assertEqual(len(calls), 1)
            replay = actions.submit(_principal("one"), _body())
            self.assertTrue(replay["replayed"])
            self.assertEqual(len(calls), 1)

    def test_a_new_revision_does_not_bypass_an_unresolved_intent(self):
        calls = []

        def executor(root, principal, payload):
            del root, principal
            calls.append(payload["expected_revision"])
            return {"http": 200, "side_effect": True}

        with tempfile.TemporaryDirectory() as directory:
            actions = ManualActions(Path(directory), executor=executor)
            actions.submit(_principal(), _body())
            bypass = actions.submit(_principal(), _body(idempotency_key="next-key", expected_revision=2))
            self.assertEqual(bypass["code"], "SIDE_EFFECT_UNKNOWN", bypass)
            self.assertEqual(calls, [1])

    def test_settle_does_not_report_success_when_no_row_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            actions = ManualActions(Path(directory), executor=lambda *args: {"http": 200})
            principal = _principal()
            actions.submit(principal, _body())
            connection = sqlite3.connect(actions.path)
            connection.execute("UPDATE manual_actions SET state='SETTLED', result_json='{\"http\":409,\"code\":\"KEPT\"}'")
            connection.commit()
            connection.close()
            settled = actions._settle(principal.principal_id, __import__("hashlib").sha256(b"idem-p0").hexdigest(), {"http": 200, "code": None, "detail": "should not win"})
            self.assertNotEqual(settled.get("http"), 200)
            self.assertNotEqual(settled.get("detail"), "should not win")


class SchedulerReadTests(unittest.TestCase):
    def test_constructor_does_not_create_or_migrate_and_query_checks_the_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            scheduler = Scheduler(Path(directory), object(), object(), repo_id="repo_a")
            self.assertFalse((Path(directory) / "scheduler.sqlite3").exists())
            with self.assertRaises(ValueError):
                scheduler.query_board(app_id="base", task_ids={"tsk"})
            scheduler.open_version("v060", baseline="a" * 40)
            self.assertTrue((Path(directory) / "scheduler.sqlite3").is_file())
            page = scheduler.query_board(app_id="base", task_ids=set())
            self.assertEqual(page["repo_id"], "repo_a")
            self.assertEqual(page["chains"], [])
            other = Scheduler(Path(directory), object(), object(), repo_id="repo_b")
            with self.assertRaises(ValueError):
                other.query_board(app_id="base", task_ids=set())


class SafeIoTests(unittest.TestCase):
    def test_open_root_rejects_a_symlink_component(self):
        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory) / "real"
            real.mkdir()
            link = Path(directory) / "link"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaises(UnsafePath):
                open_root(link)

    def test_replace_bytes_keeps_both_concurrent_payloads_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            root = open_root(directory)
            errors = []

            def write(payload):
                try:
                    for _ in range(20):
                        replace_bytes(root, "note", payload)
                except Exception as exc:
                    errors.append(exc)

            left = threading.Thread(target=write, args=(b"left-value\n",))
            right = threading.Thread(target=write, args=(b"right-value\n",))
            left.start()
            right.start()
            left.join()
            right.join()
            os.close(root)
            self.assertEqual(errors, [])
            found = (Path(directory) / "note").read_bytes()
            self.assertIn(found, {b"left-value\n", b"right-value\n"})
            leftovers = [name for name in os.listdir(directory) if name.endswith(".tmp")]
            self.assertEqual(leftovers, [])
            info = (Path(directory) / "note").stat()
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)


class TrustedReadTests(unittest.TestCase):
    def test_payload_digest_and_applied_are_not_a_trusted_source(self):
        from app.control_events.trusted_read import query_applied

        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            path = Path(directory) / "events.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE events (
                    event_id TEXT PRIMARY KEY,
                    stream_id TEXT NOT NULL,
                    stream_epoch INTEGER NOT NULL,
                    producer_seq INTEGER NOT NULL,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    applied INTEGER NOT NULL,
                    hold_reason TEXT,
                    epoch_closed INTEGER NOT NULL,
                    authority_json TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE consumed_nonces (
                    slot TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    payload_hash TEXT NOT NULL,
                    operation TEXT NOT NULL
                )
                """
            )
            payload = '{"app_id":"base","event_id":"evt","repo_id":"repo","trusted_source":"agent"}'
            from app.control_events.engine import _identity
            import json
            digest = _identity(json.loads(payload))
            connection.execute(
                """
                INSERT INTO events(
                    event_id, stream_id, stream_epoch, producer_seq, payload_hash, payload_json,
                    received_at, applied, hold_reason, epoch_closed, authority_json
                ) VALUES('evt','fixture:task',1,1,?,?,'2026-11-28T00:00:00Z',1,NULL,0,NULL)
                """,
                (digest, payload),
            )
            connection.execute(
                """
                INSERT INTO events(
                    event_id, stream_id, stream_epoch, producer_seq, payload_hash, payload_json,
                    received_at, applied, hold_reason, epoch_closed, authority_json
                ) VALUES('other','fixture:task',1,2,'not-json','{','2026-11-28T00:00:00Z',1,NULL,0,NULL)
                """
            )
            connection.commit()
            connection.close()
            page = query_applied(Path(directory), repo_id="repo", app_id="base", task_ids={"tsk"}, stream_id=None)
            self.assertEqual(page["items"], [])
            self.assertEqual(page["bad"], 1)

    def test_signature_split_cross_object_and_stale_nonce_are_rejected(self):
        import json

        from app.control_events.engine import _identity
        from app.control_events.trusted_read import query_applied
        from tests.test_v060_w01_events import engine as open_engine
        from tests.test_v060_w01_events import task as task_event

        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            opened = open_engine(Path(directory))
            os.chmod(opened.root, 0o700)
            self.assertEqual(opened.append(task_event(opened)).get("http"), 200)
            other = task_event(opened, event_id="evt_b", producer_seq=2, idempotency_key="idem-b", run_id="run_2")
            self.assertEqual(opened.append(other).get("http"), 200)
            connection = sqlite3.connect(opened.root / "events.sqlite3")
            connection.row_factory = sqlite3.Row
            original = connection.execute("SELECT * FROM events WHERE event_id='evt_task'").fetchone()
            authority = json.loads(original["authority_json"])
            payload = json.loads(original["payload_json"])
            payload["run_id"] = "run_other"
            payload["event_id"] = "evt_split"
            payload["producer_seq"] = 3
            digest = _identity({key: value for key, value in payload.items() if key != "received_at"})
            authority["authorized"] = True
            authority["event_digest"] = digest
            authority["signed"] = {key: payload.get(key) or "" for key in (
                "repo_id", "app_id", "task_id", "candidate_id", "head", "base", "merge", "tree", "stream_id", "producer_id",
            )}
            nonce = connection.execute("SELECT principal_id, expires_at FROM consumed_nonces LIMIT 1").fetchone()
            connection.execute(
                "INSERT INTO consumed_nonces(slot, principal_id, expires_at, payload_hash, operation) VALUES(?,?,?,?,?)",
                ("other-slot", nonce["principal_id"], nonce["expires_at"], digest, "event.append"),
            )
            connection.execute(
                """
                INSERT INTO events(
                    event_id, stream_id, stream_epoch, producer_seq, payload_hash, payload_json,
                    received_at, applied, hold_reason, epoch_closed, authority_json
                ) VALUES('evt_split','fixture:task',1,3,?,?,'2026-11-28T00:00:00Z',1,NULL,0,?)
                """,
                (digest, json.dumps(payload, sort_keys=True), json.dumps(authority)),
            )
            connection.execute(
                "UPDATE consumed_nonces SET payload_hash='stale' WHERE payload_hash=?",
                (original["payload_hash"],),
            )
            connection.commit()
            connection.close()
            page = query_applied(
                opened.root, repo_id=opened.repo_id, app_id="base", task_ids={"tsk_w01"}, stream_id=None,
            )
            self.assertNotIn("evt_split", [item["event_id"] for item in page["items"]])
            self.assertNotIn("evt_task", [item["event_id"] for item in page["items"]])
            self.assertGreaterEqual(page["bad"], 1)


class SqliteSnapshotTests(unittest.TestCase):
    def test_uncheckpointed_wal_rows_are_in_the_snapshot(self):
        from app.control_diagnostics.sqlite_fd import verified_sqlite_copy

        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            path = Path(directory) / "events.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("CREATE TABLE rows(id INTEGER PRIMARY KEY, part TEXT)")
            connection.execute("BEGIN")
            connection.execute("INSERT INTO rows(part) VALUES('left')")
            connection.execute("INSERT INTO rows(part) VALUES('right')")
            connection.commit()
            self.assertNotIn(b"left", path.read_bytes())
            torn = Path(directory) / "torn.sqlite3"
            torn.write_bytes(path.read_bytes())
            torn_connection = sqlite3.connect(torn)
            with self.assertRaises(sqlite3.OperationalError):
                torn_connection.execute("SELECT COUNT(*) FROM rows").fetchone()
            torn_connection.close()
            with verified_sqlite_copy(directory, "events.sqlite3") as copied:
                copied_connection = sqlite3.connect(copied)
                parts = [row[0] for row in copied_connection.execute("SELECT part FROM rows ORDER BY id")]
                self.assertEqual(parts, ["left", "right"])
                self.assertEqual(copied_connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                copied_connection.close()
            connection.close()

    def test_concurrent_wal_writes_and_checkpoints_stay_paired(self):
        from app.control_diagnostics.sqlite_fd import verified_sqlite_copy

        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            path = Path(directory) / "events.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE pairs(k INTEGER, part TEXT)")
            connection.commit()
            connection.close()
            stop = threading.Event()
            errors = []

            def write():
                writer = sqlite3.connect(path, timeout=5)
                writer.execute("PRAGMA journal_mode=WAL")
                number = 0
                try:
                    while not stop.is_set() and number < 40:
                        writer.execute("BEGIN")
                        writer.execute("INSERT INTO pairs(k, part) VALUES(?, 'left')", (number,))
                        writer.execute("INSERT INTO pairs(k, part) VALUES(?, 'right')", (number,))
                        writer.commit()
                        if number % 4 == 0:
                            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        number += 1
                except Exception as exc:
                    errors.append(exc)
                finally:
                    writer.close()

            writer_thread = threading.Thread(target=write)
            writer_thread.start()
            try:
                for _ in range(15):
                    with verified_sqlite_copy(directory, "events.sqlite3") as copied:
                        copied_connection = sqlite3.connect(copied)
                        self.assertEqual(copied_connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                        rows = copied_connection.execute("SELECT k, part FROM pairs").fetchall()
                        copied_connection.close()
                    seen = {}
                    for key, part in rows:
                        seen.setdefault(key, set()).add(part)
                    self.assertTrue(all(parts == {"left", "right"} for parts in seen.values()))
            finally:
                stop.set()
                writer_thread.join()
            self.assertEqual(errors, [])

    def test_read_failures_and_bounds_are_blocked(self):
        from app.control_diagnostics.sqlite_fd import _MAX_SNAPSHOT_BYTES, verified_sqlite_copy
        import app.control_diagnostics.sqlite_fd as sqlite_fd

        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            path = Path(directory) / "events.sqlite3"
            path.write_bytes(b"not a database")
            with self.assertRaises(UnsafePath):
                with verified_sqlite_copy(directory, "events.sqlite3"):
                    pass
            path.unlink()
            link = Path(directory) / "events.sqlite3"
            target = Path(directory) / "other.sqlite3"
            target.write_bytes(b"x")
            link.symlink_to(target.name)
            with self.assertRaises(UnsafePath):
                with verified_sqlite_copy(directory, "events.sqlite3"):
                    pass
            link.unlink()
            target.unlink()
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE rows(id INTEGER PRIMARY KEY)")
            connection.commit()
            connection.close()
            original = sqlite_fd._MAX_SNAPSHOT_BYTES
            sqlite_fd._MAX_SNAPSHOT_BYTES = 8
            try:
                with self.assertRaises(UnsafePath) as raised:
                    with verified_sqlite_copy(directory, "events.sqlite3"):
                        pass
                self.assertEqual(raised.exception.kind, "bound")
            finally:
                sqlite_fd._MAX_SNAPSHOT_BYTES = original
            os.chmod(directory, 0o755)
            with self.assertRaises(UnsafePath) as raised:
                with verified_sqlite_copy(directory, "events.sqlite3"):
                    pass
            self.assertEqual(raised.exception.kind, "blocked")
            self.assertGreater(_MAX_SNAPSHOT_BYTES, 8)


class ReceiptRoutingTests(unittest.TestCase):
    def _scheduler(self, directory, repo_id="repo_joint"):
        schedule = Path(directory) / "scheduler"
        schedule.mkdir()
        os.chmod(schedule, 0o700)
        scheduler = Scheduler(schedule, object(), object(), repo_id=repo_id)
        scheduler.open_version("v060", baseline="a" * 40)
        scheduler.open_task(task_id="tsk_w01", version="v060", app_id="base", worktree="work/tree")
        return schedule

    def test_w04_absence_needs_a_consistent_binding_and_w05_does_not_change_it(self):
        from app.control_joint.receipts import query_operation_receipt

        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            self._scheduler(directory)
            absent = query_operation_receipt(
                Path(directory), operation_id="op-1", expected_revision=1, candidate_digest="ab" * 32,
                repo_id="repo_joint", app_id="base", authority="W04",
            )
            self.assertEqual(absent["status"], "absent")
            operation = Path(directory) / "delivery" / "operation"
            operation.mkdir(parents=True)
            (operation / "op-1.json").write_text(json.dumps({"reconciled": True, "status": "applied"}), encoding="utf-8")
            (operation / "other.json").write_text("{", encoding="utf-8")
            still = query_operation_receipt(
                Path(directory), operation_id="op-1", expected_revision=1, candidate_digest="ab" * 32,
                repo_id="repo_joint", app_id="base", authority="W04",
            )
            self.assertEqual(still["status"], "absent")
            w05 = query_operation_receipt(
                Path(directory), operation_id="op-1", expected_revision=1, candidate_digest="ab" * 32,
                repo_id="repo_joint", app_id="base", authority="W05",
            )
            self.assertEqual(w05["status"], "unknown")
            os.chmod(Path(directory) / "scheduler", 0o755)
            blocked = query_operation_receipt(
                Path(directory), operation_id="op-1", expected_revision=1, candidate_digest="ab" * 32,
                repo_id="repo_joint", app_id="base", authority="W04",
            )
            self.assertEqual(blocked["status"], "unknown")

    def test_w05_requires_a_complete_persisted_receipt(self):
        import hashlib
        import json as json_module

        from app.control_joint.receipts import query_operation_receipt
        from app.control_joint.w05_issue import issue_w05_receipt

        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            arguments = dict(
                operation_id="op-5", expected_revision=2, candidate_digest="cd" * 32,
                repo_id="repo_joint", app_id="base",
            )
            missing = query_operation_receipt(Path(directory), authority="W05", **arguments)
            self.assertEqual(missing["status"], "unknown")
            w04_before = query_operation_receipt(Path(directory), authority="W04", **arguments)
            folder = Path(directory) / "delivery" / "receipt"
            folder.mkdir(parents=True)
            result = {"http": 200, "state": "AUTHORIZED"}
            binding = {
                "app_id": "base",
                "authority": "W05",
                "candidate_digest": "cd" * 32,
                "expected_revision": 2,
                "operation_id": "op-5",
                "receipt_id": "rcpt-5",
                "repo_id": "repo_joint",
                "result": result,
            }
            document = dict(binding)
            document.update({"issued": True, "persisted": True, "status": "applied", "reconciled": False})
            document["binding_digest"] = hashlib.sha256(
                json_module.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            (folder / "op-5.json").write_text(json_module.dumps(document), encoding="utf-8")
            self_hashed = query_operation_receipt(Path(directory), authority="W05", **arguments)
            self.assertEqual(self_hashed["status"], "unknown")
            issued = issue_w05_receipt(Path(directory), result=result, **arguments)
            self.assertEqual(issued["status"], "applied")
            self.assertTrue(issued["wrote"])
            applied = query_operation_receipt(Path(directory), authority="W05", **arguments)
            self.assertEqual(applied["status"], "applied")
            conflict = issue_w05_receipt(
                Path(directory), result={"http": 500, "state": "FAILED"}, **arguments,
            )
            self.assertEqual(conflict["status"], "conflict")
            self.assertFalse(conflict["wrote"])
            still = query_operation_receipt(Path(directory), authority="W05", **arguments)
            self.assertEqual(still["status"], "applied")
            self.assertEqual(still["result"], result)
            w04_after = query_operation_receipt(Path(directory), authority="W04", **arguments)
            self.assertEqual(w04_before["status"], w04_after["status"])


class ManualRecoveryTests(unittest.TestCase):
    def test_success_without_a_receipt_is_unknown_and_a_new_action_cannot_bypass_it(self):
        calls = []

        def executor(root, principal, payload):
            del root, principal
            calls.append((payload["action"], payload.get("candidate_id")))
            return {"http": 200, "code": None, "side_effect": False}

        with tempfile.TemporaryDirectory() as directory:
            actions = ManualActions(Path(directory), executor=executor)
            first = actions.submit(_principal(), _body(chain_id="ch", candidate_id="cand-a"))
            self.assertEqual(first["code"], "SIDE_EFFECT_UNKNOWN", first)
            changed = actions.submit(_principal(), _body(
                action="retry_phase", idempotency_key="other-action", chain_id="ch", candidate_id="cand-b",
            ))
            self.assertEqual(changed["code"], "SIDE_EFFECT_UNKNOWN", changed)
            self.assertEqual(calls, [("unblock", "cand-a")])

    def test_in_flight_retry_does_not_execute_twice_and_rechecks_authorization(self):
        from app.control_authz.errors import AuthzError
        import app.control_joint.actions as actions_module

        calls = []
        started = threading.Event()
        release = threading.Event()

        def executor(root, principal, payload):
            del root, principal, payload
            calls.append("run")
            started.set()
            self.assertTrue(release.wait(2))
            return {"http": 409, "code": "HELD", "side_effect": False}

        phase = {"retry": False, "hits": 0}
        original = actions_module._authorize_action

        def authorize(principal, task_id, operation, scope):
            if phase["retry"]:
                phase["hits"] += 1
                if phase["hits"] >= 2:
                    raise AuthzError("NOT_AUTHORIZED", 403, "revoked", reason="revoked")
            return original(principal, task_id, operation, scope)

        with tempfile.TemporaryDirectory() as directory:
            actions = ManualActions(Path(directory), executor=executor)
            actions_module._authorize_action = authorize
            try:
                worker = threading.Thread(target=lambda: actions.submit(_principal(), _body()))
                worker.start()
                self.assertTrue(started.wait(2))
                same = actions.submit(_principal(), _body())
                other = actions.submit(_principal(), _body(idempotency_key="other-key"))
                release.set()
                worker.join(3)
                self.assertEqual(calls, ["run"])
                self.assertEqual(same["code"], "SIDE_EFFECT_UNKNOWN")
                self.assertEqual(other["code"], "SIDE_EFFECT_UNKNOWN")
                schedule = Path(directory) / "scheduler"
                schedule.mkdir()
                os.chmod(schedule, 0o700)
                scheduler = Scheduler(schedule, object(), object(), repo_id="repo_joint")
                scheduler.open_version("v060", baseline="a" * 40)
                scheduler.open_task(task_id="tsk_w01", version="v060", app_id="base", worktree="work/tree")
                held = actions.submit(_principal(), _body(idempotency_key="held-key", chain_id="ch"))
                self.assertEqual(held["code"], "SIDE_EFFECT_UNKNOWN", held)
                self.assertIsNone(held["side_effect"])
                self.assertFalse(held["http_proves_no_side_effect"])
                connection = sqlite3.connect(actions.path)
                connection.execute("UPDATE manual_actions SET state='PENDING', result_json=NULL WHERE key_hash=?", (
                    __import__("hashlib").sha256(b"held-key").hexdigest(),
                ))
                connection.commit()
                connection.close()
                phase["retry"] = True
                retried = actions.submit(_principal(), _body(idempotency_key="held-key", chain_id="ch"))
                self.assertEqual(retried["code"], "NOT_AUTHORIZED", retried)
                self.assertEqual(calls, ["run", "run"])
            finally:
                actions_module._authorize_action = original
                release.set()


if __name__ == "__main__":
    unittest.main()
