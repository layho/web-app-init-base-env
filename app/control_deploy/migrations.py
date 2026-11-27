"""Read-only migration identity check.

Filename equality is not enough. This function never opens a database and
never executes SQL. The updater path stays incapable of applying migrations.
"""

from app.control_deploy.contract_machine import digest_of


def check_migrations(spec):
    files = list(spec.get("files") or [])
    ledger = list(spec.get("ledger") or [])
    problems = []
    seen = set()
    for item in files:
        filename = item.get("filename")
        body = item.get("body")
        if not isinstance(filename, str) or not isinstance(body, str):
            problems.append({"filename": filename, "reason": "unreadable"})
            continue
        seen.add(filename)
        digest = digest_of(body.encode("utf-8"))
        rows = [row for row in ledger if row.get("filename") == filename]
        if not rows:
            problems.append({"filename": filename, "reason": "pending", "digest": digest})
            continue
        recorded = rows[0].get("digest")
        if recorded != digest:
            problems.append(
                {
                    "filename": filename,
                    "reason": "digest_mismatch",
                    "digest": digest,
                    "ledger_digest": recorded,
                }
            )
    for row in ledger:
        if row.get("filename") not in seen:
            problems.append({"filename": row.get("filename"), "reason": "ledger_without_file"})
    return {
        "ok": not problems,
        "executed": False,
        "held": False,
        "problems": problems,
    }
