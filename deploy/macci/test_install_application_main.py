"""Offline safety gates for application merge coordinator installer."""
import importlib.util
from pathlib import Path
import plistlib
import stat
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "app_main_installer", Path(__file__).resolve().parent / "install-application-main.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)

SHA = "a" * 40


class InstallApplicationMainTests(unittest.TestCase):
    def test_opted_in_application_only(self):
        args = installer.job("/usr/bin/python3")["ProgramArguments"]
        self.assertEqual(args[-2:], ["--repo", "layho/macmini-office-app-panel"])
        self.assertNotIn("layho/xiongzhan-ti-hunter", args)
        self.assertNotIn("layho/web-app-init-base-env", args)
        self.assertFalse(installer.job()["RunAtLoad"])
        self.assertEqual(installer.job()["StartInterval"], 60)

    def test_incorrect_checkout_fails_before_any_host_action(self):
        with patch.object(installer.subprocess, "check_output",
                          side_effect=["b" * 40, "main", ""]):
            with self.assertRaisesRegex(ValueError, "exact SHA"):
                installer.checked_checkout(SHA)

    def test_stage_does_not_register_service(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root)
            source = target / "source"
            source.mkdir()
            (source / "merge_application.py").write_text("print('allowed')\n")
            with patch.object(installer, "SOURCE", source), \
                 patch.object(installer, "ROOT", target), \
                 patch.object(installer, "preflight"), \
                 patch.object(installer.subprocess, "run") as run:
                result = installer.stage(SHA)
                self.assertIn("Staged", result)
                self.assertTrue((target / "merge_application.py").exists())
                self.assertEqual(stat.S_IMODE((target / "merge_application.py").stat().st_mode), 0o700)
                run.assert_not_called()
                self.assertIn("Already staged", installer.stage(SHA))

    def test_stage_never_overwrites_other_host_module(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root)
            source = target / "source"
            source.mkdir()
            (source / "merge_application.py").write_text("approved-version\n")
            (target / "merge_application.py").write_text("other-version\n")
            (target / "merge_application.py").chmod(0o700)
            with patch.object(installer, "SOURCE", source), \
                 patch.object(installer, "ROOT", target), \
                 patch.object(installer, "preflight"):
                with self.assertRaisesRegex(ValueError, "differs"):
                    installer.stage(SHA)
                self.assertEqual((target / "merge_application.py").read_text(),
                                 "other-version\n")

    def test_enable_rejects_missing_stage_without_launchctl(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root)
            source = target / "source"
            source.mkdir()
            (source / "merge_application.py").write_text("approved\n")
            with patch.object(installer, "SOURCE", source), \
                 patch.object(installer, "ROOT", target), \
                 patch.object(installer, "preflight"), \
                 patch.object(installer.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "Stage"):
                    installer.enable(SHA)
                run.assert_not_called()

    def test_rejects_unsafe_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            target = path / "regular"
            target.write_text("token")
            target.chmod(0o600)
            link = path / "link"
            link.symlink_to(target)
            self.assertFalse(installer.private_file(link))
            self.assertFalse(installer.private_dir(link))


if __name__ == "__main__":
    unittest.main()
