from pathlib import Path
import subprocess

SCRIPT = Path(__file__).with_name("install-receipt-delivery.sh")


def test_receipt_agent_installer_has_shell_syntax():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_receipt_agent_is_independent_and_never_runs_candidate_tests():
    source = SCRIPT.read_text()
    assert 'label="com.layho.macci-receipt-delivery"' in source
    assert '"ProgramArguments": [python, root+"/runner.py", "--receipts-only"]' in source
    assert '"StartInterval": 60' in source
    assert '"RunAtLoad": True' in source
    assert 'launchctl bootstrap "$domain" "$plist"' in source
    assert '"worker.lock"' not in source
    assert "docker_command" not in source
    assert "test_candidate" not in source


def test_installer_rejects_missing_or_invalid_digest():
    for args in ([], ["not-a-digest"], ["a" * 64, "extra"]):
        result = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True)
        assert result.returncode == 2
        assert "independently verified runner SHA-256" in result.stdout


def test_runner_digest_validation_executes_without_launchctl(tmp_path):
    import hashlib
    import os
    import sys

    source = SCRIPT.read_text()
    validation = source.split('"$python" - "$root" "$expected_hash" <<\'PY\'\n', 1)[1].split('\nPY', 1)[0]
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    runner = root / "runner.py"
    runner.write_bytes(b"# reviewed runner\n")
    runner.chmod(0o600)
    token = root / "github-token"
    token.write_text("fixture")
    token.chmod(0o600)
    env = dict(os.environ, HOME=str(tmp_path))
    digest = hashlib.sha256(runner.read_bytes()).hexdigest()
    def check(value):
        return subprocess.run([sys.executable, "-c", validation, str(root), value], env=env, capture_output=True, text=True)
    assert check(digest).returncode == 0
    wrong = check("0" * 64)
    assert wrong.returncode != 0
    assert "SHA-256 mismatch" in wrong.stderr
    runner.unlink()
    runner.symlink_to(token)
    assert check(digest).returncode != 0
