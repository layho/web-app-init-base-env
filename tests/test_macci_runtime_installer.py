"""Installer regression: candidate modules are data, not host-executed tests."""
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "deploy/macci/upgrade-runtime.py"
spec = importlib.util.spec_from_file_location("trusted_installer_test", SOURCE)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class RuntimeInstallerTests(unittest.TestCase):
    def test_candidate_top_level_code_is_not_executed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def git(*args):
                return subprocess.check_output(["git", "-C", folder, *args])
            git("init", "-q")
            git("config", "user.email", "test@example.invalid")
            git("config", "user.name", "test")
            source = root / "deploy/macci"
            source.mkdir(parents=True)
            sentinel = root / "executed"
            data = ("from pathlib import Path\nPath(" + repr(str(sentinel)) + ").touch()\n").encode()
            for name in installer.NAMES:
                (source / name).write_bytes(data)
            git("add", ".")
            git("commit", "-qm", "fixture")
            sha = git("rev-parse", "HEAD").decode().strip()
            modules = installer.read_candidate_modules(source, sha)
            self.assertFalse(sentinel.exists())
            self.assertEqual(modules["runner.py"], data)
            digest = hashlib.sha256(data).hexdigest()
            installer.verify_reviewed_digests(modules, digest, digest)
            with self.assertRaises(ValueError):
                installer.verify_reviewed_digests(modules, None, digest)
            with self.assertRaises(ValueError):
                installer.verify_reviewed_digests(modules, "0"*64, digest)
            (source / "runner.py").unlink()
            (source / "runner.py").symlink_to("merge_preview.py")
            git("add", ".")
            git("commit", "-qm", "symlink")
            with self.assertRaises(ValueError):
                installer.read_candidate_modules(source, git("rev-parse", "HEAD").decode().strip())
