"""Real loopback sockets + mocked process inspection; no production services."""
import importlib.util
from pathlib import Path
import socket
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_port", ROOT / "scripts/check_port.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


@pytest.fixture
def bound_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        yield listener.getsockname()[1]


def test_free_port_does_not_query_processes(tmp_path, monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(guard, "_listener_pids", lambda _: pytest.fail("no process query expected"))
    assert guard.check_port(tmp_path, port) == "free"


def test_occupied_other_app_never_allowed(tmp_path, bound_port, monkeypatch):
    monkeypatch.setattr(guard, "_listener_pids", lambda _: {4123})
    monkeypatch.setattr(guard, "_owner_pid", lambda _: 5123)
    with pytest.raises(RuntimeError, match=r"端口冲突.*4123"):
        guard.check_port(tmp_path, bound_port)


def test_existing_own_web_pid_is_allowed(tmp_path, bound_port, monkeypatch):
    monkeypatch.setattr(guard, "_listener_pids", lambda _: {4123})
    monkeypatch.setattr(guard, "_owner_pid", lambda _: 4123)
    monkeypatch.setattr(guard, "_belongs_to_app", lambda *args: True)
    assert guard.check_port(tmp_path, bound_port) == "owned"


@pytest.mark.parametrize("pids", [{4123, 5123}, {5123}])
def test_multiple_or_mismatched_listeners_fail(tmp_path, bound_port, monkeypatch, pids):
    monkeypatch.setattr(guard, "_listener_pids", lambda _: pids)
    monkeypatch.setattr(guard, "_owner_pid", lambda _: 4123)
    monkeypatch.setattr(guard, "_belongs_to_app", lambda *args: True)
    with pytest.raises(RuntimeError, match="端口冲突"):
        guard.check_port(tmp_path, bound_port)


def test_stale_pid_fails_even_when_same_number(tmp_path, bound_port, monkeypatch):
    monkeypatch.setattr(guard, "_listener_pids", lambda _: {4123})
    monkeypatch.setattr(guard, "_owner_pid", lambda _: 4123)
    monkeypatch.setattr(guard, "_belongs_to_app", lambda *args: False)
    with pytest.raises(RuntimeError, match="端口冲突"):
        guard.check_port(tmp_path, bound_port)


@pytest.mark.parametrize("value", ["", "oops", "-1", "123 trailing", "0"])
def test_pid_file_rejects_invalid_values(tmp_path, value):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "web.pid").write_text(value)
    assert guard._owner_pid(tmp_path) is None


def test_pid_file_rejects_symlink(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    other = tmp_path / "other"
    other.write_text("4123")
    (logs / "web.pid").symlink_to(other)
    assert guard._owner_pid(tmp_path) is None


def test_identity_requires_matching_working_directory_and_command(tmp_path, monkeypatch):
    def fake(*args):
        if args[0] == "lsof":
            return subprocess.CompletedProcess(args, 0, f"p4123\\nfcwd\\nn{tmp_path}\\n", "")
        return subprocess.CompletedProcess(
            args, 0,
            ".venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 18500 --workers 1", "",
        )
    monkeypatch.setattr(guard, "_command", fake)
    assert guard._belongs_to_app(4123, tmp_path, 18500)
    assert not guard._belongs_to_app(4123, tmp_path / "other", 18500)
    assert not guard._belongs_to_app(4123, tmp_path, 18501)


def test_lsof_inspection_failure_is_not_treated_as_free(monkeypatch):
    monkeypatch.setattr(
        guard, "_command",
        lambda *args: subprocess.CompletedProcess(args, 1, "", "permission denied"),
    )
    with pytest.raises(RuntimeError, match="无法确定监听进程"):
        guard._listener_pids(18500)


def test_cli_reports_collision_and_does_not_touch_port(tmp_path, bound_port, monkeypatch, capsys):
    monkeypatch.setattr(guard, "_listener_pids", lambda _: {9922})
    monkeypatch.setattr(guard, "_owner_pid", lambda _: None)
    assert guard.main(["--root", str(tmp_path), "--port", str(bound_port)]) == 1
    assert "端口冲突" in capsys.readouterr().err


def test_app_init_and_web_service_both_call_guard():
    installer = (ROOT / "deploy/app-init/app_init.py").read_text()
    manager = (ROOT / "deploy/macmini/manage.sh").read_text()
    assert '"scripts/check_port.py"' in installer
    assert "root / \"scripts/check_port.py\"" in installer
    assert "scripts/check_port.py --root \"$ROOT\" --port \"$PORT\"" in manager
