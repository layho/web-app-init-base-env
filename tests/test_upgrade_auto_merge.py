"""Safety regression for Panel foundation upgrade promotion; no network."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy" / "app-init"))
import upgrade_auto_merge as m

BASE = "a" * 40
HEAD = "b" * 40
TARGET = "c" * 40
MERGE = "d" * 40
REPO = "layho/macmini-office-app-panel"
CHANGES = [{"path": "frontend/src/ProjectInfoPage.tsx", "action": "replace"}]


def fixture():
    report = {"schema": 1, "app_sha": BASE, "blocked": False,
              "target": {"repository": m.upgrade.FOUNDATION, "sha": TARGET},
              "changes": CHANGES}
    pr = {"state": "open", "draft": True, "merged": False, "number": 9,
          "user": {"login": "layho"}, "author_association": "OWNER",
          "mergeable": True, "mergeable_state": "clean", "node_id": "PR_12345",
          "body": "Foundation upgrade from verified official main.\n\n" + json.dumps(report),
          "head": {"sha": HEAD, "ref": "base-upgrade/" + TARGET,
                   "repo": {"full_name": REPO}},
          "base": {"ref": "main", "sha": BASE, "repo": {"full_name": REPO}}}
    files = [{"filename": f, "status": "modified"} for f in
             [".app-init.json", "frontend/src/ProjectInfoPage.tsx", "app/project_info.json"]]
    status = [{"context": m.CONTEXT, "state": "success",
               "creator": {"login": m.OWNER},
               "description": f"base={BASE} merge={MERGE} passed"}]
    def api(path, **kwargs):
        if path.endswith("/pulls/9"):
            return pr
        if path.endswith("/pulls/9/files"):
            return files
        if path.endswith("/commits/" + HEAD + "/statuses"):
            return status
        if path.endswith("/git/commits/" + MERGE):
            return {"parents": [{"sha": BASE}, {"sha": HEAD}]}
        if path.endswith("/branches/main"):
            return {"commit": {"sha": BASE}}
        if path.endswith("/pulls/9/merge"):
            return {"merged": True}
        raise AssertionError(path)
    return pr, files, status, api


class PromotionTests(unittest.TestCase):
    def test_exact_mac_ci_and_scope(self):
        _, _, _, api = fixture()
        with patch.object(m, "api", side_effect=api):
            self.assertEqual(m.promotion_gate(REPO, 9, BASE, TARGET), HEAD)
            self.assertIsNone(m.promotion_gate("example/app", 9, BASE, TARGET))

    def test_modified_base_or_failing_test_blocks(self):
        pr, files, statuses, api = fixture()
        with patch.object(m, "api", side_effect=api):
            pr["base"]["sha"] = "e" * 40
            self.assertIsNone(m.promotion_gate(REPO, 9, BASE, TARGET))
            pr["base"]["sha"] = BASE
            statuses[0]["state"] = "failure"
            self.assertIsNone(m.promotion_gate(REPO, 9, BASE, TARGET))
            statuses[0]["state"] = "success"
            statuses[0]["description"] = f"base={BASE} merge={'f'*40} passed"
            # Tested merge must be fetchable and prove exact parent pair.
            with self.assertRaises(AssertionError):
                m.promotion_gate(REPO, 9, BASE, TARGET)

    def test_unplanned_business_file_blocks(self):
        pr, files, statuses, api = fixture()
        with patch.object(m, "api", side_effect=api):
            files.append({"filename": "app/business.py", "status": "modified"})
            self.assertIsNone(m.promotion_gate(REPO, 9, BASE, TARGET))
            files.pop()
            pr["body"] = pr["body"].replace('"blocked": false', '"blocked": true')
            self.assertIsNone(m.promotion_gate(REPO, 9, BASE, TARGET))

    def test_draft_or_missing_status_never_promotes(self):
        pr, _, statuses, api = fixture()
        with patch.object(m, "api", side_effect=api):
            pr["draft"] = False
            self.assertIsNone(m.promotion_gate(REPO, 9, BASE, TARGET))
            pr["draft"] = True
            statuses.clear()
            self.assertIsNone(m.promotion_gate(REPO, 9, BASE, TARGET))

    def test_promote_rechecks_gate_then_merges_exact_head(self):
        pr, _, _, api = fixture()
        calls = []
        def monitored(path, **kw):
            calls.append((path, kw))
            return api(path, **kw)
        with patch.object(m, "api", side_effect=monitored), \
             patch.object(m.upgrade, "remote_ref", return_value=TARGET), \
             patch.object(m, "graphql_ready", side_effect=lambda node: pr.update(draft=False) or True):
            self.assertTrue(m.promote(REPO, {"number": 9}, BASE, TARGET))
        merges = [x for x in calls if x[0].endswith("/merge")]
        self.assertEqual(len(merges), 1)
        self.assertEqual(merges[0][1]["payload"]["sha"], HEAD)

    def test_changed_source_never_marks_ready(self):
        _, _, _, api = fixture()
        with patch.object(m, "api", side_effect=api), \
             patch.object(m.upgrade, "remote_ref", return_value="e"*40), \
             patch.object(m, "graphql_ready") as ready:
            self.assertFalse(m.promote(REPO, {"number": 9}, BASE, TARGET))
            ready.assert_not_called()


if __name__ == "__main__":
    unittest.main()
