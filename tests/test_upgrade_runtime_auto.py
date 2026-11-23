"""Offline safety tests: opted-in MacCI scheduler runtime refresh."""
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "deploy/app-init"
sys.path.insert(0, str(SOURCE))
import upgrade_runtime_auto as auto
sys.path.remove(str(SOURCE))
A, B = "a" * 40, "b" * 40


class RuntimeAutoUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "scheduler"
        root.mkdir(mode=0o700)
        for key, value in (("ROOT", root),):
            p = patch.object(auto.scheduler, key, value)
            p.start()
            self.addCleanup(p.stop)
        for path_attr, path in (
            ("OPT_IN", root / "runtime-auto-update.enabled"),
            ("INSTALLED", root / "runtime-installed-sha"),
        ):
            p = patch.object(auto, path_attr, path)
            p.start()
            self.addCleanup(p.stop)

    def enable(self, installed=A):
        auto.OPT_IN.write_text("enabled\n")
        auto.OPT_IN.chmod(0o600)
        if installed is not None:
            auto.INSTALLED.write_text(installed + "\n")
            auto.INSTALLED.chmod(0o600)

    def test_disabled_never_queries_remote_or_changes_host(self):
        with patch.object(auto.upgrade, "remote_ref") as remote, \
             patch.object(auto, "candidate_accepted") as accepted, \
             patch.object(auto.subprocess, "run") as run:
            self.assertEqual(auto.run_once(), "disabled")
            remote.assert_not_called()
            accepted.assert_not_called()
            run.assert_not_called()

    def test_up_to_date_no_clone(self):
        self.enable()
        with patch.object(auto.upgrade, "remote_ref", return_value=A), \
             patch.object(auto, "candidate_accepted") as accepted:
            self.assertEqual(auto.run_once(), "current")
            accepted.assert_not_called()

    def test_missing_acceptance_never_executes_source(self):
        self.enable()
        with patch.object(auto.upgrade, "remote_ref", return_value=B), \
             patch.object(auto, "candidate_accepted", return_value=False), \
             patch.object(auto.subprocess, "run") as run:
            self.assertEqual(auto.run_once(), "unaccepted")
            run.assert_not_called()

    def test_invalid_controls_or_untrusted_main_fail_closed(self):
        self.enable()
        auto.INSTALLED.write_text("not-a-sha")
        with self.assertRaisesRegex(ValueError, "Invalid installed"):
            auto.run_once()
        auto.INSTALLED.write_text(A)
        auto.OPT_IN.unlink()
        auto.OPT_IN.symlink_to(auto.INSTALLED)
        with self.assertRaisesRegex(ValueError, "Unsafe runtime"):
            auto.run_once()
        auto.OPT_IN.unlink()
        self.enable()
        with patch.object(auto.upgrade, "remote_ref", return_value="untrusted"):
            with self.assertRaisesRegex(ValueError, "No exact"):
                auto.run_once()

    def test_exact_accepted_main_must_be_checked_out_before_install(self):
        self.enable()
        git_refs = iter([B, B])
        invokes = []
        def command(args, **kwargs):
            invokes.append(args)
            if args[:2] == ["git", "clone"]:
                # The temp checkout belongs only to the verified test.
                root = Path(args[-1])
                (root / "deploy/app-init").mkdir(parents=True)
                (root / "deploy/app-init/install-upgrade-scheduler.py").write_text("# reviewed")
            return None
        with patch.object(auto.upgrade, "remote_ref", side_effect=lambda *_: next(git_refs)), \
             patch.object(auto, "candidate_accepted", return_value=True), \
             patch.object(auto.upgrade, "line", side_effect=[B, ""]), \
             patch.object(auto.subprocess, "run", side_effect=command):
            self.assertEqual(auto.run_once(), "upgraded")
        self.assertEqual(len(invokes), 2)
        self.assertEqual(invokes[0][:2], ["git", "clone"])
        self.assertNotIn("--no-checkout", invokes[0])
        self.assertIn("--apply", invokes[1])
        self.assertIn("--upgrade", invokes[1])
        self.assertEqual(invokes[1][invokes[1].index("--sha") + 1], B)
        self.assertEqual(invokes[1][invokes[1].index("--check-repo") + 1], auto.PANEL)

    def test_checkout_sha_mismatch_never_runs_installer(self):
        self.enable()
        invoked = []
        def clone(args, **kwargs):
            invoked.append(args)
        with patch.object(auto.upgrade, "remote_ref", return_value=B), \
             patch.object(auto, "candidate_accepted", return_value=True), \
             patch.object(auto.upgrade, "line", return_value=A), \
             patch.object(auto.subprocess, "run", side_effect=clone):
            with self.assertRaisesRegex(ValueError, "advanced"):
                auto.run_once()
        self.assertEqual(len(invoked), 1)

    def test_acceptance_must_match_closed_owner_issue_and_exact_sha(self):
        issue = {
            "title": "[BASE-ACCEPTANCE " + B + "]", "state": "closed",
            "user": {"login": "layho"}, "closed_by": {"login": "layho"},
            "body": '[BASE_ACCEPTANCE] ' + json.dumps({
                "schema": 1, "candidate_sha": B, "tested_merge": B,
                "expected_main_sha": A, "test_head": A, "tree": A
            })
        }
        class Response:
            def __init__(self, data): self.data = data
            def __enter__(self): return self
            def __exit__(self, *args): return False
        def run_issue(data):
            class Opener:
                def open(self, req, timeout):
                    return Response(data)
            with patch.object(auto.upgrade, "read_token", return_value="test-token"), \
                 patch.object(auto, "build_opener", return_value=Opener()), \
                 patch.object(auto.json, "load", side_effect=lambda _: data):
                return auto.candidate_accepted(B)
        self.assertTrue(run_issue([issue]))
        self.assertFalse(run_issue([{**issue, "closed_by": {"login": "other"}}]))
        self.assertFalse(run_issue([{**issue, "title": "other"}]))
        self.assertFalse(run_issue([{**issue, "body": "[BASE_ACCEPTANCE] {}"}]))


if __name__ == "__main__":
    unittest.main()
