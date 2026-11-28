"""Isolated lifecycle adapter.

Default deny is the absence of an isolated temporary root. ``plan`` and
``record`` drive the W09 lifecycle machine inside that root. They do not
install, uninstall, or switch a service. Writes go through a directory file
descriptor and refuse a symlink, hard link, or FIFO.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from app.control_diagnostics.safe_io import UnsafePath
from app.control_joint.root import temporary_root
from app.control_joint.w09_driver import drive, read_status


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    command = arguments[0] if arguments else "status"
    if os.environ.get("V060_JOINT_EXECUTE") != "isolated":
        print("BLOCKED: app-init lifecycle adapter is not authorized to install or remove anything", file=sys.stderr)
        return 2
    if command not in {"status", "plan", "record"}:
        print("BLOCKED: command is not in the isolated allowlist", file=sys.stderr)
        return 2
    checked = temporary_root()
    if not checked["ok"]:
        print("BLOCKED: " + checked["reason"], file=sys.stderr)
        return 2
    try:
        if command == "status":
            body = read_status(checked["path"])
            if body is None:
                print("journal-empty")
            else:
                sys.stdout.buffer.write(body)
                if not body.endswith(b"\n"):
                    sys.stdout.buffer.write(b"\n")
            return 0
        result = drive(checked["path"], command)
    except (UnsafePath, OSError, ValueError):
        print("BLOCKED: isolated lifecycle root was refused", file=sys.stderr)
        return 2
    if not result["ok"] or result["install"] or result["service_switch"] or result["host_side_effect"]:
        print("BLOCKED: lifecycle machine did not stay closed", file=sys.stderr)
        return 2
    sys.stdout.write(command + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
