"""Isolated W07 adapter entry.

Default deny is the absence of ``V060_JOINT_EXECUTE=isolated`` and a private
temporary root. The root is opened by file descriptor. This module does not
treat the text ``/tmp`` as proof of isolation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from app.control_diagnostics.safe_io import UnsafePath, proc_fd_path, read_bounded
from app.control_joint.root import temporary_root
from app.control_joint.safe_root import BoundRoot
from app.control_joint.w07_driver import IsolatedDeploy, public_line


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    command = arguments[0] if arguments else "status"
    if os.environ.get("V060_JOINT_EXECUTE") != "isolated":
        print("BLOCKED: joint deploy adapter is not authorized", file=sys.stderr)
        return 2
    if command not in {"status", "plan", "record"}:
        print("BLOCKED: command is not in the isolated allowlist", file=sys.stderr)
        return 2
    checked = temporary_root()
    if not checked["ok"]:
        print("BLOCKED: " + checked["reason"], file=sys.stderr)
        return 2
    try:
        root = BoundRoot.open(checked["path"])
    except (UnsafePath, OSError):
        print("BLOCKED: control root is not a real temporary directory", file=sys.stderr)
        return 2
    try:
        if command == "status":
            return _status(root)
        child = root.mkdir("w07")
        try:
            drive = IsolatedDeploy(Path(proc_fd_path(child)))
            try:
                if command == "plan":
                    result = drive.call("plan", "idem-w11-plan")
                    ok = result.state == "PLAN" and result.http == 200
                else:
                    result = drive.recover_unknown_switch()
                    ok = result.get("state") == "SAFE_FROZEN" and result.get("real_host") is False and result.get("switched_again") is False
            finally:
                drive.close()
        finally:
            os.close(child)
        if not ok:
            print("BLOCKED: isolated deploy machine did not reach a closed state", file=sys.stderr)
            return 2
        if command == "record":
            caller = os.environ.get("V060_JOINT_CALLER") or "adapter"
            if caller not in {"adapter", "manage.sh"} or "\n" in caller:
                print("BLOCKED: caller is not in the isolated allowlist", file=sys.stderr)
                return 2
            root.append_line("control-journal.tsv", public_line(result if isinstance(result, dict) else {"state": "PLAN", "code": None}, caller))
        sys.stdout.write(command + "\n")
        return 0
    except (UnsafePath, OSError):
        print("BLOCKED: isolated journal write was refused", file=sys.stderr)
        return 2
    finally:
        root.close()


def _status(root: BoundRoot) -> int:
    try:
        raw = read_bounded(root.fd, "control-journal.tsv", 262144)
    except FileNotFoundError:
        sys.stdout.write("journal-empty\n")
        return 0
    except UnsafePath:
        print("BLOCKED: control journal is not a private regular file", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
