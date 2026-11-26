import unittest
from unittest.mock import patch
import diagnostics as d


class DiagnosticTests(unittest.TestCase):
    def test_operations_deny_unknown_and_parameters(self):
        for operation, params in (("shell", {}), ("host.health", {"path": "/"}),
                                  ("service.inventory", []), (None, {})):
            with self.assertRaises(ValueError):
                d.execute(operation, params)

    def test_health_truthful_missing_metrics(self):
        with patch.object(d.shutil, "disk_usage", return_value=(100, 30, 70)):
            result = d.execute("host.health", {})["result"]
        self.assertEqual(result["disk"]["free_bytes"], 70)
        self.assertIsNone(result["cpu_percent"])
        self.assertIsNone(result["memory_percent"])

    def test_inventory_only_returns_known_services(self):
        result = d.subprocess.CompletedProcess([], 0,
            "PID Status Label\n123 0 com.layho.macci\n- 0 com.layho.macci-preview-merge\n- 1 com.layho.macci-main-publish\n2 0 unrelated.secret\n")
        with patch.object(d.platform, "system", return_value="Darwin"), patch.object(d.subprocess, "run", return_value=result) as run:
            rows = {x["service_id"]: x for x in d.services()}
        self.assertEqual(rows["macci.runner"]["state"], "running")
        self.assertEqual(rows["macci.preview"]["state"], "loaded_idle")
        self.assertEqual(rows["macci.publish"]["state"], "last_exit_failed")
        self.assertEqual(rows["panel.merge"]["state"], "not_visible")
        self.assertNotIn("unrelated.secret", str(rows))
        self.assertEqual(run.call_args.args[0], ["/bin/launchctl", "list"])
        self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_failure_and_other_os_not_reported_as_healthy(self):
        with patch.object(d.platform, "system", return_value="Darwin"), patch.object(d.subprocess, "run", side_effect=OSError):
            self.assertTrue(all(x["state"] == "unknown" for x in d.services()))
        with patch.object(d.platform, "system", return_value="Linux"):
            self.assertTrue(all(x["state"] == "unsupported" for x in d.services()))


if __name__ == "__main__":
    unittest.main()
