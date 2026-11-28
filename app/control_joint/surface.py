"""Read authorized W01, W04, W05, and W06 rows for one server grant.

Each package is read through its query port. Every task is authorized on its
own. A caller-supplied read receipt is not authority. Missing, bad, and
truncated reads stay partial or blocked. An empty list is an authorized
scope with no rows.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.control_authz.authorize import authorize
from app.control_authz.errors import AuthzError
from app.control_authz.principal import Principal, ResourceBinding, server_source
from app.control_board.projection import project
from app.control_joint.delivery_read import DeliveryReadError, query_tasks
from app.control_diagnostics.redaction import redact_value
from app.control_events.trusted_read import TrustedReadError, query_applied
from app.control_joint.actions import ManualActions
from app.control_joint.capability import host_capability
from app.control_joint.root import inspect_root

_GRANT = "authorization.json"
_PAGES = 20


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
    except (AuthzError, TypeError, ValueError) as exc:
        reason = exc.detail if isinstance(exc, AuthzError) else "The authorization grant is not usable."
        return _denied(reason)
    allowed = []
    blocked_tasks = []
    for task_id in sorted(tasks):
        try:
            authorize(principal, "event.read", _read_binding(principal, task_id))
        except AuthzError:
            blocked_tasks.append(task_id)
            continue
        allowed.append(task_id)
    if not allowed:
        return _denied("The authorization grant does not allow this read.")
    partial = []
    blocked = []
    try:
        events, event_partial = _events(root, principal, set(allowed))
    except TrustedReadError:
        events = []
        event_partial = False
        blocked.append("events")
    if event_partial:
        partial.append("events")
    schedule, schedule_flag = _schedule(root, principal, set(allowed))
    if schedule_flag == "blocked":
        blocked.append("schedule")
    elif schedule_flag == "partial":
        partial.append("schedule")
    diagnostics, diagnostic_flag = _diagnostics(root, principal, set(allowed))
    if diagnostic_flag == "blocked":
        blocked.append("diagnostics")
    elif diagnostic_flag == "partial":
        partial.append("diagnostics")
    delivery, delivery_flag = _delivery(root, principal, set(allowed))
    if delivery_flag == "blocked":
        blocked.append("delivery")
    elif delivery_flag == "partial":
        partial.append("delivery")
    if blocked_tasks:
        partial.append("authorization")
    shells = [(principal.app_id, task_id) for task_id in allowed if task_id in diagnostics or task_id in delivery]
    projected = project(events, schedule, shells)
    items = []
    history = []
    failures = []
    tests = []
    for task_id in sorted(allowed):
        view = projected["tasks"].get((principal.app_id, task_id))
        if view is None:
            continue
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
    if blocked:
        state = "blocked"
    elif partial or projected["gap"]:
        state = "partial"
    elif not items and not history and not failures:
        state = "empty"
    else:
        state = "available"
    actions = ManualActions(root)
    buttons = []
    for task_id in sorted(allowed):
        buttons.extend(actions.describe(principal, task_id))
    reason = "授权范围内的记录来自各包的查询端口。没有记录时列表为空。"
    if blocked:
        reason = "一个或多个控制源无法读取，结果是 BLOCKED。"
    elif partial:
        reason = "记录不完整。缺失、损坏或被截断的部分保持 partial。"
    body = _status(state, reason)
    body.update({
        "http": 200 if not blocked else 409,
        "items": [] if blocked else items,
        "history": [] if blocked else history,
        "failures": [] if blocked else failures,
        "tests": [] if blocked else tests,
        "actions": [] if blocked else buttons,
        "held": projected["held"],
        "rejected": projected["rejected"],
        "gap": bool(projected["gap"] or partial or blocked),
        "partial_reasons": partial,
        "blocked_sources": blocked,
        "blocked_tasks": blocked_tasks,
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
    try:
        return ManualActions(checked["path"]).submit(principal, body if isinstance(body, dict) else {})
    except sqlite3.DatabaseError:
        return {
            "schema_version": 2,
            "http": 409,
            "code": "BLOCKED",
            "detail": "The manual action ledger cannot be used.",
            "side_effect": False,
            "accepted_as_success": False,
        }


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


def _events(root: Path, principal: Principal, tasks: set[str]):
    if not (root / "events.sqlite3").exists():
        return [], False
    found = []
    cursor = 0
    bad = 0
    truncated = False
    for _ in range(_PAGES):
        page = query_applied(
            root,
            repo_id=principal.repo_id,
            app_id=principal.app_id,
            task_ids=tasks,
            stream_id=principal.stream_id,
            after_rowid=cursor,
        )
        found.extend(page["items"])
        bad += page["bad"]
        if page["truncated"] and page["next_rowid"] is not None:
            cursor = page["next_rowid"]
            truncated = True
            continue
        truncated = False
        break
    else:
        truncated = True
    return found, bool(bad or truncated)


def _schedule(root: Path, principal: Principal, tasks: set[str]):
    path = root / "scheduler" / "scheduler.sqlite3"
    empty = {"tasks": [], "chains": [], "rounds": [], "runtime_evidence": False, "test_double": False}
    if not path.exists():
        return empty, "missing"
    from app.control_scheduler.errors import SchedulerError
    from app.control_scheduler.scheduler import Scheduler

    try:
        scheduler = Scheduler(root / "scheduler", _Closed(), _Closed(), repo_id=principal.repo_id)
        page = scheduler.query_board(app_id=principal.app_id, task_ids=tasks, limit=50)
        chains = list(page["chains"])
        cursor = page.get("next_cursor")
        truncated = bool(page["truncated"])
        for _ in range(_PAGES - 1):
            if not cursor:
                truncated = False
                break
            more = scheduler.query_board(app_id=principal.app_id, task_ids=tasks, limit=50, after=cursor)
            chains.extend(more["chains"])
            cursor = more.get("next_cursor")
            truncated = bool(more["truncated"])
        page["chains"] = chains
        page.pop("signers", None)
        flag = "partial" if truncated else "ok"
        return page, flag
    except (OSError, ValueError, sqlite3.Error, SchedulerError):
        return empty, "blocked"


class _Closed:
    def execute(self, operation, body, route_class="isolated"):
        del operation, body, route_class
        raise RuntimeError("closed")


def _diagnostics(root: Path, principal: Principal, tasks: set[str]):
    directory = root / "diagnostics"
    if not directory.exists():
        return {}, "missing"
    from app.control_diagnostics import DiagnosticService, ServerClock

    service = None
    try:
        service = DiagnosticService.open(
            directory,
            clock=ServerClock(datetime.now(timezone.utc)),
            writable=False,
        )
        grouped = {}
        token = None
        truncated = False
        partial = False
        for _ in range(_PAGES):
            page = service.export_page(
                app_id=principal.app_id,
                repo_id=principal.repo_id,
                task_ids=tasks,
                page_size=50,
                page_token=token,
            )
            if page.get("http") != 200:
                return {}, "blocked"
            if page.get("gap") or page.get("unscoped") or page.get("corrupt_lines"):
                partial = True
            for record in page.get("items") or []:
                if record.get("severity") not in {"error", "warning"}:
                    continue
                task_id = record.get("task_id") or record.get("subject_id")
                if task_id not in tasks:
                    continue
                text = _failure_text(record)
                grouped.setdefault(task_id, []).append({
                    "text": text,
                    "display": text,
                    "evidence": "recorded",
                    "authoritative": False,
                    "source": "w06",
                    "at": record.get("received_at") if isinstance(record.get("received_at"), str) else None,
                    "task_id": task_id,
                })
            token = page.get("next_page_token")
            if not token:
                truncated = False
                break
            truncated = True
        else:
            truncated = True
        flag = "partial" if partial or truncated else "ok"
        return grouped, flag
    except (OSError, ValueError, sqlite3.Error):
        return {}, "blocked"
    finally:
        journal = getattr(service, "journal", None)
        if journal is not None and journal.root_fd is not None:
            os.close(journal.root_fd)
            journal.root_fd = None


def _failure_text(record: dict) -> str:
    body = record.get("body") if isinstance(record.get("body"), dict) else {}
    text = body.get("failure_reason") or body.get("summary") or record.get("diagnostic_class") or "诊断记录"
    if not isinstance(text, str):
        text = "诊断记录"
    try:
        cleaned = redact_value(text)
    except ValueError:
        return "[REDACTED]"
    if not isinstance(cleaned, str):
        return "[REDACTED]"
    return cleaned


def _delivery(root: Path, principal: Principal, tasks: set[str]):
    if not (root / "delivery").exists():
        return {}, "missing"
    try:
        found = {}
        cursor = None
        bad = 0
        unscoped = 0
        truncated = False
        for _ in range(_PAGES):
            page = query_tasks(
                root,
                repo_id=principal.repo_id,
                app_id=principal.app_id,
                task_ids=tasks,
                after=cursor,
            )
            found.update(page["items"])
            bad += page["bad"]
            unscoped += page["unscoped"]
            if page["truncated"] and page["next_cursor"]:
                cursor = page["next_cursor"]
                truncated = True
                continue
            truncated = False
            break
        else:
            truncated = True
        flag = "partial" if bad or unscoped or truncated else "ok"
        return found, flag
    except DeliveryReadError:
        return {}, "blocked"


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

