"""Lock and clean-prefix install check for cryptography==41.0.7.

Compatibility: the isolated Ed25519 verifier imports this exact version.
The pin is not a range, and a newer release is not treated as compatible.

Security maintenance: keep 41.0.7 until a reviewed change replaces the
requirements.txt pin and its hashes in the same commit. Advisories published
after 41.0.7 are not closed by staying on this pin. Closing them is that
reviewed lock update, not a silent upgrade and not a reason to enable
production verification. Do not remove the hashes. Do not install this pin
into a host or production prefix.

A `python -I` version print from the interpreter that is already running
these tests is not an install verification. The install check creates an
empty directory outside the host prefix, installs the hashed requirement
there with ``--ignore-installed``, and reads only that directory.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
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
    def test_requirements_pin_is_exact_and_hashed(self):
        block = cryptography_block((ROOT / "requirements.txt").read_text(encoding="utf-8"))
        self.assertTrue(block[0].startswith(f"cryptography=={PIN}"))
        self.assertNotIn(">", block[0])
        hashes = [line.split("sha256:", 1)[1].replace("\\", "").strip() for line in block if "--hash=sha256:" in line]
        self.assertIn(SDIST_HASH, hashes)
        self.assertGreaterEqual(len(hashes), 20)
        self.assertNotIn("cryptography", (ROOT / "requirements-runtime.in").read_text(encoding="utf-8"))
        self.assertNotIn("cryptography", (ROOT / "requirements-dev.in").read_text(encoding="utf-8"))
        policy = (ROOT / "tests" / "test_v060_w02_cryptography_lock.py").read_text(encoding="utf-8")
        self.assertIn("41.0.7", policy)
        self.assertIn("reviewed lock update", policy)

    def test_hashed_pin_installs_into_a_clean_prefix(self):
        block = cryptography_block((ROOT / "requirements.txt").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="w02-crypto-") as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            requirement = Path(tmp) / "cryptography.txt"
            requirement.write_text("\n".join(block) + "\n", encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONNOUSERSITE"] = "1"
            installed = subprocess.run(
                [
                    sys.executable, "-m", "pip", "install",
                    "--target", str(target),
                    "--ignore-installed",
                    "--require-hashes",
                    "--no-deps",
                    "--disable-pip-version-check",
                    "-r", str(requirement),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            distinfo = list(target.glob(f"cryptography-{PIN}.dist-info"))
            self.assertEqual(len(distinfo), 1, [path.name for path in target.iterdir()])
            site = distinfo[0].parent
            self.assertEqual(site.resolve(), target.resolve())
            self.assertFalse(str(site.resolve()).startswith(sys.prefix))
            probe = textwrap.dedent(
                f"""
                import importlib.metadata
                import pathlib
                import sys
                sys.path.insert(0, {str(site)!r})
                dist = importlib.metadata.distribution("cryptography")
                print(dist.version)
                print(pathlib.Path(dist.locate_file("")).resolve())
                """
            )
            version = subprocess.run(
                [sys.executable, "-I", "-c", probe],
                check=False,
                capture_output=True,
                text=True,
                env={"PATH": env.get("PATH", ""), "PYTHONNOUSERSITE": "1"},
            )
            self.assertEqual(version.returncode, 0, version.stderr)
            lines = [line.strip() for line in version.stdout.splitlines() if line.strip()]
            self.assertEqual(lines[0], PIN)
            self.assertTrue(lines[1].startswith(str(target.resolve())))
            self.assertFalse(lines[1].startswith(sys.prefix))


if __name__ == "__main__":
    unittest.main()
