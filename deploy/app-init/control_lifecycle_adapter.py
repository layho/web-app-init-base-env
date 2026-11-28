"""Isolated lifecycle adapter.

Default deny is the absence of an isolated temporary root. The commands below
only read or append files inside that root. They do not install, uninstall,
or switch a service.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    command = arguments[0] if arguments else "status"
    allowed = _temporary_root()
    if not allowed["ok"]:
        print("BLOCKED: " + allowed["reason"], file=sys.stderr)
        return 2
    if command not in {"status", "plan", "record"}:
        print("BLOCKED: command is not in the isolated allowlist", file=sys.stderr)
        return 2
    root = allowed["path"]
    journal = root / "lifecycle-journal.tsv"
    if command == "status":
        if journal.is_file() and not journal.is_symlink():
            sys.stdout.write(journal.read_text(encoding="utf-8"))
        else:
            print("journal-empty")
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp}\t{command}\tno-install\n")
    if command == "plan":
        plan = root / "lifecycle-plan.json"
        plan.write_text(json.dumps({
            "command": "plan",
            "install": False,
            "uninstall": False,
            "privilege": False,
            "service_switch": False,
            "root": str(root),
        }, sort_keys=True), encoding="utf-8")
    print(command)
    return 0


def _temporary_root() -> dict:
    import os
    if os.environ.get("V060_JOINT_EXECUTE") != "isolated":
        return {"ok": False, "reason": "app-init lifecycle adapter is not authorized to install or remove anything"}
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo))
    from app.control_joint.root import temporary_root

    checked = temporary_root()
    if not checked["ok"]:
        return {"ok": False, "reason": checked["reason"]}
    return checked


if __name__ == "__main__":
    raise SystemExit(main())
