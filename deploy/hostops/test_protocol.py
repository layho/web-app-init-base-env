import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import protocol as p


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 26, 8, 0, tzinfo=timezone.utc)
        self.job = {"schema": 1, "task_id": "job-1", "host_id": "macmini-01",
                    "operation": "host.health", "parameters": {},
                    "issued_at": "2026-11-26T07:59:00Z",
                    "expires_at": "2026-11-26T08:01:00Z",
                    "parameter_sha256": p.parameter_digest({})}

    def test_valid_fixture(self):
        with patch.object(p, "execute", return_value={"schema": 1}) as execute:
            result = p.run_local_fixture(self.job, "macmini-01", self.now)
        execute.assert_called_once_with("host.health", {})
        self.assertEqual(result["transport"], "local_fixture")
        self.assertEqual(result["envelope_sha256"], p.envelope_digest(self.job))

    def test_reject_invalid_envelopes_without_execution(self):
        for changes in ({"schema": True}, {"operation": "shell"},
                        {"parameters": {"path": "/"}}, {"host_id": "other"},
                        {"parameter_sha256": "0"*64}, {"task_id": "../escape"},
                        {"expires_at": "2026-11-26T08:00:00Z"},
                        {"issued_at": "2026-11-26T08:00:01Z"},
                        {"expires_at": "2026-11-26T09:00:00Z"},
                        {"unexpected": True}):
            with self.subTest(changes=changes), patch.object(p, "execute") as execute:
                with self.assertRaises(ValueError):
                    p.run_local_fixture({**self.job, **changes}, "macmini-01", self.now)
                execute.assert_not_called()

    def test_digest_stable_and_bound(self):
        self.assertEqual(p.envelope_digest(self.job), p.envelope_digest(dict(reversed(list(self.job.items())))))
        self.assertNotEqual(p.envelope_digest(self.job), p.envelope_digest({**self.job, "task_id": "job-2"}))
