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
                return f"{MERGE} {BASE} {HEAD}" if "%H %P" in args else TREE
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
                return f"{MERGE} {BASE} {HEAD}" if "%H %P" in args else "e"*40
            if args[:1] == ("rev-parse",):
                return BASE
            return ""
        with patch.object(m, "authorized", return_value=(HEAD, BASE, MERGE, TREE)), \
             patch.object(m.base, "git", side_effect=fake_git):
            self.assertFalse(m.merge_one("token", REPO, 7))
        self.assertFalse(any(args[0] == "push" for args in calls))


if __name__ == "__main__":
    unittest.main()


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
