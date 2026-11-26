import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch

import local_queue as q
from protocol import parameter_digest


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "jobs.sqlite"
        self.now = datetime(2026, 11, 26, 8, 0, tzinfo=timezone.utc)
        self.job = {"schema": 1, "task_id": "job-1", "host_id": "macmini-01",
                    "operation": "host.health", "parameters": {},
                    "issued_at": "2026-11-26T07:59:00Z",
                    "expires_at": "2026-11-26T08:01:00Z",
                    "parameter_sha256": parameter_digest({})}

    def test_reopen_returns_same_result_without_execution(self):
        with patch.object(q, "run_local_fixture", return_value={"result": 1}) as run:
            self.assertEqual(q.LocalQueue(self.path).run_fixture(self.job, "macmini-01", self.now), {"result": 1})
            self.assertEqual(q.LocalQueue(self.path).run_fixture(self.job, "macmini-01", self.now), {"result": 1})
        run.assert_called_once()

    def test_conflicting_identity_rejected(self):
        with patch.object(q, "run_local_fixture", return_value={"result": 1}) as run:
            ledger = q.LocalQueue(self.path)
            ledger.run_fixture(self.job, "macmini-01", self.now)
            with self.assertRaisesRegex(ValueError, "identity conflict"):
                ledger.run_fixture({**self.job, "operation": "service.inventory"}, "macmini-01", self.now)
        run.assert_called_once()

    def test_interrupted_job_not_replayed(self):
        with patch.object(q, "run_local_fixture", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                q.LocalQueue(self.path).run_fixture(self.job, "macmini-01", self.now)
        with patch.object(q, "run_local_fixture") as run:
            with self.assertRaisesRegex(ValueError, "outcome unknown"):
                q.LocalQueue(self.path).run_fixture(self.job, "macmini-01", self.now)
        run.assert_not_called()
