"""Read authorized W01, W04, W05, and W06 rows for one server grant.

Rows come from the configured stores. An empty list means those stores had
no row inside the grant. Missing configuration stays not_configured.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from app.control_authz.authorize import authorize
from app.control_authz.errors import AuthzError
from app.control_authz.principal import Principal, ResourceBinding, server_source
from app.control_board.projection import project
from app.control_joint.actions import ManualActions
from app.control_joint.capability import host_capability
from app.control_joint.root import inspect_root

_GRANT = "authorization.json"


def read_board(username: str) -> dict:
    checked = inspect_root()
    if not checked["ok"]:
        return _status(checked["state"], checked["reason"])
    root = checked["path"]
    grant = _grant(root)
    if grant.get("error"):
        return _status(grant["state"], grant["reason"])
    if grant.get("reader_username") != username:
        return _denied("The signed-in user is not the configured control reader.")
    try:
        principal = _principal(grant)
        tasks = _task_ids(principal)
        if not tasks:
            return _status("empty", "The authorization grant does not name a task.")
        authorize(principal, "event.read", _read_binding(principal, sorted(tasks)[0]))
    except (AuthzError, TypeError, ValueError) as exc:
        reason = exc.detail if isinstance(exc, AuthzError) else "The authorization grant is not usable."
        return _denied(reason)
    events = _events(root, principal, tasks)
    schedule = _schedule(root, principal.app_id, tasks)
    diagnostics = _diagnostics(root, principal.app_id, tasks)
    delivery = _delivery(root, principal, tasks)
    shells = [(principal.app_id, task_id) for task_id in tasks if task_id in diagnostics or task_id in delivery]
    projected = project(events, schedule, shells)
    items = []
    history = []
    failures = []
    tests = []
    for task_id in sorted(tasks):
        view = projected["tasks"].get((principal.app_id, task_id))
        if view is None:
            continue
        signer = schedule["signers"].get(task_id)
        if signer and view["owner"]["evidence"] == "missing":
            view["owner"] = {"value": signer, "evidence": "recorded", "display": signer}
        if task_id in delivery:
            view["delivery"] = delivery[task_id]
        for failure in diagnostics.get(task_id, []):
            view["advisory_failures"].append(failure)
            failures.append(failure)
        occurred = [row.get("occurred_at") for row in view["history"] if isinstance(row.get("occurred_at"), str)]
        view["recorded_at"] = occurred[-1] if occurred else None
        items.append(view)
        history.extend(view["history"])
        tests.extend(view.get("tests") or [])
        failures.extend(view.get("failure_reasons") or [])
    state = "empty" if not items and not history and not failures else "available"
    if projected["gap"]:
        state = "partial"
    actions = ManualActions(root)
    buttons = []
    for task_id in sorted(tasks):
        buttons.extend(actions.describe(principal, task_id))
    body = _status(state, "授权范围内的记录来自已配置的控制根。没有记录时列表为空。")
    body.update({
        "http": 200,
        "items": items,
        "history": history,
        "failures": failures,
        "tests": tests,
        "actions": buttons,
        "held": projected["held"],
        "rejected": projected["rejected"],
        "gap": projected["gap"],
        "sources": {
            "events": (root / "events.sqlite3").is_file(),
            "schedule": (root / "scheduler" / "scheduler.sqlite3").is_file(),
            "diagnostics": (root / "diagnostics").is_dir(),
            "delivery": (root / "delivery").is_dir(),
        },
    })
    return body


def submit_action(username: str, body: dict) -> dict:
    checked = inspect_root()
    if not checked["ok"]:
        return {
            "schema_version": 2,
            "http": 409,
            "code": "NOT_CONFIGURED",
            "detail": checked["reason"],
            "side_effect": False,
            "accepted_as_success": False,
        }
    grant = _grant(checked["path"])
    if grant.get("error"):
        return {
            "schema_version": 2,
            "http": 409,
            "code": "NOT_CONFIGURED",
            "detail": grant["reason"],
            "side_effect": False,
            "accepted_as_success": False,
        }
    if grant.get("reader_username") != username:
        return _denied("The signed-in user is not the configured control reader.")
    try:
        principal = _principal(grant)
    except (TypeError, ValueError):
        return _denied("The authorization grant is not usable.")
    return ManualActions(checked["path"]).submit(principal, body if isinstance(body, dict) else {})


def _grant(root: Path) -> dict:
    path = root / _GRANT
    if not path.exists():
        return {
            "error": True,
            "state": "not_configured",
            "reason": "No authorization grant is configured for this control root.",
        }
    try:
        info = path.lstat()
    except OSError:
        return {"error": True, "state": "blocked", "reason": "The authorization grant cannot be read."}
    if stat_link(info) or not stat_file(info) or info.st_uid != os.getuid():
        return {"error": True, "state": "blocked", "reason": "The authorization grant must be a regular file owned by this user."}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"error": True, "state": "blocked", "reason": "The authorization grant is not valid JSON."}
    if not isinstance(document, dict):
        return {"error": True, "state": "blocked", "reason": "The authorization grant must be an object."}
    return document


def stat_link(info) -> bool:
    import stat
    return stat.S_ISLNK(info.st_mode)


def stat_file(info) -> bool:
    import stat
    return stat.S_ISREG(info.st_mode)


def _principal(grant: dict) -> Principal:
    kind = grant.get("kind")
    source = server_source(kind) if isinstance(kind, str) else ""
    scopes = grant.get("scopes")
    if not isinstance(scopes, list):
        raise ValueError("scopes")
    return Principal(
        principal_id=str(grant.get("principal_id") or ""),
        kind=str(kind or ""),
        repo_id=str(grant.get("repo_id") or ""),
        app_id=str(grant.get("app_id") or ""),
        producer_id=str(grant.get("producer_id") or ""),
        stream_id=str(grant.get("stream_id") or ""),
        scopes=frozenset(str(item) for item in scopes),
        trusted_source=source,
    )


def _task_ids(principal: Principal) -> set[str]:
    return {item[5:] for item in principal.scopes if item.startswith("task:") and len(item) > 5}


def _read_binding(principal: Principal, task_id: str) -> ResourceBinding:
    return ResourceBinding(
        repo_id=principal.repo_id,
        app_id=principal.app_id,
        producer_id=principal.producer_id,
        stream_id=principal.stream_id,
        operation="event.read",
        scope="event.read",
        task_id=task_id,
        target_app_id=principal.app_id,
    )


def _events(root: Path, principal: Principal, tasks: set[str]) -> list[dict]:
    path = root / "events.sqlite3"
    if not path.is_file() or path.is_symlink():
        return []
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT payload_json FROM events
            WHERE applied=1
            ORDER BY stream_id, stream_epoch, producer_seq
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    found = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("repo_id") != principal.repo_id or payload.get("app_id") != principal.app_id:
            continue
        task_id = payload.get("task_id") or payload.get("subject_id")
        if task_id not in tasks:
            continue
        if "read_receipt" not in payload:
            payload = dict(payload)
            payload.pop("received_at", None)
        found.append(payload)
    return found


def _schedule(root: Path, app_id: str, tasks: set[str]) -> dict:
    path = root / "scheduler" / "scheduler.sqlite3"
    empty = {"tasks": [], "chains": [], "rounds": [], "runtime_evidence": False, "test_double": False, "signers": {}}
    if not path.is_file() or path.is_symlink():
        return empty
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        task_rows = connection.execute(
            "SELECT task_id, app_id, version, acceptance_signer, closed FROM tasks"
        ).fetchall()
        chain_rows = connection.execute(
            "SELECT chain_id, task_id, candidate_id, state, priority, head, base, merge_sha, tree FROM chains"
        ).fetchall()
    except sqlite3.Error:
        return empty
    finally:
        connection.close()
    signers = {}
    visible = []
    allowed = set()
    for row in task_rows:
        if row["app_id"] != app_id or row["task_id"] not in tasks:
            continue
        allowed.add(row["task_id"])
        if isinstance(row["acceptance_signer"], str) and row["acceptance_signer"]:
            signers[row["task_id"]] = row["acceptance_signer"]
        visible.append({
            "task_id": row["task_id"],
            "app_id": row["app_id"],
            "version": row["version"],
            "closed": bool(row["closed"]),
        })
    chains = []
    for row in chain_rows:
        if row["task_id"] not in allowed:
            continue
        chains.append({
            "task_id": row["task_id"],
            "app_id": app_id,
            "candidate_id": row["candidate_id"],
            "state": row["state"],
            "priority": row["priority"],
            "head": row["head"],
            "base": row["base"],
            "merge": row["merge_sha"],
            "tree": row["tree"],
        })
    return {
        "tasks": visible,
        "chains": chains,
        "rounds": [],
        "runtime_evidence": False,
        "test_double": False,
        "signers": signers,
    }


def _diagnostics(root: Path, app_id: str, tasks: set[str]) -> dict:
    directory = root / "diagnostics"
    if not directory.is_dir() or directory.is_symlink():
        return {}
    from app.control_diagnostics.journal import Journal

    try:
        journal = Journal.open(directory, writable=False)
    except (OSError, ValueError):
        return {}
    try:
        records, _missing, _corrupt = journal.records()
    finally:
        if journal.root_fd is not None:
            os.close(journal.root_fd)
    grouped = {}
    for record in records:
        if record.get("app_id") != app_id:
            continue
        task_id = record.get("task_id") or record.get("subject_id")
        if task_id not in tasks:
            continue
        if record.get("severity") not in {"error", "warning"}:
            continue
        body = record.get("body") if isinstance(record.get("body"), dict) else {}
        text = body.get("failure_reason") or body.get("summary") or record.get("diagnostic_class") or "诊断记录"
        if not isinstance(text, str):
            text = "诊断记录"
        grouped.setdefault(task_id, []).append({
            "text": text,
            "display": text,
            "evidence": "recorded",
            "authoritative": False,
            "source": "w06",
            "at": record.get("received_at") if isinstance(record.get("received_at"), str) else None,
            "task_id": task_id,
        })
    return grouped


def _delivery(root: Path, principal: Principal, tasks: set[str]) -> dict:
    directory = root / "delivery"
    if not directory.is_dir() or directory.is_symlink():
        return {}
    folder = directory / "task"
    if not folder.is_dir() or folder.is_symlink():
        return {}
    found = {}
    for path in sorted(folder.iterdir()):
        if path.is_symlink() or path.suffix != ".json" or not path.is_file():
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        task_id = document.get("task_id")
        if task_id not in tasks:
            continue
        if document.get("repo_id") != principal.repo_id or document.get("task_id") != task_id:
            continue
        found[task_id] = {
            "phase": document.get("phase") if isinstance(document.get("phase"), str) else None,
            "revision": document.get("revision") if type(document.get("revision")) is int else None,
            "run_id": document.get("run_id") if isinstance(document.get("run_id"), str) else None,
            "authoritative": False,
            "source": "w05",
        }
    return found


def _status(state: str, reason: str) -> dict:
    return {
        "schema_version": 2,
        "side_effect": False,
        "production_enabled": False,
        "production_wired": False,
        "preview_merge_authorized": False,
        "main_release_authorized": False,
        "state": state,
        "reason": reason,
        "owner": None,
        "recorded_at": None,
        "items": [],
        "history": [],
        "failures": [],
        "tests": [],
        "actions": [],
        "macci": host_capability(),
    }


def _denied(reason: str) -> dict:
    body = _status("denied", reason)
    body["http"] = 403
    body["code"] = "NOT_AUTHORIZED"
    return body
