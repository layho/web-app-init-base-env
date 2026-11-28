"""Durable manual actions.

The idempotency key is stored only as a hash. A second payload for the same
key does not run again. An unknown side effect is stored as unknown and is
not treated as success. Production merge, deploy, and publish stay closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from app.control_authz.authorize import authorize
from app.control_authz.errors import AuthzError
from app.control_authz.principal import Principal, ResourceBinding

_ACTIONS = {
    "nudge": ("run.reconcile", "run", "催促"),
    "retry_phase": ("run.reconcile", "run", "重试阶段"),
    "unblock": ("run.reconcile", "run", "解除阻塞"),
    "authorize_repair": ("run.reconcile", "run", "授权修复"),
    "merge": ("preview.merge", "merge", "请求合并"),
    "deploy": ("deploy.apply", "deploy", "请求部署"),
    "request_publish": ("release.publish", "publish", "请求发布"),
}
_CLOSED = frozenset({"merge", "deploy", "request_publish"})
_SCHEDULER = frozenset({"retry_phase", "unblock", "authorize_repair"})


def action_specs():
    return dict(_ACTIONS)


class ManualActions:
    def __init__(self, root: Path, executor=None):
        self.root = Path(root)
        self.executor = executor or execute_isolated
        self.path = self.root / "manual-actions.sqlite3"

    def describe(self, principal: Principal, task_id: str) -> list[dict]:
        found = []
        for action, (operation, scope, label) in _ACTIONS.items():
            try:
                _authorize_action(principal, task_id, operation, scope)
            except AuthzError as exc:
                found.append({
                    "id": action,
                    "label": label,
                    "task_id": task_id,
                    "enabled": False,
                    "code": exc.code,
                    "reason": exc.reason,
                    "calls_authority": True,
                    "connected_executor": False,
                })
                continue
            found.append({
                "id": action,
                "label": label,
                "task_id": task_id,
                "enabled": True,
                "code": None,
                "reason": None,
                "calls_authority": True,
                "connected_executor": False,
            })
        return found

    def submit(self, principal: Principal, body: dict) -> dict:
        action = body.get("action") if isinstance(body, dict) else None
        task_id = body.get("task_id") if isinstance(body, dict) else None
        key = body.get("idempotency_key") if isinstance(body, dict) else None
        revision = body.get("expected_revision") if isinstance(body, dict) else None
        evidence = body.get("evidence_ref") if isinstance(body, dict) else None
        if action not in _ACTIONS or not isinstance(task_id, str) or not task_id:
            return _result(422, "SCHEMA_UNSUPPORTED", "The action is not recognized.")
        if not isinstance(key, str) or not key or len(key) > 200 or "\n" in key:
            return _result(422, "SCHEMA_UNSUPPORTED", "The idempotency key is invalid.")
        if isinstance(revision, bool) or type(revision) is not int:
            return _result(422, "SCHEMA_UNSUPPORTED", "expected_revision must be an integer.")
        if evidence is not None and (not isinstance(evidence, str) or not evidence or len(evidence) > 200):
            return _result(422, "SCHEMA_UNSUPPORTED", "evidence_ref is invalid.")
        operation, scope, _label = _ACTIONS[action]
        try:
            _authorize_action(principal, task_id, operation, scope)
        except AuthzError as exc:
            return _result(exc.status, exc.code, exc.detail, reason=exc.reason)
        if task_id not in _task_ids(principal):
            return _result(403, "NOT_AUTHORIZED", "The task is outside the server binding.", reason="cross_task")
        payload = {
            "action": action,
            "task_id": task_id,
            "expected_revision": revision,
            "evidence_ref": evidence or "",
        }
        payload_hash = _digest(payload)
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        stored = self._read(principal.principal_id, key_hash)
        if stored is not None:
            if stored["payload_hash"] != payload_hash:
                return _result(
                    409,
                    "IDEMPOTENCY_PAYLOAD_MISMATCH",
                    "The idempotency key was already used with a different payload.",
                    replayed=False,
                    forwarded=False,
                )
            replay = dict(stored["result"])
            replay["replayed"] = True
            replay["forwarded"] = False
            replay["accepted_as_success"] = False
            replay["side_effect"] = False
            return replay
        try:
            produced = self.executor(self.root, principal, payload)
        except Exception:
            produced = _result(409, "SIDE_EFFECT_UNKNOWN", "The action result is unknown.", state="UNKNOWN")
        if not isinstance(produced, dict) or produced.get("side_effect") not in (False, None):
            produced = _result(409, "SIDE_EFFECT_UNKNOWN", "The action result is unknown.", state="UNKNOWN")
        produced = _result(
            int(produced.get("http") or 200),
            produced.get("code"),
            produced.get("detail") or "",
            reason=produced.get("reason"),
            state=produced.get("state"),
            forwarded=True,
        )
        produced["accepted_as_success"] = False
        self._write(principal.principal_id, key_hash, payload_hash, produced)
        return produced

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        os.chmod(self.path, 0o600)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS manual_actions (
                principal_id TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                result_json TEXT NOT NULL,
                PRIMARY KEY (principal_id, key_hash)
            )
            """
        )
        return conn

    def _read(self, principal_id: str, key_hash: str):
        if not self.path.exists():
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT payload_hash, result_json FROM manual_actions WHERE principal_id=? AND key_hash=?",
                (principal_id, key_hash),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {"payload_hash": row["payload_hash"], "result": json.loads(row["result_json"])}

    def _write(self, principal_id: str, key_hash: str, payload_hash: str, result: dict) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO manual_actions(principal_id, key_hash, payload_hash, result_json)
                VALUES(?,?,?,?)
                """,
                (principal_id, key_hash, payload_hash, json.dumps(result, sort_keys=True)),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def execute_isolated(root: Path, principal: Principal, payload: dict) -> dict:
    action = payload["action"]
    if action in _CLOSED:
        return _result(403, "PRODUCTION_CLOSED", "Production merge, deploy, and publish stay closed.", forwarded=False)
    if action == "nudge":
        return _result(409, "UNKNOWN_EXECUTOR", "No executor is connected for this action.", forwarded=False)
    if action in _SCHEDULER:
        schedule = Path(root) / "scheduler"
        database = schedule / "scheduler.sqlite3"
        if not database.is_file():
            return _result(409, "INTERFACE_PENDING", "No scheduler ledger is configured for this root.", forwarded=False)
        from app.control_scheduler.errors import SchedulerError
        from app.control_scheduler.scheduler import Scheduler

        try:
            scheduler = Scheduler(schedule, _ClosedPort(), _ClosedPort(), repo_id=principal.repo_id)
            body = scheduler.intervene(
                action=action,
                subject_id=payload["task_id"],
                evidence_ref=payload.get("evidence_ref") or "",
                caller={
                    "id": principal.principal_id,
                    "kind": principal.kind,
                    "app_id": principal.app_id,
                    "task_id": payload["task_id"],
                },
            )
        except SchedulerError as exc:
            return _result(exc.status, exc.code, exc.message, forwarded=True)
        except ValueError:
            return _result(409, "INTERFACE_PENDING", "The scheduler ledger is not a private directory.", forwarded=False)
        except Exception:
            return _result(409, "SIDE_EFFECT_UNKNOWN", "The scheduler result is unknown.", state="UNKNOWN")
        if not isinstance(body, dict) or body.get("side_effect") not in (False, None):
            return _result(409, "SIDE_EFFECT_UNKNOWN", "The scheduler result is unknown.", state="UNKNOWN")
        return _result(int(body.get("http") or 200), body.get("code"), body.get("message") or "", state=body.get("state"))
    return _result(422, "SCHEMA_UNSUPPORTED", "The action is not recognized.")


class _ClosedPort:
    def execute(self, operation, body, route_class="isolated"):
        del operation, body, route_class
        raise RuntimeError("closed")


def _authorize_action(principal: Principal, task_id: str, operation: str, scope: str) -> None:
    authorize(principal, operation, _binding(principal, task_id, operation, scope))


def _binding(principal: Principal, task_id: str, operation: str, scope: str) -> ResourceBinding:
    return ResourceBinding(
        repo_id=principal.repo_id,
        app_id=principal.app_id,
        producer_id=principal.producer_id,
        stream_id=principal.stream_id,
        operation=operation,
        scope=scope,
        task_id=task_id,
        target_app_id=principal.app_id,
    )


def _task_ids(principal: Principal) -> set[str]:
    return {item[5:] for item in principal.scopes if item.startswith("task:") and len(item) > 5}


def _digest(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _result(http: int, code, detail: str, *, reason=None, state=None, replayed=False, forwarded=False) -> dict:
    return {
        "schema_version": 2,
        "http": http,
        "code": code,
        "detail": detail,
        "reason": reason,
        "state": state,
        "side_effect": False,
        "accepted_as_success": False,
        "production_enabled": False,
        "replayed": replayed,
        "forwarded": forwarded,
        "connected_executor": False,
    }
