"""Regression check: bootstrap must honor existing SSH credentials."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BootstrapAuthTests(unittest.TestCase):
    def test_bootstrap_does_not_force_https_login(self):
        script = (ROOT / "deploy/app-init/bootstrap.sh").read_text(encoding="utf-8")
        commands = [
            line.strip() for line in script.splitlines()
            if line.strip().startswith('bash "$work/deploy/app-init/app-init"')
        ]
        self.assertEqual(commands, ['bash "$work/deploy/app-init/app-init" --install-deps'])


if __name__ == "__main__":
    unittest.main()
