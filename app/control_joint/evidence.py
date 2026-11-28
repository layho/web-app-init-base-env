"""74-item evidence against the entries this process actually ran.

Package-level invocation is not capability evidence. Until per-item assertions
are wired, all capability rows remain blocked.
"""

from __future__ import annotations

import json
from pathlib import Path

_ITEMS = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "v060" / "w00" / "items.json"

# These stay blocked even when a neighboring entry ran. A11 is branch
# protection, which this process does not read.
ENVIRONMENT_BLOCKED = frozenset({"F11", "U09", "S10", "D10", "A12", "I12", "R10", "A11"})

UNCONNECTED_ENTRIES = (
    "app.main W01-W10 route mounts",
    "manage.sh update",
    "deploy/app-init/app_init.py install",
    "GitHub remote client",
    "MacCI runner",
    "GitHub branch protection for main and preview",
    "execute_isolated merge",
    "execute_isolated deploy",
    "execute_isolated request_publish",
    "host service switch",
)


def evidence_matrix(exercised: dict[str, str]) -> dict:
    """Build one row per capability item.

    ``exercised`` is diagnostic only, mapping package ids to invoked entries.
    No capability is promoted by a package-level invocation.
    """

    payload = json.loads(_ITEMS.read_text(encoding="utf-8"))
    items = []
    connected = []
    for item in payload["items"]:
        package = item["package"]
        # A diagnostic entry only proves a package was invoked. It does not
        # prove the specific capability or the same-candidate workflow.
        entry = None
        blocked = True
        if item["id"] == "A11":
            reason = "branch protection was not read in this run"
        elif item["id"] in ENVIRONMENT_BLOCKED:
            reason = "MacCI, two-platform host evidence, or a public running SHA is not available"
        elif not entry:
            reason = "no independently asserted per-item runtime evidence"
        else:
            reason = "isolated entry returned; this is not a MacCI receipt or a public running SHA"
        row = {
            "id": item["id"],
            "package": package,
            "status": "blocked" if blocked else "evidence",
            "entry": None if blocked else entry,
            "macci_receipt": False,
            "public_running_sha": None,
            "production_enabled": False,
            "reason": reason,
        }
        items.append(row)
        if row["status"] == "evidence":
            connected.append(package)
    unconnected = tuple(
        package for package in sorted({item["package"] for item in payload["items"]})
        if package not in connected
    )
    return {
        "items": items,
        "counts": {
            "items": len(items),
            "evidence": sum(1 for row in items if row["status"] == "evidence"),
            "blocked": sum(1 for row in items if row["status"] == "blocked"),
        },
        "unconnected_packages": unconnected,
        "unconnected_entries": UNCONNECTED_ENTRIES,
        "macci_receipt": False,
        "public_running_sha": None,
        "branch_protection_verified": False,
    }
