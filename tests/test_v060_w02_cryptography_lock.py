"""Lock check for the approved cryptography pin.

This test reads the lock and the installed distribution. It does not invoke
pip, uv, or the host installer.
"""

from __future__ import annotations

import importlib.metadata
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = "41.0.7"
SDIST_HASH = "13f93ce9bea8016c253b34afc6bd6a75993e5c40672ed5405a9c832f0d4a00bc"


def cryptography_block(text: str) -> list[str]:
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(f"cryptography=={PIN}"))
    block = [lines[start]]
    for line in lines[start + 1:]:
        if line.startswith("    --hash=") or line.startswith("    #"):
            block.append(line)
            continue
        break
    return block


class CryptographyLockTests(unittest.TestCase):
    def test_requirements_pin_matches_a_clean_interpreter(self):
        block = cryptography_block((ROOT / "requirements.txt").read_text(encoding="utf-8"))
        self.assertTrue(block[0].startswith(f"cryptography=={PIN}"))
        self.assertNotIn(">", block[0])
        hashes = [line.split("sha256:", 1)[1].replace("\\", "").strip() for line in block if "--hash=sha256:" in line]
        self.assertIn(SDIST_HASH, hashes)
        self.assertGreaterEqual(len(hashes), 20)
        self.assertNotIn("cryptography", (ROOT / "requirements-runtime.in").read_text(encoding="utf-8"))
        self.assertNotIn("cryptography", (ROOT / "requirements-dev.in").read_text(encoding="utf-8"))
        self.assertEqual(importlib.metadata.version("cryptography"), PIN)
        probe = subprocess.run(
            [sys.executable, "-I", "-c", "import importlib.metadata; print(importlib.metadata.version('cryptography'))"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout.strip(), PIN)


if __name__ == "__main__":
    unittest.main()
