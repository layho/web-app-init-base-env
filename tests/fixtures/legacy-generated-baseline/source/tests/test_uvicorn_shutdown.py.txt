"""Exercise real Uvicorn shutdown under an outstanding request, not a shell mock."""
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

@pytest.mark.skipif(os.name != "posix", reason="requires POSIX inherited socket")
def test_uvicorn_bounded_shutdown_with_active_request(tmp_path):
    script = (Path(__file__).resolve().parents[1] / "deploy/macmini/manage.sh").read_text()
    assert "--timeout-graceful-shutdown 5" in script
    (tmp_path / "slow.py").write_text(
        "import asyncio\nfrom pathlib import Path\n"
        "async def app(scope, receive, send):\n"
        "    if scope['type'] != 'http': return\n"
        "    Path('request-started').touch()\n"
        "    await asyncio.sleep(90)\n"
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port = listener.getsockname()[1]
        child = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "slow:app", "--fd", str(listener.fileno()),
             "--lifespan", "off", "--timeout-graceful-shutdown", "5"],
            cwd=tmp_path, pass_fds=[listener.fileno()],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
                client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                deadline = time.monotonic() + 10
                while not (tmp_path / "request-started").exists():
                    assert child.poll() is None
                    assert time.monotonic() < deadline, "request did not start"
                    time.sleep(.05)
                start = time.monotonic()
                child.send_signal(signal.SIGTERM)
                child.wait(timeout=12)
                assert time.monotonic() - start < 12
        finally:
            if child.poll() is None:
                child.kill()
            _, stderr = child.communicate(timeout=5)
        assert b"timeout graceful shutdown exceeded" in stderr.lower(), stderr.decode(errors="replace")
