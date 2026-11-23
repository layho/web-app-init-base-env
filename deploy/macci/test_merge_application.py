"""Offline regression for application-only main merge coordinator."""
import json
import unittest
from unittest.mock import patch
import merge_application as m

REPO = "layho/macmini-office-app-panel"
BASE, HEAD, MERGE, TREE = ("b"*40, "a"*40, "c"*40, "d"*40)


def pr():
    return {"state": "open", "draft": False, "mergeable": True,
            "mergeable_state": "clean", "author_association": "OWNER",
            "user": {"login": "layho"}, "number": 7,
            "base": {"ref": "main", "sha": BASE, "repo": {"full_name": REPO}},
            "head": {"sha": HEAD, "repo": {"full_name": REPO}},
            "merge_commit_sha": MERGE}


def review(decision="approved", number=1):
    return {"id": number, "author_association": "OWNER",
            "user": {"login": "layho"}, "body": m.MARKER +
            json.dumps({"schema": 1, "head": HEAD, "decision": decision, "base": BASE})}


def status(state="success"):
    return {"context": m.CONTEXT, "creator": {"login": "layho"}, "state": state,
            "description": f"base={BASE} merge={MERGE} passed"}


class ApplicationMainTests(unittest.TestCase):
    def test_allowlist_never_includes_foundation(self):
        self.assertIn(REPO, m.ALLOWED)
        self.assertNotIn("layho/web-app-init-base-env", m.ALLOWED)
        self.assertNotIn("layho/xiongzhan-ti-hunter", m.ALLOWED)
        self.assertNotIn("layho/untrusted", m.ALLOWED)
        self.assertFalse(m.eligible("layho/untrusted", pr()))

    def test_app_main_is_not_base_preview(self):
        p = pr()
        self.assertTrue(m.eligible(REPO, p))
        p["base"]["ref"] = "preview"
        self.assertFalse(m.eligible(REPO, p))
        p = pr()
        p["draft"] = True
        self.assertFalse(m.eligible(REPO, p))
        p = pr()
        p["head"]["repo"]["full_name"] = "attacker/fork"
        self.assertFalse(m.eligible(REPO, p))
        p = pr()
        p["user"]["login"] = "other"
        self.assertFalse(m.eligible(REPO, p))

    def test_owner_exact_review_and_revocation(self):
        self.assertTrue(m.review([review()], HEAD, BASE))
        self.assertFalse(m.review([review()], HEAD, "e"*40))
        self.assertFalse(m.review([review(), review("revoked", 2)], HEAD, BASE))
        outsider = {**review(), "id": 3, "user": {"login": "stranger"}}
        self.assertTrue(m.review([review(), outsider], HEAD, BASE))
        self.assertFalse(m.review([], HEAD, BASE))

    def test_status_must_match_exact_base_merge_and_owner(self):
        self.assertTrue(m.base.tested([status()], HEAD, BASE, MERGE))
        self.assertFalse(m.base.tested([status()], HEAD, BASE, "f"*40))
        self.assertFalse(m.base.tested([status()], HEAD, "f"*40, MERGE))
        self.assertFalse(m.base.tested([{**status(), "creator": {"login": "other"}}],
                                        HEAD, BASE, MERGE))
        self.assertFalse(m.base.tested([{**status(), "state": "pending"}, status()],
                                        HEAD, BASE, MERGE))

    def test_authorization_requires_exact_candidate_and_current_main(self):
        def fake_api(token, repo, path):
            self.assertEqual(repo, REPO)
            if path == "/pulls/7":
                return pr()
            if path == "/branches/main":
                return {"commit": {"sha": BASE}}
            if path == "/git/commits/" + MERGE:
                return {"parents": [{"sha": BASE}, {"sha": HEAD}],
                        "tree": {"sha": TREE}}
            if path.startswith("/commits/"):
                return [status()]
            raise AssertionError(path)
        with patch.object(m, "api", side_effect=fake_api), patch.object(
                m, "pages", return_value=[review()]):
            self.assertEqual(m.authorized("token", REPO, 7),
                             (HEAD, BASE, MERGE, TREE))
            self.assertIsNone(m.authorized("token", "layho/untrusted", 7))

    def test_stale_synthetic_merge_is_never_authorized(self):
        def fake_api(token, repo, path):
            if path == "/pulls/7":
                return pr()
            if path == "/branches/main":
                return {"commit": {"sha": BASE}}
            if path.startswith("/git/commits/"):
                return {"parents": [{"sha": "e"*40}, {"sha": HEAD}],
                        "tree": {"sha": TREE}}
            raise AssertionError(path)
        with patch.object(m, "api", side_effect=fake_api):
            self.assertIsNone(m.authorized("token", REPO, 7))

    def test_no_review_never_executes_git_or_push(self):
        with patch.object(m, "authorized", return_value=None), patch.object(
                m.base, "git") as git:
            self.assertFalse(m.merge_one("token", REPO, 7))
            git.assert_not_called()

    def test_dry_run_validates_tree_without_pushing(self):
        calls = []
        def fake_git(folder, *args):
            calls.append(args)
            if args[:2] == ("show", "-s"):
                return f"{MERGE} {BASE} {HEAD}" if any("%H %P" in arg for arg in args) else TREE
            if args[:1] == ("rev-parse",):
                return BASE
            return ""
        with patch.object(m, "authorized", return_value=(HEAD, BASE, MERGE, TREE)), \
             patch.object(m.base, "git", side_effect=fake_git):
            self.assertTrue(m.merge_one("token", REPO, 7, dry_run=True))
        self.assertFalse(any(args[0] == "push" for args in calls))

    def test_candidate_tree_mismatch_must_not_push(self):
        calls = []
        def fake_git(folder, *args):
            calls.append(args)
            if args[:2] == ("show", "-s"):
                return f"{MERGE} {BASE} {HEAD}" if any("%H %P" in arg for arg in args) else "e"*40
            if args[:1] == ("rev-parse",):
                return BASE
            return ""
        with patch.object(m, "authorized", return_value=(HEAD, BASE, MERGE, TREE)), \
             patch.object(m.base, "git", side_effect=fake_git):
            self.assertFalse(m.merge_one("token", REPO, 7))
        self.assertFalse(any(args[0] == "push" for args in calls))


class ApplicationCliTests(unittest.TestCase):
    def test_exact_pr_manual_path_uses_same_safe_merge_gate(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(m, "ROOT", Path(folder)), \
             patch.object(m.base, "read_token", return_value="test"), \
             patch.object(m, "api", return_value=pr()) as api, \
             patch.object(m, "pages") as pages, \
             patch.object(m, "merge_one", return_value=True) as merge, \
             patch("sys.argv", ["merge_application.py", "--repo", REPO,
                                "--pr", "7", "--dry-run"]):
            self.assertEqual(m.main(), 0)
            api.assert_called_once_with("test", REPO, "/pulls/7")
            pages.assert_not_called()
            merge.assert_called_once_with("test", REPO, 7, True)

    def test_unknown_repo_rejected_before_reading_token(self):
        with patch.object(m.base, "read_token") as token, \
             patch("sys.argv", ["merge_application.py", "--repo",
                                "layho/web-app-init-base-env", "--pr", "7"]):
            with self.assertRaises(SystemExit):
                m.main()
            token.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class ApplicationMergeReceiptTests(unittest.TestCase):
    def test_merge_receipt_is_private_and_never_claims_deployment(self):
        import tempfile
        from pathlib import Path
        import stat
        with tempfile.TemporaryDirectory() as temporary, patch.object(m, "ROOT", Path(temporary)):
            file = m.merge_receipt(REPO, 7, HEAD, BASE, MERGE, TREE)
            record = json.loads(file.read_text())
            self.assertEqual(record["merge_state"], "github_main_verified")
            self.assertEqual(record["deployment_state"], "unverified")
            self.assertEqual(record["head_sha"], HEAD)
            self.assertEqual(record["previous_main_sha"], BASE)
            self.assertEqual(record["merged_main_sha"], MERGE)
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(file.parent.stat().st_mode), 0o700)
            with self.assertRaisesRegex(ValueError, "already exists"):
                m.merge_receipt(REPO, 7, HEAD, BASE, MERGE, TREE)

    def test_receipt_rejects_untrusted_repo_and_bad_sha(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temporary, patch.object(m, "ROOT", Path(temporary)):
            with self.assertRaisesRegex(ValueError, "Invalid merge receipt"):
                m.merge_receipt("layho/untrusted", 7, HEAD, BASE, MERGE, TREE)
            with self.assertRaisesRegex(ValueError, "Invalid merge receipt"):
                m.merge_receipt(REPO, 7, "unverified", BASE, MERGE, TREE)
            self.assertFalse((Path(temporary) / "app-main-receipts").exists())

    def test_dry_run_writes_no_merge_receipts(self):
        import tempfile
        from pathlib import Path
        def git(folder, *args):
            if args[:2] == ("show", "-s"):
                return f"{MERGE} {BASE} {HEAD}" if any("%H %P" in x for x in args) else TREE
            if args[:1] == ("rev-parse",):
                return BASE
            return ""
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(m, "ROOT", Path(temporary)), \
             patch.object(m, "authorized", return_value=(HEAD, BASE, MERGE, TREE)), \
             patch.object(m.base, "git", side_effect=git):
            self.assertTrue(m.merge_one("token", REPO, 7, dry_run=True))
            self.assertFalse((Path(temporary) / "app-main-receipts").exists())


    def test_receipt_written_only_after_exact_remote_main_confirmed(self):
        calls = []
        def git(folder, *args):
            calls.append(args)
            if args[:2] == ("show", "-s"):
                return f"{MERGE} {BASE} {HEAD}" if any("%H %P" in x for x in args) else TREE
            if args[:1] == ("rev-parse",):
                return BASE
            return ""
        with patch.object(m, "authorized", return_value=(HEAD, BASE, MERGE, TREE)), \
             patch.object(m.base, "git", side_effect=git), \
             patch.object(m, "api", return_value={"commit": {"sha": MERGE}}), \
             patch.object(m, "merge_receipt") as receipt:
            self.assertTrue(m.merge_one("token", REPO, 7))
            receipt.assert_called_once_with(REPO, 7, HEAD, BASE, MERGE, TREE)
            self.assertTrue(any(x[0] == "push" for x in calls))

        with patch.object(m, "authorized", return_value=(HEAD, BASE, MERGE, TREE)), \
             patch.object(m.base, "git", side_effect=git), \
             patch.object(m, "api", return_value={"commit": {"sha": BASE}}), \
             patch.object(m, "merge_receipt") as receipt:
            self.assertFalse(m.merge_one("token", REPO, 7))
            receipt.assert_not_called()


class ApplicationReceiptReconcileTests(unittest.TestCase):
    def test_reconcile_rejects_closed_pr_without_owner_review(self):
        closed = {**pr(), "state": "closed", "merged": True}
        with patch.object(m, "api", side_effect=lambda token, repo, path: (
                    closed if path == "/pulls/7" else
                    {"parents": [{"sha": BASE}, {"sha": HEAD}], "tree": {"sha": TREE}})), \
             patch.object(m, "pages", return_value=[]), \
             patch.object(m.base, "git") as git, \
             patch.object(m, "merge_receipt") as receipt:
            self.assertFalse(m.reconcile_one("token", REPO, 7))
            git.assert_not_called()
            receipt.assert_not_called()

    def test_reconcile_requires_historical_candidate_ancestry(self):
        import subprocess
        closed = {**pr(), "state": "closed", "merged": True}
        def api(token, repo, path):
            if path == "/pulls/7":
                return closed
            if path == "/git/commits/" + MERGE:
                return {"parents": [{"sha": BASE}, {"sha": HEAD}], "tree": {"sha": TREE}}
            if path.startswith("/commits/"):
                return [status()]
            if path == "/branches/main":
                return {"commit": {"sha": MERGE}}
            raise AssertionError(path)
        def git(folder, *args):
            if args[:2] == ("merge-base", "--is-ancestor"):
                raise subprocess.CalledProcessError(1, args)
            if args[0] == "rev-parse":
                return MERGE
            return ""
        with patch.object(m, "api", side_effect=api), \
             patch.object(m, "pages", return_value=[review()]), \
             patch.object(m.base, "git", side_effect=git), \
             patch.object(m, "merge_receipt") as receipt:
            with self.assertRaises(subprocess.CalledProcessError):
                m.reconcile_one("token", REPO, 7)
            receipt.assert_not_called()

    def test_reconcile_exact_success_recovers_receipt_and_dry_run_does_not(self):
        closed = {**pr(), "state": "closed", "merged": True}
        def api(token, repo, path):
            if path == "/pulls/7":
                return closed
            if path == "/git/commits/" + MERGE:
                return {"parents": [{"sha": BASE}, {"sha": HEAD}], "tree": {"sha": TREE}}
            if path.startswith("/commits/"):
                return [status()]
            if path == "/branches/main":
                return {"commit": {"sha": MERGE}}
            raise AssertionError(path)
        def git(folder, *args):
            if args[0] == "rev-parse":
                return MERGE
            if args[:2] == ("show", "-s"):
                return f"{MERGE} {BASE} {HEAD}" if any("%H %P" in x for x in args) else TREE
            return ""
        with patch.object(m, "api", side_effect=api), \
             patch.object(m, "pages", return_value=[review()]), \
             patch.object(m.base, "git", side_effect=git), \
             patch.object(m, "merge_receipt") as receipt:
            self.assertTrue(m.reconcile_one("token", REPO, 7, dry_run=True))
            receipt.assert_not_called()
            self.assertTrue(m.reconcile_one("token", REPO, 7))
            receipt.assert_called_once_with(REPO, 7, HEAD, BASE, MERGE, TREE)

    def test_reconcile_refuses_wrong_repo_or_open_pr(self):
        with patch.object(m, "api") as api:
            self.assertFalse(m.reconcile_one("token", "layho/web-app-init-base-env", 7))
            api.assert_not_called()
        with patch.object(m, "api", return_value=pr()), \
             patch.object(m, "merge_receipt") as receipt:
            self.assertFalse(m.reconcile_one("token", REPO, 7))
            receipt.assert_not_called()
