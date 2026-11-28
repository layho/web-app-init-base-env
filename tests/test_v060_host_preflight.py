import json
import os
from pathlib import Path
import subprocess


ENTRY = Path(__file__).resolve().parents[1] / "deploy/app-init/app-init"


def test_real_preflight_reports_host_and_preserves_home(tmp_path):
    result = subprocess.run(["bash", str(ENTRY), "--host-preflight"],
                            env={**os.environ, "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"},
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
    report = json.loads(result.stdout)
    assert report["platform"]["os"] == os.uname().sysname.lower()
    assert result.returncode == (0 if report["checks_passed"] else 2)
    assert report["host_mutated"] is False
    assert not list(tmp_path.iterdir())


def test_preflight_rejects_install_flag():
    result = subprocess.run(["bash", str(ENTRY), "--install-deps", "--host-preflight"],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert "must run alone" in result.stderr
