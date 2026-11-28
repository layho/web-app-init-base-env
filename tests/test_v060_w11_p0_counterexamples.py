"""Counterexamples for the remaining W11 P0 review.

These tests use temporary directories. They do not open a production database,
install a host service, or claim a MacCI receipt.
"""

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


if __name__ == "__main__":
    unittest.main()
