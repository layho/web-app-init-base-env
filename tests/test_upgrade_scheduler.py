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

    def test_blocked_plan_retries_on_next_tick_after_resolver_upgrade(self):
        with patch.object(s.upgrade,"remote_ref",return_value=A), patch.object(s,"open_upgrades",return_value=[]), patch.object(s.upgrade,"coordinate",side_effect=[
            {"state":"blocked","changes":[{"path":"deploy/macmini/manage.sh","action":"review"}]},
            {"state":"draft_created","url":"https://github.com/example/app/pull/8"}]) as create:
            entry={"mode":"create"}
            first=s.tick_app(REPO,entry,B)
            second=s.tick_app(REPO,entry,B)
            self.assertEqual(first["state"],"blocked")
            self.assertEqual(first["review_count"],1)
            self.assertEqual(second["state"],"draft_created")
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

    def test_blocked_status_includes_only_safe_code_paths(self):
        with patch.object(s.upgrade, "remote_ref", return_value=A), \
             patch.object(s, "open_upgrades", return_value=[]), \
             patch.object(s.upgrade, "coordinate", return_value={
                 "state": "blocked", "changes": [
                     {"path": "app/main.py", "action": "conflict", "reason": "overlap"},
                     {"path": "/private/token", "action": "conflict", "reason": "unsafe"},
                     {"path": "frontend/src/main.tsx", "action": "review", "reason": "review"},
                 ]}):
            result = s.tick_app(REPO, {"mode": "create"}, B)
        self.assertEqual(result["review_count"], 2)
        self.assertEqual([item["path"] for item in result["blocked_files"]],
                         ["app/main.py", "frontend/src/main.tsx"])

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


    def test_panel_verified_pending_pr_can_promote(self):
        panel = "layho/macmini-office-app-panel"
        row = {"number": 17, "url": "https://github.com/" + panel + "/pull/17",
               "headRefName": "base-upgrade/" + B}
        with patch.object(s.upgrade,"remote_ref",return_value=A), \
             patch.object(s,"open_upgrades",return_value=[row]), \
             patch.object(s.auto_merge,"official_foundation_target",return_value=True), \
             patch.object(s.auto_merge,"promote",return_value=True) as promote:
            result = s.tick_app(panel,{"mode":"create"},B)
            self.assertTrue(result["merged_now"])
            self.assertEqual(result["state"], "upgrade_merged")
            promote.assert_called_once_with(panel,row,A,B)

    def test_panel_stale_ancestor_pr_finishes_then_next_tick_plans_latest(self):
        panel = "layho/macmini-office-app-panel"
        previous_target = "c" * 40
        latest_target = B
        row = {"number": 18, "url": "https://github.com/" + panel + "/pull/18",
               "headRefName": "base-upgrade/" + previous_target}
        with patch.object(s.upgrade, "remote_ref", return_value=A), \
             patch.object(s, "open_upgrades", side_effect=[[row], []]), \
             patch.object(s.auto_merge, "official_foundation_target", return_value=True), \
             patch.object(s.auto_merge, "promote", return_value=True) as promote, \
             patch.object(s.upgrade, "coordinate", return_value={
                 "state": "draft_created", "url": "https://github.com/" + panel + "/pull/19"}) as create:
            first = s.tick_app(panel, {"mode": "create"}, latest_target)
            self.assertEqual(first["state"], "upgrade_merged")
            self.assertEqual(first["promoted_target_sha"], previous_target)
            promote.assert_called_once_with(panel, row, A, previous_target)
            second = s.tick_app(panel, {"mode": "create"}, latest_target)
            self.assertEqual(second["state"], "draft_created")
            create.assert_called_once()

    def test_stale_or_nonancestor_remains_paused_without_side_effects(self):
        panel = "layho/macmini-office-app-panel"
        row = {"number": 22, "url": "https://github.com/" + panel + "/pull/22",
               "headRefName": "base-upgrade/" + "c" * 40}
        with patch.object(s.upgrade, "remote_ref", return_value=A), \
             patch.object(s, "open_upgrades", return_value=[row]), \
             patch.object(s.auto_merge, "official_foundation_target", return_value=False), \
             patch.object(s.auto_merge, "promote") as promote:
            result = s.tick_app(panel, {"mode": "create"}, B)
            self.assertEqual(result["state"], "upgrade_review_required")
            self.assertEqual(result["reason"], "unknown_or_nonancestor_target")
            promote.assert_not_called()

    def test_ambiguous_upgrades_require_review(self):
        panel = "layho/macmini-office-app-panel"
        rows = [{"number": n, "headRefName": "base-upgrade/" + B} for n in (18, 19)]
        with patch.object(s.upgrade, "remote_ref", return_value=A), \
             patch.object(s, "open_upgrades", return_value=rows), \
             patch.object(s.auto_merge, "promote") as promote:
            result = s.tick_app(panel, {"mode": "create"}, B)
            self.assertEqual(result["state"], "upgrade_review_required")
            promote.assert_not_called()

    def test_other_apps_never_auto_merge(self):
        row = {"number": 8, "url": "https://github.com/example/app/pull/8"}
        with patch.object(s.upgrade,"remote_ref",return_value=A), \
             patch.object(s,"open_upgrades",return_value=[row]), \
             patch.object(s.auto_merge,"promote") as promote:
            s.tick_app(REPO,{"mode":"create"},B)
            promote.assert_not_called()

if __name__ == "__main__":
    unittest.main()
