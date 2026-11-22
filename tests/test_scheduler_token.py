"""Headless REST API auth and repository-scoped PR listing."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy/app-init"))
import upgrade_pr as u
sys.path.pop(0)


class TokenTests(unittest.TestCase):
    def test_private_token_and_no_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "token"
            token.write_text("a" * 40)
            token.chmod(0o600)
            with patch.object(u, "TOKEN_FILE", token):
                self.assertEqual(u.read_token(), "a" * 40)
                token.chmod(0o644)
                with self.assertRaises(ValueError):
                    u.read_token()
            token.chmod(0o600)
            link = Path(tmp) / "link"
            link.symlink_to(token)
            with patch.object(u, "TOKEN_FILE", link), self.assertRaises(OSError):
                u.read_token()

    def test_scoped_list_and_exact_head(self):
        row = {"number": 3, "state": "open", "html_url": "https://github.com/example/app/pull/3",
               "head": {"ref": "base-upgrade/abc", "repo": {"full_name": "example/app"}},
               "base": {"ref": "main"}}
        fork = {**row, "head": {**row["head"], "repo": {"full_name": "other/app"}}}
        with patch.object(u, "github", return_value=[row, fork]) as api:
            items = u.pull_rows("example/app", branch="base-upgrade/abc")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["state"], "OPEN")
            self.assertEqual(api.call_args.kwargs["query"]["head"], "example:base-upgrade/abc")

    def test_rejects_unsupported_api_path_and_token(self):
        with self.assertRaises(ValueError):
            u.github("https://evil.example", token="a"*40)
        with self.assertRaises(ValueError):
            u.github("/user", token="invalid")

    def test_no_gh_login_dependency(self):
        root = Path(__file__).resolve().parents[1] / "deploy/app-init"
        for path in ("upgrade_pr.py", "upgrade_scheduler.py", "install-upgrade-scheduler.py"):
            self.assertNotIn('["gh",', (root/path).read_text())
            self.assertNotIn('run("gh",', (root/path).read_text())


if __name__ == "__main__":
    unittest.main()
