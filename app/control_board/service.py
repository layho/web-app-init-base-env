"""Unified task board. Reads are read-only. Manual actions only forward.

A port response is displayed and is not copied onto the projection. Test
doubles stay marked as doubles. Production flags cannot be turned on here.
"""

from __future__ import annotations

import hashlib
import json
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
_MAX_PAGES = 8
_MAX_EVENTS = 500
_SECRET_FIELDS = frozenset({
    "lease_token",
    "proof",
    "proof_ref",
    "signature",
    "nonce",
    "token",
    "secret",
    "password",
    "authorization",
    "session_id",
    "credential",
})


class Board:
    def __init__(self, *, auth, events, schedule, delivery, diagnostics):
        self.auth = auth
        self.events = events
        self.schedule = schedule
        self.delivery = delivery
        self.diagnostics = diagnostics
        self._lock = threading.Lock()
        self._scopes = {}
        self._shells = {}
        self._unscoped = {}
        self._digests = {}
        self._replays = {}
        self._revision = 0
        self._schedules = {}
        self._scope_keys = {}

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
            scope_key = self._scope_keys[(principal["id"], app_id)]
            objects = self._scope_objects(scope_key)
            snapshot = self.schedule.snapshot(principal, app_id=app_id, version=version)
            if not isinstance(snapshot, dict) or snapshot.get("side_effect") not in (False, None):
                return _stamp({"http": 409, "code": "SIDE_EFFECT_UNKNOWN", "side_effect": False, "retained": False, "complete": False})
            schedule_complete = _feed_complete(snapshot, kind="schedule")
            self._schedules[scope_key] = _clip_schedule(snapshot, app_id, objects)
            diagnostic = self._poll_diagnostics(principal, app_id)
            if diagnostic.get("error"):
                return diagnostic["body"]
            diagnostics_complete = diagnostic["complete"]
            request = _read_request(principal, app_id, objects)
            catalog = self.events.catalog(request=request, app_id=app_id)
            if not isinstance(catalog, dict) or catalog.get("side_effect") not in (False, None):
                return _stamp({"http": 409, "code": "SIDE_EFFECT_UNKNOWN", "side_effect": False, "retained": False, "complete": False})
            events_complete = _feed_complete(catalog, kind="catalog")
            rejected = []
            incoming = []
            conflicts = []
            if events_complete:
                for stream in catalog.get("streams") or []:
                    declared = stream.get("app_id")
                    if isinstance(declared, str) and declared != app_id:
                        events_complete = False
                        continue
                    page_request = dict(request)
                    page_request["stream_id"] = stream.get("stream_id")
                    page_request["stream_epoch"] = stream.get("stream_epoch", 1)
                    read = self._read_stream(page_request, stream)
                    if read.get("error"):
                        return read["body"]
                    if not read["complete"]:
                        events_complete = False
                    incoming.extend(read["items"])
            else:
                events_complete = False
            accepted = []
            for item in incoming:
                relation = _relation(item, app_id, objects)
                if relation == "foreign":
                    continue
                if relation == "unscoped":
                    event_id = item.get("event_id")
                    if isinstance(event_id, str) and event_id:
                        self._unscoped.setdefault(scope_key, set()).add(event_id)
                    continue
                reason = reject_reason(item)
                if reason:
                    rejected.append({"event_id": item.get("event_id"), "reason": reason})
                    task_id = item.get("task_id")
                    if isinstance(task_id, str) and task_id:
                        self._shells.setdefault(scope_key, set()).add(task_id)
                    continue
                accepted.append(dict(item))
            conflicts.extend(self._retain(scope_key, accepted))
            if conflicts or rejected or self._unscoped.get(scope_key):
                events_complete = False
            for task_id in self._task_ids(scope_key, app_id):
                viewed = self.delivery.project(
                    principal, app_id=app_id, task_id=task_id, scope={"app_id": app_id, "task_id": task_id},
                )
                if not isinstance(viewed, dict) or viewed.get("side_effect") not in (False, None):
                    return _stamp({"http": 409, "code": "SIDE_EFFECT_UNKNOWN", "side_effect": False, "adopted": False, "complete": False})
            projected = self._project(principal, app_id)
            complete = bool(schedule_complete and diagnostics_complete and events_complete and not projected["gap"] and not conflicts)
            return _stamp({
                "http": 200,
                "side_effect": False,
                "authoritative_state_changed": False,
                "accepted_as_success": False,
                "gap": projected["gap"] or bool(conflicts),
                "held": projected["held"],
                "rejected": rejected,
                "conflicts": conflicts,
                "unscoped_event_count": len(self._unscoped.get(scope_key, ())),
                "unscoped_payloads_hidden": True,
                "deleted_event_ids": False,
                "production_healthy": False,
                "retained": True,
                "complete": complete,
                "schedule_complete": schedule_complete,
                "diagnostics_complete": diagnostics_complete,
                "events_complete": events_complete,
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
            projected = self._project(principal, app_id)
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
            projected = self._project(principal, app_id)
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
            projected = self._project(principal, app_id)
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
            server = self._server_principal(principal)
            if server.get("error"):
                return self._action_result({
                    "http": server.get("http") or 403,
                    "code": server.get("code") or "NOT_AUTHORIZED",
                    "side_effect": False,
                    "port_response": _redact_port({}),
                }, revision)
            role = server["role"]
            if role not in spec["roles"]:
                code = "NOT_AN_EXECUTOR" if spec["executor_action"] else "POLICY_DENIED"
                return self._action_result({
                    "http": 403,
                    "code": code,
                    "side_effect": False,
                    "port_response": _redact_port({}),
                }, revision)
            actor = server["principal"]
            decision = self.auth.evaluate(actor, app_id=app_id, action=spec["evaluate"], task_id=task_id, candidate_id=chain_id)
            if not isinstance(decision, dict) or decision.get("side_effect") not in (False, None):
                return self._action_result({"http": 409, "code": "SIDE_EFFECT_UNKNOWN", "side_effect": False, "port_response": _redact_port({})}, revision)
            if decision.get("blocked") or decision.get("code") == "INTERFACE_PENDING":
                return self._action_result({"http": 409, "code": "INTERFACE_PENDING", "side_effect": False, "port_response": _redact_port(decision)}, revision)
            if not decision.get("allowed"):
                return self._action_result({
                    "http": decision.get("http") or 403,
                    "code": decision.get("code") or "POLICY_DENIED",
                    "side_effect": False,
                    "port_response": _redact_port(decision),
                }, revision)
            bound = _bind_submission(
                spec, app_id, action, task_id, chain_id, evidence_ref, successor_id, expected_revision, scope, idempotency_key,
            )
            if bound.get("error"):
                return self._action_result(bound["body"], revision)
            replay_key = (server["id"], app_id, action, idempotency_key)
            stored = self._replays.get(replay_key)
            if stored is not None:
                if stored.get("digest") != bound["digest"]:
                    return self._action_result({
                        "http": 409,
                        "code": "IDEMPOTENCY_PAYLOAD_MISMATCH",
                        "side_effect": False,
                        "port_response": _redact_port({}),
                    }, revision)
                if stored.get("unknown"):
                    reconciled = self._reconcile(spec, actor, bound)
                    redacted = _redact_port(reconciled)
                    result = self._action_result({
                        "http": redacted.get("http") or 409,
                        "code": redacted.get("code") or "SIDE_EFFECT_UNKNOWN",
                        "side_effect": False,
                        "port_response": redacted,
                        "replayed": True,
                        "reconciled": True,
                        "forwarded": False,
                    }, revision)
                    stored["result"] = dict(result)
                    stored["unknown"] = _unknown_result(redacted)
                    return result
                replayed = dict(stored["result"])
                replayed["replayed"] = True
                replayed["reconciled"] = False
                replayed["forwarded"] = False
                replayed["projection_revision_before"] = revision
                replayed["projection_revision_after"] = self._revision
                replayed["accepted_as_success"] = False
                replayed["port_response"] = _redact_port(replayed.get("port_response"))
                return replayed
            port_response = self._forward(
                spec, actor, app_id, action, task_id, chain_id, evidence_ref, successor_id, idempotency_key, expected_revision, bound["scope"],
            )
            redacted = _redact_port(port_response)
            result = self._action_result({
                "http": redacted.get("http") or 200,
                "code": redacted.get("code"),
                "side_effect": False,
                "port_response": redacted,
                "forwarded": True,
                "reconciled": False,
                "forged_success_rejected": _forged_success(port_response),
            }, revision)
            self._replays[replay_key] = {
                "digest": bound["digest"],
                "result": dict(result),
                "unknown": _unknown_result(redacted),
            }
            return result

    def render(self, principal, app_id):
        page = self.list_tasks(principal, app_id, page_size=50)
        return render_view(page, self.flags())

    def render_text(self, principal, app_id):
        page = self.list_tasks(principal, app_id, page_size=50)
        return render_text(page)

    def retained_event_ids(self, app_id):
        with self._lock:
            found = []
            for key, events in self._scopes.items():
                if key[1] != app_id:
                    continue
                found.extend(events)
            return list(dict.fromkeys(found))

    def _server_principal(self, principal):
        authenticate = getattr(self.auth, "authenticate", None)
        if not callable(authenticate):
            return {"error": True, "http": 403, "code": "NOT_AUTHORIZED"}
        record = authenticate(principal)
        if not isinstance(record, dict) or record.get("side_effect") not in (False, None):
            return {"error": True, "http": 409, "code": "SIDE_EFFECT_UNKNOWN"}
        if record.get("blocked") or record.get("code") == "INTERFACE_PENDING":
            return {"error": True, "http": 409, "code": "INTERFACE_PENDING"}
        if not record.get("ok") or not record.get("role"):
            return {"error": True, "http": record.get("http") or 403, "code": record.get("code") or "NOT_AUTHORIZED"}
        actor = dict(principal)
        actor["role"] = record["role"]
        actor["id"] = record.get("principal_id") or principal["id"]
        return {"error": False, "id": actor["id"], "role": record["role"], "principal": actor, "http": 200}

    def _remember_scope(self, principal, app_id, decision):
        objects = decision.get("objects") if isinstance(decision, dict) else None
        if objects is None:
            marker = ("*",)
        else:
            marker = tuple(sorted(str(item) for item in objects))
        scope_key = (principal["id"], app_id, marker)
        self._scope_keys[(principal["id"], app_id)] = scope_key
        return scope_key

    def _scope_objects(self, scope_key):
        if scope_key[2] == ("*",):
            return None
        return set(scope_key[2])

    def _poll_diagnostics(self, principal, app_id):
        pages = 0
        after = 0
        seen = set()
        complete = True
        while pages < _MAX_PAGES:
            pages += 1
            page = self.diagnostics.poll(principal, app_id=app_id, after_seq=after)
            if not isinstance(page, dict) or page.get("side_effect") not in (False, None):
                return {"error": True, "body": _stamp({"http": 409, "code": "SIDE_EFFECT_UNKNOWN", "side_effect": False, "retained": False, "complete": False})}
            if not _feed_complete(page, kind="diagnostic"):
                complete = False
            if page.get("truncated") is not True:
                break
            nxt = page.get("next_after_seq")
            if type(nxt) is not int or isinstance(nxt, bool) or nxt <= after or nxt in seen:
                complete = False
                break
            seen.add(after)
            after = nxt
        else:
            complete = False
        return {"error": False, "complete": complete}

    def _read_stream(self, request, stream):
        stream_id = stream.get("stream_id")
        epoch = stream.get("stream_epoch", 1)
        after = 0
        seen = set()
        items = []
        complete = True
        pages = 0
        while pages < _MAX_PAGES and len(items) <= _MAX_EVENTS:
            pages += 1
            page = self.events.query(
                request=request,
                stream_id=stream_id,
                stream_epoch=epoch,
                after_seq=after,
                task_id=request.get("authorization", {}).get("task_id"),
                limit=50,
            )
            if not isinstance(page, dict) or page.get("side_effect") not in (False, None):
                return {"error": True, "body": _stamp({"http": 409, "code": "SIDE_EFFECT_UNKNOWN", "side_effect": False, "retained": False, "complete": False})}
            batch = page.get("items") or []
            if "truncated" not in page:
                items.extend(batch)
                complete = False
                break
            items.extend(batch)
            if page.get("gap") is True:
                complete = False
            if page.get("truncated") is not True:
                break
            nxt = page.get("next_after_seq")
            if type(nxt) is not int or isinstance(nxt, bool) or nxt <= after or nxt in seen:
                complete = False
                break
            seen.add(after)
            after = nxt
        else:
            complete = False
        return {"error": False, "items": items, "complete": complete}

    def _retain(self, scope_key, accepted):
        conflicts = []
        store = self._scopes.setdefault(scope_key, {})
        digests = self._digests.setdefault(scope_key, {})
        occupied = {}
        for item in accepted:
            event_id = item.get("event_id")
            digest = _event_digest(item)
            previous = digests.get(event_id)
            if previous == "conflict" or previous not in (None, digest):
                conflicts.append({"event_id": event_id, "reason": "digest_mismatch"})
                store.pop(event_id, None)
                digests[event_id] = "conflict"
                task_id = item.get("task_id")
                if isinstance(task_id, str) and task_id:
                    self._shells.setdefault(scope_key, set()).add(task_id)
                continue
            if previous is None and event_id not in store:
                store[event_id] = item
                digests[event_id] = digest
                self._revision += 1
            slot = (item.get("stream_id"), item.get("stream_epoch", 1), item.get("producer_seq"))
            prior = occupied.get(slot)
            if prior is not None and prior != event_id:
                conflicts.append({"event_id": prior, "reason": "same_seq", "stream_id": slot[0], "producer_seq": slot[2]})
                conflicts.append({"event_id": event_id, "reason": "same_seq", "stream_id": slot[0], "producer_seq": slot[2]})
            else:
                occupied[slot] = event_id
        return conflicts

    def _reconcile(self, spec, principal, binding):
        payload = binding["binding"]
        port = {"w02": self.auth, "w04": self.schedule, "w05": self.delivery}.get(spec["port"])
        reconcile = getattr(port, "reconcile", None)
        if not callable(reconcile):
            return {
                "http": 409,
                "code": "SIDE_EFFECT_UNKNOWN",
                "state": "UNKNOWN",
                "forwarded": False,
                "reconciled": False,
                "side_effect": False,
            }
        return reconcile(
            principal,
            action=payload["action"],
            subject_id=payload["chain_id"] or payload["task_id"],
            idempotency_key=binding["idempotency_key"],
            expected_revision=payload["expected_revision"],
            task_id=payload["task_id"],
            chain_id=payload["chain_id"],
        )

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
        server = self._server_principal(principal)
        enabled = False
        code = None
        if server.get("error"):
            code = server.get("code") or "NOT_AUTHORIZED"
            role = None
        else:
            role = server["role"]
        if role not in spec["roles"]:
            if code is None:
                code = "NOT_AN_EXECUTOR" if spec["executor_action"] else "POLICY_DENIED"
        else:
            decision = self.auth.evaluate(server["principal"], app_id=app_id, action=spec["evaluate"], task_id=task_id)
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

    def _project(self, principal, app_id):
        scope_key = self._scope_keys.get((principal["id"], app_id))
        events = list(self._scopes.get(scope_key, {}).values())
        schedule = self._schedules.get(scope_key) or {"tasks": [], "chains": [], "rounds": [], "runtime_evidence": False, "test_double": False}
        shells = [(app_id, task_id) for task_id in self._shells.get(scope_key, ())]
        return project(events, schedule, shells)

    def _task_ids(self, scope_key, app_id):
        ids = {event.get("task_id") for event in self._scopes.get(scope_key, {}).values() if isinstance(event.get("task_id"), str)}
        schedule = self._schedules.get(scope_key) or {}
        for row in schedule.get("tasks") or []:
            if row.get("app_id") in (None, app_id) and isinstance(row.get("task_id"), str):
                ids.add(row["task_id"])
        ids.update(self._shells.get(scope_key, ()))
        return sorted(ids)

    def _all_tasks(self, principal, app_id, include_archived):
        page = self.list_tasks(principal, app_id, page_size=50, include_archived=include_archived)
        if page.get("http") != 200:
            return []
        with self._lock:
            projected = self._project(principal, app_id)
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
        server = self._server_principal(principal)
        if server.get("error"):
            return _stamp({
                "http": server.get("http") or 403,
                "code": server.get("code") or "NOT_AUTHORIZED",
                "side_effect": False,
                "evidence_state": "missing",
                "production_healthy": False,
                "items": [],
            })
        principal = server["principal"]
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
        self._remember_scope(principal, app_id, decision)
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


def _read_request(principal, app_id, objects):
    return {
        "schema_version": 2,
        "side_effect": False,
        "app_id": app_id,
        "operation": "event.read",
        "actor": {
            "id": principal.get("id"),
            "app_id": app_id,
            "kind": principal.get("kind"),
        },
        "authorization": {
            "action": "board.read",
            "app_id": app_id,
            "principal_id": principal.get("id"),
            "allowed": True,
            "objects": None if objects is None else sorted(objects),
        },
    }


def _feed_complete(body, *, kind):
    if not isinstance(body, dict) or body.get("side_effect") not in (False, None):
        return False
    if body.get("blocked") or body.get("code") == "INTERFACE_PENDING":
        return False
    if body.get("gap") is True or body.get("truncated") is True:
        return False
    if kind == "schedule":
        return isinstance(body.get("tasks"), list) and isinstance(body.get("chains"), list)
    if kind == "diagnostic":
        return isinstance(body.get("items"), list)
    if kind == "catalog":
        return isinstance(body.get("streams"), list)
    return False


def _clip_schedule(snapshot, app_id, objects):
    if snapshot.get("blocked") or snapshot.get("code") == "INTERFACE_PENDING":
        return {
            "tasks": [],
            "chains": [],
            "rounds": [],
            "runtime_evidence": False,
            "test_double": False,
            "blocked": True,
        }

    def keep(row):
        if not isinstance(row, dict):
            return False
        if row.get("app_id") not in (None, app_id):
            return False
        if objects is not None and row.get("task_id") not in objects:
            return False
        return True

    return {
        "tasks": [row for row in (snapshot.get("tasks") or []) if keep(row)],
        "chains": [row for row in (snapshot.get("chains") or []) if keep(row)],
        "rounds": [row for row in (snapshot.get("rounds") or []) if keep(row)],
        "runtime_evidence": snapshot.get("runtime_evidence") is True,
        "test_double": snapshot.get("test_double") is True,
        "blocked": False,
    }


def _relation(item, app_id, objects):
    if not isinstance(item, dict):
        return "foreign"
    app = item.get("app_id")
    if not isinstance(app, str) or not app:
        return "unscoped"
    if app != app_id:
        return "foreign"
    task_id = item.get("task_id")
    if objects is not None and task_id not in objects:
        return "foreign"
    return "local"


def _event_digest(event):
    encoded = json.dumps(event, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _redact_port(body):
    if not isinstance(body, dict):
        cleaned = {}
    else:
        cleaned = {}
        for key, value in body.items():
            if key in _SECRET_FIELDS or str(key).startswith("_"):
                continue
            if isinstance(value, dict):
                cleaned[key] = _redact_port(value)
            elif isinstance(value, list):
                cleaned[key] = [_redact_port(item) if isinstance(item, dict) else item for item in value]
            else:
                cleaned[key] = value
    cleaned["side_effect"] = False
    cleaned["github_proof"] = False
    cleaned["macci_proof"] = False
    cleaned["real_merge"] = False
    cleaned["real_deploy"] = False
    cleaned["accepted_as_success"] = False
    return cleaned


def _unknown_result(body):
    if not isinstance(body, dict):
        return True
    if body.get("state") in {"UNKNOWN", "DELIVERY_UNKNOWN"}:
        return True
    if body.get("code") in {"DELIVERY_UNKNOWN", "SIDE_EFFECT_UNKNOWN"}:
        return True
    if body.get("classification") == "unknown":
        return True
    return False


def _forged_success(body):
    if not isinstance(body, dict):
        return False
    if body.get("state") in {"SUCCEEDED", "COMMIT", "PUBLISHED", "PASSED"}:
        return True
    if body.get("performed") is True or body.get("ok") is True or body.get("github_proof") is True:
        return True
    return False


def _bind_submission(spec, app_id, action, task_id, chain_id, evidence_ref, successor_id, expected_revision, scope, idempotency_key):
    empty = {"side_effect": False, "port_response": _redact_port({})}
    if not isinstance(task_id, str) or not task_id or not isinstance(idempotency_key, str) or not idempotency_key:
        return {"error": True, "body": {"http": 422, "code": "SCHEMA_UNSUPPORTED", **empty}}
    if isinstance(expected_revision, bool) or type(expected_revision) is not int:
        return {"error": True, "body": {"http": 422, "code": "SCHEMA_UNSUPPORTED", **empty}}
    if spec["evidence"] and (not isinstance(evidence_ref, str) or not evidence_ref):
        return {"error": True, "body": {"http": 409, "code": "EVIDENCE_MISSING", **empty}}
    if spec["chain"] and (not isinstance(chain_id, str) or not chain_id):
        return {"error": True, "body": {"http": 409, "code": "EVIDENCE_MISSING", **empty}}
    bound_scope = scope if isinstance(scope, dict) else {"app_id": app_id, "task_id": task_id, "chain_id": chain_id}
    payload = {
        "action": action,
        "task_id": task_id,
        "chain_id": chain_id,
        "evidence_ref": evidence_ref,
        "successor_id": successor_id or "",
        "expected_revision": expected_revision,
        "scope": bound_scope,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return {
        "error": False,
        "digest": digest,
        "binding": payload,
        "scope": bound_scope,
        "idempotency_key": idempotency_key,
    }


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
