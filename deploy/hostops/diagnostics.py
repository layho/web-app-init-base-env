"""HostOps phase 1: local, read-only, bounded diagnostics.

No network listener, credentials, shell, or service mutation. Transport and
installation are separate reviewed phases.
"""
import argparse
import json
import os
import platform
import re
import shutil
import subprocess
from datetime import datetime, timezone

SERVICES = {
    "macci.runner": "com.layho.macci",
    "macci.preview": "com.layho.macci-preview-merge",
    "macci.publish": "com.layho.macci-main-publish",
    "macci.receipts": "com.layho.macci-receipt-delivery",
    "panel.merge": "com.layho.macci-app-main-panel",
}
OPERATIONS = frozenset({"host.health", "service.inventory", "agent.snapshot"})


def health():
    try:
        total, used, free = shutil.disk_usage(os.path.expanduser("~"))
        disk = {"total_bytes": total, "used_bytes": used, "free_bytes": free}
    except OSError:
        disk = None
    return {
        "system": platform.system(),
        "architecture": platform.machine(),
        "disk": disk,
        "cpu_percent": None,
        "memory_percent": None,
    }


def services():
    if platform.system() != "Darwin":
        return [{"service_id": key, "state": "unsupported"} for key in SERVICES]
    # launchctl list returns a compact PID/status/label table. Do not expose
    # launchctl print output (which may include process environment values).
    try:
        result = subprocess.run(
            ["/bin/launchctl", "list"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=5, check=False,
            env={"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~")},
        )
        if result.returncode != 0 or len(result.stdout) > 262144:
            raise ValueError("inventory unavailable")
    except (OSError, subprocess.SubprocessError, ValueError):
        return [{"service_id": key, "state": "unknown"} for key in SERVICES]
    rows = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[2] in SERVICES.values():
            pid, status, label = fields
            if re.fullmatch(r"(?:-|[0-9]{1,10})", pid) and re.fullmatch(r"-?[0-9]{1,10}", status):
                rows[label] = (pid, int(status))
    output = []
    for key, label in SERVICES.items():
        row = rows.get(label)
        if row is None:
            state = "not_visible"
        elif row[0] != "-":
            state = "running"
        elif row[1] == 0:
            state = "loaded_idle"
        else:
            state = "last_exit_failed"
        output.append({"service_id": key, "state": state,
                       "last_exit_code": row[1] if row else None})
    return output


def execute(operation, parameters):
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise ValueError("unsupported operation")
    if not isinstance(parameters, dict) or parameters:
        raise ValueError("operation accepts no parameters")
    if operation == "host.health":
        payload = health()
    elif operation == "service.inventory":
        payload = services()
    else:
        payload = {"health": health(), "services": services(),
                   "capabilities": sorted(OPERATIONS), "transport": "local_only"}
    return {"schema": 1, "operation": operation,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "result": payload}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=sorted(OPERATIONS))
    args = parser.parse_args()
    print(json.dumps(execute(args.operation, {}), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
