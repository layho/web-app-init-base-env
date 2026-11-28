"""Joint checks for the W11 candidate. Temporary directories only.

These tests do not produce a MacCI receipt, a public running SHA, or a
production fault. BLOCKED means the environment named in the assertion is
absent.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.control_authz.principal import Principal
from app.control_board.projection import authoritative, fold
from app.control_diagnostics.safe_io import UnsafePath, proc_fd_path, read_bounded
from app.control_joint.actions import ManualActions
from app.control_joint.capability import host_capability
from app.control_joint.surface import read_board


ROOT = Path(__file__).resolve().parents[1]
ITEMS = ROOT / "tests" / "fixtures" / "v060" / "w00" / "items.json"
BLOCKED = {"F11", "U09", "S10", "D10", "A12", "I12", "R10"}


class JointTests(unittest.TestCase):
    def test_board_route_is_read_only_and_has_no_demo_rows(self):
        os.environ.pop("V060_CONTROL_ROOT", None)
        body = read_board("admin")
        self.assertFalse(body["side_effect"])
        self.assertFalse(body["production_enabled"])
        self.assertFalse(body["preview_merge_authorized"])
        self.assertFalse(body["main_release_authorized"])
        self.assertEqual(body["items"], [])
        self.assertEqual(body["history"], [])
        self.assertEqual(body["state"], "not_configured")
        self.assertEqual(body["macci"]["status"], "BLOCKED")
        self.assertIn("MacCI receipt for this exact tree", body["macci"]["missing"])

    def test_host_capability_does_not_downgrade(self):
        report = host_capability()
        self.assertEqual(report["status"], "BLOCKED")
        self.assertFalse(report["host_downgrade"])
        self.assertIsNone(report["public_running_sha"])
        self.assertFalse(report["macci_receipt"])

    def test_platform_fd_paths_differ(self):
        self.assertEqual(proc_fd_path(3, "linux"), "/proc/self/fd/3")
        self.assertEqual(proc_fd_path(3, "darwin"), "/dev/fd/3")
        with self.assertRaises(UnsafePath):
            proc_fd_path(3, "windows")

    def test_fifo_open_does_not_block(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            fifo = Path(directory) / "pipe"
            os.mkfifo(fifo)
            root = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                with self.assertRaises(UnsafePath):
                    read_bounded(root, "pipe", 32)
            finally:
                os.close(root)

    def test_incomplete_read_receipt_is_not_authority(self):
        event = {
            "event_id": "evt-1",
            "event_type": "run.started",
            "event_class": "task",
            "fact_state": "ACCEPTED",
            "evidence": "verified",
            "trusted_source": "agent",
            "stream_id": "s",
            "stream_epoch": 1,
            "producer_seq": 1,
            "read_receipt": {
                "kind": "read",
                "authority": "W01",
                "event_id": "evt-1",
                "server_source": "agent",
                "fact_state": "ACCEPTED",
                "evidence": "verified",
            },
        }
        self.assertFalse(authoritative(event))

    def test_same_sequence_adopts_neither_side(self):
        left = _event("evt-a", 1, "develop")
        right = _event("evt-b", 1, "review")
        folded = fold([left, right])
        self.assertEqual(folded["applied"], [])
        reasons = {item["reason"] for item in folded["held"]}
        self.assertIn("same_seq", reasons)

    def test_rejection_is_not_an_acceptance(self):
        from tests.test_v060_w02_identity import BASE, HEAD, MERGE, REPO, TASK, TREE, standard_world

        world = standard_world()
        candidate = world.svc.register_candidate(repo=REPO, task=TASK, head=HEAD, base=BASE, merge=MERGE, tree=TREE)
        fields = dict(
            candidate_id=candidate, head=HEAD, base=BASE, merge=MERGE, tree=TREE, object_id=candidate,
            credential_scope="accept",
        )
        rejected, _ = world.post("release.reject", "accept-1", **fields)
        self.assertEqual(rejected.status_code, 200, rejected.text)
        publish, _ = world.post(
            "release.publish", "publisher-1", credential_scope="publish", credential_generation=1,
            candidate_id=candidate, head=HEAD, base=BASE, merge=MERGE, tree=TREE, object_id=candidate,
        )
        self.assertEqual(publish.json()["code"], "ACCEPTANCE_REJECTED")
        self.assertEqual(publish.json()["reason"], "acceptance_rejected")
        self.assertFalse(publish.json()["allow"])

    def test_tracker_keeps_environment_items_blocked(self):
        payload = json.loads(ITEMS.read_text(encoding="utf-8"))
        self.assertEqual(payload["counts"]["items"], 74)
        self.assertEqual({item["id"] for item in payload["items"]}, _tracker_ids())
        tracker = json.loads((ROOT / "tests" / "fixtures" / "v060" / "w11" / "tracker.json").read_text(encoding="utf-8"))
        self.assertEqual(len(tracker["items"]), 74)
        blocked = {item["id"] for item in tracker["items"] if item["integration_status"] == "BLOCKED"}
        self.assertEqual(blocked, BLOCKED)
        for item in tracker["items"]:
            self.assertNotEqual(item["integration_status"], "PRODUCTION_ACCEPTED")
            self.assertFalse(item["macci_receipt"])
            self.assertIsNone(item["public_running_sha"])

    def test_manual_action_idempotency_rejects_a_changed_payload(self):
        calls = []

        def executor(root, principal, payload):
            del root, principal
            calls.append(payload["expected_revision"])
            return {"http": 202, "code": None, "detail": "requested", "side_effect": False}

        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            actions = ManualActions(Path(directory), executor=executor)
            principal = Principal(
                principal_id="fixture",
                kind="agent",
                repo_id="repo_joint",
                app_id="base",
                producer_id="fixture",
                stream_id="fixture:task",
                scopes=frozenset({"run", "task:tsk_w01"}),
                trusted_source="agent",
            )
            raw = "idem-visible-secret"
            first = actions.submit(principal, {
                "action": "unblock",
                "task_id": "tsk_w01",
                "idempotency_key": raw,
                "expected_revision": 1,
            })
            self.assertEqual(first["code"], "SIDE_EFFECT_UNKNOWN", first)
            self.assertEqual(first["http"], 409)
            self.assertFalse(first["accepted_as_success"])
            mismatch = actions.submit(principal, {
                "action": "unblock",
                "task_id": "tsk_w01",
                "idempotency_key": raw,
                "expected_revision": 2,
            })
            self.assertEqual(mismatch["code"], "IDEMPOTENCY_PAYLOAD_MISMATCH")
            self.assertFalse(mismatch["forwarded"])
            self.assertEqual(calls, [1])
            replay = actions.submit(principal, {
                "action": "unblock",
                "task_id": "tsk_w01",
                "idempotency_key": raw,
                "expected_revision": 1,
            })
            self.assertTrue(replay["replayed"])
            self.assertEqual(calls, [1])
            stored = (Path(directory) / "manual-actions.sqlite3").read_bytes()
            self.assertNotIn(raw.encode(), stored)


def _event(event_id, seq, stage):
    return {
        "event_id": event_id,
        "schema_version": 2,
        "event_type": "run.started",
        "event_class": "task",
        "repo_id": "layho/web-app-init-base-env",
        "producer_id": "fixture",
        "stream_id": "fixture:task",
        "stream_epoch": 1,
        "producer_seq": seq,
        "occurred_at": "2026-11-28T01:00:00Z",
        "idempotency_key": event_id,
        "app_id": "app-a",
        "task_id": "tsk_1",
        "run_id": "run_1",
        "attempt": 1,
        "revision": 1,
        "stage": stage,
        "state": "SUCCEEDED",
    }


def _tracker_ids():
    payload = json.loads(ITEMS.read_text(encoding="utf-8"))
    return {item["id"] for item in payload["items"]}


if __name__ == "__main__":
    unittest.main()
