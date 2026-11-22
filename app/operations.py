"""Read the allowlisted operational events written by the Mac launcher."""
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import os
import re
import subprocess
import time
import stat

OPERATIONS_LOG = Path(__file__).resolve().parents[1] / "logs" / "operations.tsv"

EVENT_CODES = {
    "startup_ok", "schema_check_failed", "frontend_missing",
    "install_ok", "install_failed", "update_ok", "update_blocked",
    "rollback", "rollback_ok", "rollback_failed", "feedback_ok", "feedback_failed",
    "update_started", "update_candidate", "update_dependencies", "update_test_build", "update_restarting",
    "update_locked", "update_no_change", "update_held", "update_preflight_failed", "update_fetch_failed",
    "update_dependencies_failed", "update_schema_failed", "update_test_build_failed", "update_restart_failed",
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


def check_available_update(running_sha: str) -> dict:
    """Read-only remote ref query. Never fetch, create update signals or deploy."""
    root = Path(__file__).resolve().parents[1]
    branch = "preview" if os.getenv("APP_BASE_PREVIEW") == "1" else "main"
    if not re.fullmatch(r"[0-9a-f]{40}", running_sha):
        return {"state": "unavailable", "branch": branch, "reason": "运行 SHA 不可用"}
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"],
            capture_output=True, text=True, timeout=10, check=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
        lines = result.stdout.strip().splitlines()
        if len(lines) != 1:
            raise ValueError("Invalid remote response")
        sha, ref = lines[0].split()
        if ref != f"refs/heads/{branch}" or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("Invalid remote SHA")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"state": "unavailable", "branch": branch, "reason": "无法读取远程版本，请检查网络或仓库凭据"}
    return {"state": "available" if sha != running_sha else "current",
            "branch": branch, "running_sha": running_sha, "remote_sha": sha}


def request_update(target: str):
    if target == "local-signal":
        with (OPERATIONS_LOG.parent / "update-request").open("ab") as signal:
            signal.write(b"1")
        return
    subprocess.run(
        ["/bin/launchctl", "kickstart", target],
        check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


UPDATE_PROGRESS = {"update_started", "update_candidate", "update_dependencies", "update_test_build", "update_restarting"}
UPDATE_TERMINAL = {"update_ok", "update_no_change", "update_held", "update_preflight_failed",
                   "update_fetch_failed", "update_dependencies_failed", "update_schema_failed",
                   "update_test_build_failed", "update_restart_failed"}


def deploy_status(running_sha):
    """Only allowlisted events, never raw logs or arbitrary host paths."""
    events = recent_operations(OPERATIONS_LOG)
    root = OPERATIONS_LOG.parent.parent
    request = OPERATIONS_LOG.parent / "update-request"
    try:
        st = request.lstat()
        requested = st.st_mtime if stat.S_ISREG(st.st_mode) else None
    except OSError:
        requested = None
    # Lock observations do not override the active worker's progress.
    last = next((e for e in events if e["code"] in UPDATE_PROGRESS | UPDATE_TERMINAL), None)
    rollback_failed = next((e for e in events if e["code"] in {"rollback_ok", "rollback_failed", "update_started"}), None)
    # Existing WatchPaths requests predate the new event format. Treat them
    # as unacknowledged rather than inventing an execution result.
    stamp = datetime.fromisoformat(last["at"]).timestamp() if last else 0
    age = max(0, time.time() - stamp) if last else None
    locked = (root / ".webapp-deploy.lock").is_dir()
    pending = requested is not None and requested > stamp + 1
    if pending and not (locked and last and last["code"] in UPDATE_PROGRESS):
        state = "request_waiting" if time.time() - requested < 60 else "request_unacknowledged"
    elif last is None:
        state = "unknown"
    elif last["code"] in UPDATE_PROGRESS:
        state = last["code"] if locked else "update_interrupted"
    elif last["code"] in {"update_ok", "update_no_change"} and last["sha"] != running_sha:
        state = "running_sha_mismatch"
    else:
        state = last["code"]
    return {"state": state, "event": last, "locked": locked, "age_seconds": age,
            "requested_at": datetime.fromtimestamp(requested, timezone.utc).isoformat() if requested is not None else None,
            "running_sha": running_sha,
            "rollback_failed": bool(rollback_failed and rollback_failed["code"] == "rollback_failed")}
