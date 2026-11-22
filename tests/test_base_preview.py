"""Independent preview configuration tests; never operate on production services."""
import importlib.util
from pathlib import Path
import socket

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("base_preview", ROOT / "deploy/base_preview.py")
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


def test_preview_config_roundtrip(tmp_path):
    path = tmp_path / ".base-preview.env"
    preview.app_init.shell_config(path, {"BASE_PREVIEW_PORT": "28001"})
    assert preview.read_private_port(path) == 28001


@pytest.mark.parametrize("value", ["BASE_PREVIEW_PORT=0", "BASE_PREVIEW_PORT=29000", "BASE_PREVIEW_PORT=18001\necho unsafe"])
def test_preview_config_rejects_unexpected_contents(tmp_path, value):
    path = tmp_path / ".base-preview.env"
    path.write_text(value)
    path.chmod(0o600)
    with pytest.raises(RuntimeError):
        preview.read_private_port(path)


def test_preview_config_requires_private_permissions(tmp_path):
    path = tmp_path / ".base-preview.env"
    path.write_text("BASE_PREVIEW_PORT=18001\n")
    path.chmod(0o644)
    with pytest.raises(RuntimeError):
        preview.read_private_port(path)


def test_port_collision_does_not_terminate_listener():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(RuntimeError, match="端口"):
            preview.check_free(port)
        assert listener.getsockname()[1] == port
        # macOS does not expose SO_ACCEPTCONN through getsockopt.
        # Verify the original socket still accepts connections and transfers bytes.
        listener.settimeout(2)
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(2)
                client.sendall(b"x")
                assert connection.recv(1) == b"x"
                connection.sendall(b"y")
                assert client.recv(1) == b"y"


def test_preview_separate_from_normal_generated_app():
    manager = (ROOT / "deploy/macmini/manage.sh").read_text()
    assert "TRACK_BRANCH=main" in manager
    assert "TRACK_BRANCH=preview" in manager
    assert 'git fetch --quiet origin "${TRACK_BRANCH:-main}"' in manager
    assert preview.TRACK == "preview"
    assert preview.LABEL == "com.layho.basepreview"
