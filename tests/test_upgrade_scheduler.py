import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "deploy/app-init"
sys.path.insert(0, str(SOURCE))
import upgrade_scheduler as s
sys.path.remove(str(SOURCE))

A, B = "a" * 40, "b" * 40
REPO = "example/app"


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "scheduler"
        self.p = patch.object(s, "ROOT", self.root)
        self.p.start()
        self.addCleanup(self.p.stop)
        s.private_root()

    def test_registry_and_scope(self):
        s.register(REPO, "create")
        self.assertEqual(s.registry()["apps"][REPO]["mode"], "create")
        with self.assertRaises(ValueError):
            s.register(s.upgrade.FOUNDATION, "create")
        with self.assertRaises(ValueError):
            s.register("../bad", "create")

    def test_observe_never_creates(self):
        with patch.object(s.upgrade, "remote_ref", return_value=A), patch.object(s.upgrade, "coordinate") as create:
            self.assertEqual(s.tick_app(REPO, {"mode":"observe"}, B)["state"], "waiting_adoption")
            create.assert_not_called()

    def test_pending_older_version_blocks_new_pr(self):
        with patch.object(s.upgrade,"remote_ref",return_value=A), patch.object(s,"open_upgrades",return_value=[{"url":"https://github.com/example/app/pull/3"}]), patch.object(s.upgrade,"coordinate") as create:
            result=s.tick_app(REPO,{"mode":"create"},B)
            self.assertEqual(result["state"],"upgrade_pending")
            self.assertEqual(result["pr_url"],"https://github.com/example/app/pull/3")
            create.assert_not_called()

    def test_same_blocked_snapshot_no_repeat_until_change(self):
        with patch.object(s.upgrade,"remote_ref",return_value=A), patch.object(s,"open_upgrades",return_value=[]), patch.object(s.upgrade,"coordinate",return_value={"state":"blocked","changes":[{"action":"conflict"}]}) as create:
            entry={"mode":"create"}
            first=s.tick_app(REPO,entry,B)
            second=s.tick_app(REPO,entry,B)
            self.assertEqual(first["review_count"],1)
            self.assertEqual(second["state"],"blocked")
            self.assertEqual(create.call_count,1)
            s.tick_app(REPO,entry,"c"*40)
            self.assertEqual(create.call_count,2)

    def test_errors_retry_and_urls_are_scoped(self):
        with patch.object(s.upgrade,"remote_ref",return_value=A),patch.object(s,"open_upgrades",return_value=[]),patch.object(s.upgrade,"coordinate",side_effect=[ValueError("secret"), {"state":"draft_created","url":"https://evil.example/token"}]) as create:
            first=s.tick_app(REPO,{"mode":"create"},B)
            self.assertEqual(first["state"],"error")
            self.assertNotIn("secret",json.dumps(first))
            second=s.tick_app(REPO,{"mode":"create"},B)
            self.assertEqual(second["state"],"draft_created")
            self.assertIsNone(second["pr_url"])
            self.assertEqual(create.call_count,2)

    def test_nonblocking_scheduler_lock(self):
        with s.lock("tick.lock"):
            with self.assertRaises(BlockingIOError):
                with s.lock("tick.lock"):
                    pass

    def test_if_absent_preserves_manual_mode(self):
        s.register(REPO, "observe")
        s.register(REPO, "create", if_absent=True)
        self.assertEqual(s.registry()["apps"][REPO]["mode"], "observe")

    def test_if_absent_registers_new_app(self):
        s.register(REPO, "create", if_absent=True)
        self.assertEqual(s.registry()["apps"][REPO]["mode"], "create")

if __name__ == "__main__":
    unittest.main()


    def test_panel_verified_pending_pr_can_promote(self):
        panel = "layho/macmini-office-app-panel"
        row = {"number": 17, "url": "https://github.com/" + panel + "/pull/17",
               "headRefName": "base-upgrade/" + B}
        with patch.object(s.upgrade,"remote_ref",return_value=A), \
             patch.object(s,"open_upgrades",return_value=[row]), \
             patch.object(s.auto_merge,"promote",return_value=True) as promote:
            result = s.tick_app(panel,{"mode":"create"},B)
            self.assertTrue(result["merged_now"])
            promote.assert_called_once_with(panel,row,A,B)

    def test_other_apps_never_auto_merge(self):
        row = {"number": 8, "url": "https://github.com/example/app/pull/8"}
        with patch.object(s.upgrade,"remote_ref",return_value=A), \
             patch.object(s,"open_upgrades",return_value=[row]), \
             patch.object(s.auto_merge,"promote") as promote:
            s.tick_app(REPO,{"mode":"create"},B)
            promote.assert_not_called()
