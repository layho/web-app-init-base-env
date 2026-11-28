"""Unified task board. Reads are read-only. Manual actions only forward.

A port response is displayed and is not copied onto the projection. Test
doubles stay marked as doubles. Production flags cannot be turned on here.
"""

from __future__ import annotations

import threading

from .flags import (
    ACCEPTANCE_ENABLED,
    AUTHORITATIVE_MUTATION,
    GITHUB_EXECUTION,
    JOINT_INTEGRATION_READY,
    MACCI_PROOF,
    MODULE_NAME,
    MOUNTED_IN_APP_MAIN,
    PRODUCTION_DB_ACCESS,
    PRODUCTION_ENABLEMENT,
    PRODUCTION_VERIFICATION_ENABLED,
    REAL_DEPLOY,
    REAL_MERGE,
    ROUTE_MODULE,
    SUBSCRIBE_PUSH,
)
from .projection import project, reject_reason
from .view import render_text, render_view

_ROLES = frozenset({"agent", "session", "host", "hostops", "reader", "publisher", "macci"})
_ACTIONS = (
    {"id": "nudge", "label": "催促", "roles": frozenset({"agent", "session"}), "evaluate": "task.request_action", "port": "w02", "evidence": False, "chain": False, "executor_action": False},
    {"id": "execute_now", "label": "立即执行（未接通执行器）", "roles": frozenset({"agent"}), "evaluate": "task.request_action", "port": "w02", "evidence": False, "chain": False, "executor_action": True},
    {"id": "retry_phase", "label": "重试阶段", "roles": frozenset({"agent"}), "evaluate": "schedule.intervene", "port": "w04", "evidence": True, "chain": False, "executor_action": False},
    {"id": "unblock", "label": "解除阻塞", "roles": frozenset({"agent"}), "evaluate": "schedule.intervene", "port": "w04", "evidence": True, "chain": False, "executor_action": False},
    {"id": "authorize_repair", "label": "授权修复", "roles": frozenset({"agent"}), "evaluate": "repair.authorize", "port": "w04", "evidence": True, "chain": True, "executor_action": False},
    {"id": "merge", "label": "请求合并", "roles": frozenset({"session", "host"}), "evaluate": "preview.merge", "port": "w05", "evidence": False, "chain": True, "executor_action": False},
    {"id": "deploy", "label": "请求部署", "roles": frozenset({"host"}), "evaluate": "deploy.plan", "port": "w05", "evidence": False, "chain": True, "executor_action": False},
    {"id": "request_publish", "label": "请求发布", "roles": frozenset(), "evaluate": "release.publish", "port": "w05", "evidence": False, "chain": True, "executor_action": False},
)
_BY_ACTION = {item["id"]: item for item in _ACTIONS}


class Board:
    def __init__(self, *, auth, events, schedule, delivery, diagnostics):
        self.auth = auth
        self.events = events
        self.schedule = schedule
        self.delivery = delivery
        self.diagnostics = diagnostics
        self._lock = threading.Lock()
        self._apps = {}
        self._shells = {}
        self._unscoped = set()
        self._replays = {}
        self._revision = 0
        self._schedules = {}

    def flags(self):
        return {
            "module": MODULE_NAME,
            "route_module": ROUTE_MODULE,
            "mounted_in_app_main": MOUNTED_IN_APP_MAIN,
            "production_enablement": PRODUCTION_ENABLEMENT,
            "production_verification_enabled": PRODUCTION_VERIFICATION_ENABLED,
            "production_db_access": PRODUCTION_DB_ACCESS,
            "real_merge": REAL_MERGE,
            "real_deploy": REAL_DEPLOY,
            "github_proof": GITHUB_EXECUTION,
            "macci_proof": MACCI_PROOF,
            "joint_integration_ready": JOINT_INTEGRATION_READY,
            "acceptance_enabled": ACCEPTANCE_ENABLED,
            "authoritative_mutation": AUTHORITATIVE_MUTATION,
            "subscribe_push": SUBSCRIBE_PUSH,
            "side_effect": False,
            "authoritative_store": False,
        }

    def enable_production(self):
        return _stamp({
            "http": 403,
            "code": "POLICY_DENIED",
            "side_effect": False,
            "reason": "Production enablement stays false in this package.",
        })

    def read_production_database(self):
        return _stamp({
            "http": 403,
            "code": "PRODUCTION_DB_FORBIDDEN",
            "side_effect": False,
            "evidence_state": "missing",
            "rows": None,
            "reason": "This package does not open a production database.",
        })

    def refresh(self, principal, app_id, *, version=None):
        with self._lock:
            denied = self._gate(principal, app_id, "board.read")
            if denied:
                return denied
            snapshot = self.schedule.snapshot(principal, app_id=app_id, version=version)
            if snapshot.get("side_effect") not in (False, None):
                return _stamp({"http": 409, "code": "SIDE_EFFECT_UNKNOWN", "side_effect": False, "retained": False})
            if snapshot.get("blocked") or snapshot.get("code") == "INTERFACE_PENDING":
                self._schedules[app_id] = {"tasks": [], "chains": [], "rounds": [], "runtime_evidence": False, "test_double": False, "blocked": True}
            else:
                self._schedules[app_id] = snapshot
            diagnostic = self.diagnostics.poll(principal, app_id=app_id, after_seq=0)
            if diagnostic.get("side_effect") not in (False, None):
                return _stamp({"http": 409, "code": "SIDE_EFFECT_UNKNOWN", "side_effect": False, "retained": False})
            catalog = self.events.catalog(request={"side_effect": False}, app_id=app_id)
            if catalog.get("side_effect") not in (False, None):
                return _stamp({"http": 409, "code": "SIDE_EFFECT_UNKNOWN", "side_effect": False, "retained": False})
            rejected = []
            incoming = []
            if not catalog.get("blocked") and catalog.get("code") != "INTERFACE_PENDING":
                for stream in catalog.get("streams") or []:
                    after = 0
                    while True:
                        page = self.events.query(
                            request={"side_effect": False},
                            stream_id=stream["stream_id"],
                            stream_epoch=stream.get("stream_epoch", 1),
                            after_seq=after,
                            limit=50,
                        )
                        if page.get("side_effect") not in (False, None):
                            return _stamp({"http": 409, "code": "SIDE_EFFECT_UNKNOWN", "side_effect": False, "retained": False})
                        for item in page.get("items") or []:
                            reason = reject_reason(item)
                            if reason:
                                rejected.append({"event_id": item.get("event_id"), "reason": reason})
                                app = item.get("app_id")
                                task_id = item.get("task_id")
                                if isinstance(app, str) and app and isinstance(task_id, str) and task_id:
                                    self._shells.setdefault(app, set()).add(task_id)
                                continue
                            incoming.append(dict(item))
                        if not page.get("truncated"):
                            break
                        after = page.get("next_after_seq", after)
                        if after is None:
                            break
            for item in incoming:
                app = item.get("app_id")
                if not isinstance(app, str) or not app:
                    event_id = item.get("event_id")
                    if isinstance(event_id, str):
                        self._unscoped.add(event_id)
                    continue
                store = self._apps.setdefault(app, {})
                event_id = item["event_id"]
                if event_id not in store:
                    store[event_id] = item
                    self._revision += 1
            for task_id in self._task_ids(app_id):
                viewed = self.delivery.project(
                    principal, app_id=app_id, task_id=task_id, scope={"app_id": app_id, "task_id": task_id},
                )
                if viewed.get("side_effect") not in (False, None):
                    return _stamp({"http": 409, "code": "SIDE_EFFECT_UNKNOWN", "side_effect": False, "adopted": False})
            projected = self._project(app_id)
            return _stamp({
                "http": 200,
                "side_effect": False,
                "authoritative_state_changed": False,
                "accepted_as_success": False,
                "gap": projected["gap"],
                "held": projected["held"],
                "rejected": rejected,
                "unscoped_event_count": len(self._unscoped),
                "unscoped_payloads_hidden": True,
                "deleted_event_ids": False,
                "production_healthy": False,
                "retained": True,
            })

    def list_tasks(self, principal, app_id, *, page_size=20, page_token=None, include_archived=False):
        with self._lock:
            denied = self._gate(principal, app_id, "board.read")
            if denied:
                denied["items"] = []
                denied["production_healthy"] = False
                denied["evidence_state"] = denied.get("evidence_state", "missing")
                return denied
            parsed = _token(page_token, app_id, "v1")
            if parsed.get("error"):
                return _stamp({"http": parsed["http"], "code": parsed["code"], "items": [], "side_effect": False, "production_healthy": False, "evidence_state": "missing"})
            size = _page_size(page_size)
            if size is None:
                return _stamp({"http": 422, "code": "SCHEMA_UNSUPPORTED", "items": [], "side_effect": False, "production_healthy": False, "evidence_state": "missing"})
            projected = self._project(app_id)
            rows = [projected["tasks"][key] for key in sorted(projected["tasks"]) if key[0] == app_id]
            rows.sort(key=lambda item: item["task_id"])
            archived = [item for item in rows if item["archived"]]
            visible = rows if include_archived else [item for item in rows if not item["archived"]]
            start = 0
            if parsed.get("last") is not None:
                ids = [item["task_id"] for item in visible]
                if parsed["last"] not in ids:
                    visible = []
                else:
                    start = ids.index(parsed["last"]) + 1
            page = visible[start:start + size]
            next_token = None
            if page and start + size < len(visible):
                next_token = f"v1:{app_id}:{page[-1]['task_id']}"
            evidence = "missing" if not rows else ("partial" if projected["gap"] or any(item["unknowns"] for item in rows) else "present")
            return _stamp({
                "http": 200,
                "side_effect": False,
                "items": page,
                "next_page_token": next_token,
                "gap": projected["gap"],
                "production_healthy": False,
                "evidence_state": evidence,
                "archived_hidden": 0 if include_archived else len(archived),
                "included_archived": bool(include_archived),
                "authoritative_state_changed": False,
                "code": None,
            })

    def list_tests(self, principal, app_id, *, page_size=20, page_token=None, include_archived=False):
        page = self.list_tasks(principal, app_id, page_size=50, include_archived=include_archived)
        if page.get("http") != 200:
            page["items"] = []
            return page
        items = []
        for task in self._all_tasks(principal, app_id, include_archived):
            items.extend(task.get("tests") or [])
        return self._slice(items, app_id, page_size, page_token, "tests")

    def list_prs(self, principal, app_id, *, task_id=None, page_size=20, page_token=None, include_archived=False):
        denied = self._public_gate(principal, app_id)
        if denied:
            denied["items"] = []
            return denied
        items = []
        for task in self._all_tasks(principal, app_id, include_archived):
            if task_id is not None and task["task_id"] != task_id:
                continue
            for pr in task.get("prs") or []:
                if pr.get("archived") and not include_archived:
                    continue
                items.append(pr)
        return self._slice(items, app_id, page_size, page_token, "prs")

    def task(self, principal, app_id, task_id):
        with self._lock:
            denied = self._gate(principal, app_id, "board.read")
            if denied:
                return denied
            projected = self._project(app_id)
            found = projected["tasks"].get((app_id, task_id))
            if found is None:
                return _stamp({"http": 404, "code": "EVIDENCE_MISSING", "evidence_state": "missing", "side_effect": False, "task_id": task_id})
            return found

    def history(self, principal, app_id, task_id, *, page_size=20, page_token=None):
        with self._lock:
            denied = self._gate(principal, app_id, "board.read")
            if denied:
                denied["items"] = []
                return denied
            size = _page_size(page_size)
            if size is None:
                return _stamp({"http": 422, "code": "SCHEMA_UNSUPPORTED", "items": [], "side_effect": False})
            parsed = _history_token(page_token, app_id, task_id)
            if parsed.get("error"):
                return _stamp({"http": parsed["http"], "code": parsed["code"], "items": [], "side_effect": False})
            projected = self._project(app_id)
            found = projected["tasks"].get((app_id, task_id))
            items = [] if found is None else list(found.get("history") or [])
            start = parsed.get("index", 0)
            page = items[start:start + size]
            next_token = None
            if page and start + size < len(items):
                next_token = f"h1:{app_id}:{task_id}:{start + size - 1}"
            return _stamp({
                "http": 200,
                "side_effect": False,
                "items": page,
                "next_page_token": next_token,
                "evidence_state": "present" if items else "missing",
                "deleted_event_ids": False,
            })

    def buttons(self, principal, app_id, task_id):
        with self._lock:
            denied = self._gate(principal, app_id, "board.read")
            if denied:
                denied["buttons"] = []
                return denied
            return _stamp({
                "http": 200,
                "side_effect": False,
                "task_id": task_id,
                "buttons": [self._button(principal, app_id, task_id, spec) for spec in _ACTIONS],
                "connected_executor": False,
            })

    def submit(self, principal, app_id, *, action, task_id, idempotency_key, expected_revision=0, chain_id=None, evidence_ref=None, successor_id="", scope=None):
        with self._lock:
            revision = self._revision
            denied = self._shape_or_app(principal, app_id)
            if denied:
                return self._action_result(denied, revision)
            if action == "hostops_execute":
                return self._action_result({"http": 403, "code": "NOT_AN_EXECUTOR", "side_effect": False}, revision)
            spec = _BY_ACTION.get(action)
            if spec is None:
                return self._action_result({"http": 422, "code": "SCHEMA_UNSUPPORTED", "side_effect": False}, revision)
            role = principal.get("role")
            if role not in spec["roles"]:
                code = "NOT_AN_EXECUTOR" if spec["executor_action"] else "POLICY_DENIED"
                return self._action_result({"http": 403, "code": code, "side_effect": False}, revision)
            if spec["evidence"] and (not isinstance(evidence_ref, str) or not evidence_ref):
                return self._action_result({"http": 409, "code": "EVIDENCE_MISSING", "side_effect": False}, revision)
            if spec["chain"] and (not isinstance(chain_id, str) or not chain_id):
                return self._action_result({"http": 409, "code": "EVIDENCE_MISSING", "side_effect": False}, revision)
            replay_key = (principal["id"], app_id, action, idempotency_key)
            stored = self._replays.get(replay_key)
            if stored is not None:
                replayed = dict(stored)
                replayed["replayed"] = True
                replayed["projection_revision_before"] = revision
                replayed["projection_revision_after"] = self._revision
                return replayed
            decision = self.auth.evaluate(principal, app_id=app_id, action=spec["evaluate"], task_id=task_id, candidate_id=chain_id)
            if decision.get("side_effect") not in (False, None):
                return self._action_result({"http": 409, "code": "SIDE_EFFECT_UNKNOWN", "side_effect": False}, revision)
            if not decision.get("allowed"):
                result = self._action_result({
                    "http": decision.get("http") or 403,
                    "code": decision.get("code") or "POLICY_DENIED",
                    "side_effect": False,
                    "port_response": decision,
                }, revision)
                self._replays[replay_key] = dict(result)
                return result
            port_response = self._forward(spec, principal, app_id, action, task_id, chain_id, evidence_ref, successor_id, idempotency_key, expected_revision, scope)
            result = self._action_result({
                "http": port_response.get("http") or 200,
                "code": port_response.get("code"),
                "side_effect": False,
                "port_response": port_response,
            }, revision)
            self._replays[replay_key] = dict(result)
            return result

    def render(self, principal, app_id):
        page = self.list_tasks(principal, app_id, page_size=50)
        return render_view(page, self.flags())

    def render_text(self, principal, app_id):
        page = self.list_tasks(principal, app_id, page_size=50)
        return render_text(page)

    def retained_event_ids(self, app_id):
        with self._lock:
            return list(self._apps.get(app_id, {}))

    def _forward(self, spec, principal, app_id, action, task_id, chain_id, evidence_ref, successor_id, idempotency_key, expected_revision, scope):
        bound_scope = scope if isinstance(scope, dict) else {"app_id": app_id, "task_id": task_id}
        if spec["port"] == "w02":
            return self.auth.request_action(
                principal, app_id=app_id, task_id=task_id, action=action,
                idempotency_key=idempotency_key, expected_revision=expected_revision,
            )
        if spec["port"] == "w04":
            return self.schedule.intervene(
                principal, action=action, subject_id=chain_id or task_id,
                evidence_ref=evidence_ref or "", successor_id=successor_id or "", note="",
            )
        if action == "request_publish":
            return self.delivery.request_publish(
                principal, task_id=task_id, chain_id=chain_id, idempotency_key=idempotency_key,
                expected_revision=expected_revision, scope=bound_scope,
            )
        return self.delivery.coordinate(
            principal, action=action, task_id=task_id, chain_id=chain_id,
            idempotency_key=idempotency_key, expected_revision=expected_revision, scope=bound_scope,
        )

    def _button(self, principal, app_id, task_id, spec):
        role = principal.get("role")
        enabled = False
        code = None
        if role not in spec["roles"]:
            code = "NOT_AN_EXECUTOR" if spec["executor_action"] else "POLICY_DENIED"
        else:
            decision = self.auth.evaluate(principal, app_id=app_id, action=spec["evaluate"], task_id=task_id)
            if decision.get("side_effect") not in (False, None):
                code = "SIDE_EFFECT_UNKNOWN"
            elif decision.get("blocked") or decision.get("code") == "INTERFACE_PENDING":
                code = "INTERFACE_PENDING"
            elif not decision.get("allowed"):
                code = decision.get("code") or "POLICY_DENIED"
            else:
                enabled = True
        return {
            "id": spec["id"],
            "label": spec["label"],
            "enabled": enabled,
            "code": code,
            "connected_executor": False,
            "port": spec["port"],
            "calls_authority": True,
            "infers_success": False,
        }

    def _project(self, app_id):
        events = list(self._apps.get(app_id, {}).values())
        schedule = self._schedules.get(app_id) or {"tasks": [], "chains": [], "rounds": [], "runtime_evidence": False, "test_double": False}
        shells = [(app_id, task_id) for task_id in self._shells.get(app_id, ()) ]
        return project(events, schedule, shells)

    def _task_ids(self, app_id):
        ids = {event.get("task_id") for event in self._apps.get(app_id, {}).values() if isinstance(event.get("task_id"), str)}
        schedule = self._schedules.get(app_id) or {}
        for row in schedule.get("tasks") or []:
            if row.get("app_id") == app_id and isinstance(row.get("task_id"), str):
                ids.add(row["task_id"])
        return sorted(ids)

    def _all_tasks(self, principal, app_id, include_archived):
        page = self.list_tasks(principal, app_id, page_size=50, include_archived=include_archived)
        if page.get("http") != 200:
            return []
        with self._lock:
            projected = self._project(app_id)
        rows = [projected["tasks"][key] for key in sorted(projected["tasks"]) if key[0] == app_id]
        if not include_archived:
            rows = [item for item in rows if not item["archived"]]
        return rows

    def _slice(self, items, app_id, page_size, page_token, kind):
        size = _page_size(page_size)
        if size is None:
            return _stamp({"http": 422, "code": "SCHEMA_UNSUPPORTED", "items": [], "side_effect": False, "production_healthy": False})
        parsed = _offset_token(page_token, app_id, kind)
        if parsed.get("error"):
            return _stamp({"http": parsed["http"], "code": parsed["code"], "items": [], "side_effect": False, "production_healthy": False})
        start = parsed["index"]
        page = items[start:start + size]
        next_token = f"{kind}:{app_id}:{start + size}" if start + size < len(items) else None
        return _stamp({
            "http": 200,
            "side_effect": False,
            "items": page,
            "next_page_token": next_token,
            "production_healthy": False,
        })

    def _public_gate(self, principal, app_id):
        with self._lock:
            return self._gate(principal, app_id, "board.read")

    def _gate(self, principal, app_id, action):
        denied = self._shape_or_app(principal, app_id)
        if denied:
            return denied
        decision = self.auth.evaluate(principal, app_id=app_id, action=action)
        if decision.get("side_effect") not in (False, None):
            return _stamp({"http": 409, "code": "SIDE_EFFECT_UNKNOWN", "side_effect": False, "evidence_state": "unknown", "production_healthy": False})
        if decision.get("blocked") or decision.get("code") == "INTERFACE_PENDING":
            return _stamp({
                "http": 409,
                "code": "INTERFACE_PENDING",
                "side_effect": False,
                "evidence_state": "missing",
                "production_healthy": False,
                "items": [],
            })
        if not decision.get("allowed"):
            return _stamp({
                "http": decision.get("http") or 403,
                "code": decision.get("code") or "POLICY_DENIED",
                "side_effect": False,
                "evidence_state": "missing",
                "production_healthy": False,
                "items": [],
            })
        return None

    def _shape_or_app(self, principal, app_id):
        if not _shaped(principal):
            return _stamp({"http": 403, "code": "NOT_AUTHORIZED", "side_effect": False, "evidence_state": "missing", "production_healthy": False, "items": []})
        if principal.get("app_id") != app_id:
            return _stamp({"http": 403, "code": "POLICY_DENIED", "side_effect": False, "evidence_state": "missing", "production_healthy": False, "items": []})
        return None

    def _action_result(self, body, revision):
        result = _stamp({
            "accepted_as_success": False,
            "authoritative_state_changed": False,
            "connected_executor": False,
            "projection_revision_before": revision,
            "projection_revision_after": self._revision,
            "replayed": False,
            "running": False,
        })
        result.update(body)
        result["accepted_as_success"] = False
        result["authoritative_state_changed"] = False
        result["connected_executor"] = False
        result["github_proof"] = False
        result["macci_proof"] = False
        result["test_double_promoted"] = False
        result["side_effect"] = False
        return result


def _shaped(principal):
    if not isinstance(principal, dict):
        return False
    if principal.get("kind") != "test":
        return False
    proof = principal.get("proof_ref")
    if not isinstance(proof, str) or not proof.startswith("test-proof://"):
        return False
    if not isinstance(principal.get("id"), str) or not principal["id"]:
        return False
    if not isinstance(principal.get("app_id"), str) or not principal["app_id"]:
        return False
    return principal.get("role") in _ROLES


def _page_size(page_size):
    if isinstance(page_size, bool) or type(page_size) is not int or not 1 <= page_size <= 50:
        return None
    return page_size


def _token(token, app_id, prefix):
    if token is None:
        return {"last": None}
    if not isinstance(token, str):
        return {"error": True, "http": 422, "code": "SCHEMA_UNSUPPORTED"}
    parts = token.split(":", 2)
    if len(parts) != 3 or parts[0] != prefix or not parts[2]:
        return {"error": True, "http": 422, "code": "SCHEMA_UNSUPPORTED"}
    if parts[1] != app_id:
        return {"error": True, "http": 409, "code": "APP_MISMATCH"}
    return {"last": parts[2]}


def _offset_token(token, app_id, kind):
    if token is None:
        return {"index": 0}
    if not isinstance(token, str):
        return {"error": True, "http": 422, "code": "SCHEMA_UNSUPPORTED"}
    parts = token.split(":")
    if len(parts) != 3 or parts[0] != kind or parts[1] != app_id:
        return {"error": True, "http": 422, "code": "SCHEMA_UNSUPPORTED"}
    try:
        index = int(parts[2])
    except ValueError:
        return {"error": True, "http": 422, "code": "SCHEMA_UNSUPPORTED"}
    if index < 0:
        return {"error": True, "http": 422, "code": "SCHEMA_UNSUPPORTED"}
    return {"index": index}


def _history_token(token, app_id, task_id):
    if token is None:
        return {"index": 0}
    parts = token.split(":")
    if len(parts) != 4 or parts[0] != "h1" or parts[1] != app_id or parts[2] != task_id:
        return {"error": True, "http": 422, "code": "SCHEMA_UNSUPPORTED"}
    try:
        index = int(parts[3])
    except ValueError:
        return {"error": True, "http": 422, "code": "SCHEMA_UNSUPPORTED"}
    if index < 0:
        return {"error": True, "http": 422, "code": "SCHEMA_UNSUPPORTED"}
    return {"index": index + 1}


def _stamp(body):
    body.setdefault("schema_version", 2)
    body.setdefault("side_effect", False)
    body["github_proof"] = False
    body["macci_proof"] = False
    body["test_double_promoted"] = False
    body["production_enablement"] = False
    body["mounted"] = False
    body["authoritative_store"] = False
    return body
