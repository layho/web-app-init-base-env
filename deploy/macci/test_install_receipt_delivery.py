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
