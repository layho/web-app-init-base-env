"""Read the allowlisted operational events written by the Mac launcher."""
from collections import deque
from datetime import datetime
from pathlib import Path
import os
import re
import subprocess

OPERATIONS_LOG = Path(__file__).resolve().parents[1] / "logs" / "operations.tsv"

EVENT_CODES = {
    "startup_ok", "schema_check_failed", "frontend_missing",
    "install_ok", "install_failed", "update_ok", "update_blocked",
    "rollback", "feedback_ok", "feedback_failed",
}


def recent_operations(path: Path, *, deployments_only: bool = False) -> list[dict]:
    events = deque(maxlen=100)
    try:
        stream = path.open(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    with stream:
        for line in stream:
            fields = line.rstrip("\n").split("\t")
            if len(fields) != 3:
                continue
            timestamp, code, sha = fields
            if code not in EVENT_CODES or not re.fullmatch(r"[0-9a-f]{40}", sha):
                continue
            if deployments_only and code not in {"install_ok", "update_ok"}:
                continue
            try:
                instant = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                continue
            if instant.tzinfo is None:
                continue
            events.append({"at": instant.isoformat(), "code": code, "sha": sha})
    return list(reversed(events))


def updater_target() -> str | None:
    if os.getenv("APP_INIT_UPDATE_SIGNAL") == "1":
        return "local-signal"
    target = os.getenv("APP_UPDATER_TARGET", "")
    allowed = rf"gui/{os.getuid()}/com\.example\.webapp(?:\.acceptance\.[A-Za-z0-9_]+)?\.updater"
    return target if re.fullmatch(allowed, target) else None


def request_update(target: str):
    if target == "local-signal":
        with (OPERATIONS_LOG.parent / "update-request").open("ab") as signal:
            signal.write(b"1")
        return
    subprocess.run(
        ["/bin/launchctl", "kickstart", target],
        check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
