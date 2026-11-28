"""Durable manual actions.

The operation id and the full intent are committed before the executor runs.
A crash or an unknown result stays on that intent. A new idempotency key
cannot start the side effect again. Scheduler buttons run only against the
W04 object the ledger currently binds, and a button with no object is not
enabled. Production merge, deploy, and publish stay closed.
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
from app.control_joint.receipts import query_operation_receipt
from app.control_scheduler.digest import candidate_digest

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
_HELD = frozenset({"BLOCKED", "FAILED", "RETRY_WAIT"})
_FENCE_FIELDS = ("action", "repo_id", "app_id", "task_id", "chain_id", "candidate_id", "round_id", "head", "base", "merge", "tree")
_EFFECT_FIELDS = ("repo_id", "app_id", "task_id", "chain_id", "round_id")


def action_specs():
    return dict(_ACTIONS)


class ManualActions:
    def __init__(self, root: Path, executor=None):
        self.root = Path(root)
        self.executor = executor or execute_isolated
        self.path = self.root / "manual-actions.sqlite3"

    def describe(self, principal: Principal, task_id: str) -> list[dict]:
        board = _board(self.root, principal, task_id)
        found = []
        for action, (operation, scope, label) in _ACTIONS.items():
            binding = _binding_for(action, board)
            connected = binding is not None and self.executor is execute_isolated and action not in _CLOSED
            if self.executor is not execute_isolated and action not in _CLOSED:
                connected = True
            try:
                _authorize_action(principal, task_id, operation, scope)
            except AuthzError as exc:
                found.append(_button(action, label, task_id, False, exc.code, exc.reason, connected=False, binding=None))
                continue
            enabled = connected and binding is not None or (connected and self.executor is not execute_isolated)
            if action in _CLOSED:
                enabled = False
                connected = False
            if board.get("blocked") and action in _SCHEDULER and self.executor is execute_isolated:
                enabled = False
                connected = False
            found.append(_button(
                action, label, task_id, enabled, None if enabled else "NOT_CONNECTED",
                None if enabled else "no bound object",
                connected=connected and enabled,
                binding=binding if enabled else None,
            ))
        return found

    def submit(self, principal: Principal, body: dict) -> dict:
        parsed = _parse(body)
        if parsed.get("error"):
            return parsed["error"]
        action = parsed["action"]
        task_id = parsed["task_id"]
        operation, scope, _label = _ACTIONS[action]
        try:
            _authorize_action(principal, task_id, operation, scope)
        except AuthzError as exc:
            return _result(exc.status, exc.code, exc.detail, reason=exc.reason)
        if task_id not in _task_ids(principal):
            return _result(403, "NOT_AUTHORIZED", "The task is outside the server binding.", reason="cross_task")
        custom = self.executor is not execute_isolated
        board = _board(self.root, principal, task_id)
        binding = None if custom else _binding_for(action, board)
        if action in _SCHEDULER and not custom:
            if board.get("blocked"):
                return _result(409, "BLOCKED", "The scheduler ledger cannot be read.", forwarded=False)
            if binding is None:
                return _result(409, "NOT_CONNECTED", "The action is not bound to a W04 object.", forwarded=False)
            named = _named(parsed, binding, revision=False)
            if named is not None:
                return named
        intent = _intent(principal, parsed, binding if not custom else None)
        fence_id = _fence_id(intent)
        payload_hash = _digest_bytes({"action": action, "task_id": task_id, "expected_revision": parsed["revision"], "evidence_ref": parsed["evidence"], "intent": intent})
        key_hash = hashlib.sha256(parsed["key"].encode("utf-8")).hexdigest()
        reserved = self._reserve(principal.principal_id, key_hash, payload_hash, intent, fence_id)
        if reserved["kind"] == "mismatch":
            return _result(409, "IDEMPOTENCY_PAYLOAD_MISMATCH", "The idempotency key was already used with a different payload.", replayed=False, forwarded=False)
        if reserved["kind"] == "replay":
            replay = dict(reserved["result"])
            replay["replayed"] = True
            replay["forwarded"] = False
            replay["accepted_as_success"] = False
            if replay.get("state") == "UNKNOWN":
                replay["side_effect"] = None
            return replay
        if reserved["kind"] == "blocked":
            return _result(
                409, "SIDE_EFFECT_UNKNOWN", "An unresolved operation already exists for this intent.",
                state="UNKNOWN", replayed=True, side_effect=None,
            )
        if reserved["kind"] == "retry":
            try:
                _authorize_action(principal, task_id, operation, scope)
            except AuthzError as exc:
                return _result(exc.status, exc.code, exc.detail, reason=exc.reason, state="UNKNOWN")
            if task_id not in _task_ids(principal):
                return _result(403, "NOT_AUTHORIZED", "The task is outside the server binding.", reason="cross_task", state="UNKNOWN")
            if action in _SCHEDULER and not custom and binding is not None:
                named = _named(parsed, binding, revision=True)
                if named is not None:
                    return named
        elif action in _SCHEDULER and not custom and binding is not None:
            named = _named(parsed, binding, revision=True)
            if named is not None:
                return self._settle(principal.principal_id, key_hash, named)
        if not self._claim(principal.principal_id, key_hash):
            return _result(409, "SIDE_EFFECT_UNKNOWN", "The operation is already in flight.", state="UNKNOWN", replayed=True, side_effect=None)
        produced = self._execute(
            principal,
            intent,
            parsed,
            reserved["operation_id"],
            key_hash,
            reserved.get("executor_kind") or _executor_kind(self.executor),
        )
        settled = self._settle(principal.principal_id, key_hash, produced)
        return settled

    def _execute(self, principal: Principal, intent: dict, parsed: dict, operation_id: str, key_hash: str, executor_kind: str) -> dict:
        if not self._enter(principal.principal_id, key_hash):
            return _result(409, "SIDE_EFFECT_UNKNOWN", "The execution fence was not entered.", state="UNKNOWN", side_effect=None)
        payload = dict(intent)
        payload["evidence_ref"] = parsed["evidence"]
        payload["expected_revision"] = parsed["revision"]
        payload["operation_id"] = operation_id
        try:
            produced = self.executor(self.root, principal, payload)
        except Exception:
            return _result(409, "SIDE_EFFECT_UNKNOWN", "The action result is unknown.", state="UNKNOWN", side_effect=None)
        if not isinstance(produced, dict) or produced.get("side_effect") not in (False, None):
            return _result(409, "SIDE_EFFECT_UNKNOWN", "The action result is unknown.", state="UNKNOWN", side_effect=None)
        http = int(produced.get("http") or 500)
        authority = _execution_authority(intent)
        receipt = query_operation_receipt(
            self.root,
            operation_id=operation_id,
            expected_revision=intent.get("revision") if type(intent.get("revision")) is int else 0,
            candidate_digest=intent.get("digest") or "",
            repo_id=intent.get("repo_id") or "",
            app_id=intent.get("app_id") or "",
            authority=authority,
        )
        if receipt.get("status") == "applied":
            body = receipt.get("result") if isinstance(receipt.get("result"), dict) else {}
            return _result(
                int(body.get("http") or http),
                body.get("code", produced.get("code")),
                body.get("detail") or produced.get("detail") or "",
                reason=produced.get("reason"),
                state=body.get("state", produced.get("state")),
                forwarded=True,
            )
        proof = _non_execution_proof(receipt, authority, executor_kind, self.executor)
        if http < 400 or proof is None:
            # A status below 400 is not success without an exact receipt.
            # A non-success status is not, by itself, proof that nothing ran.
            return _result(409, "SIDE_EFFECT_UNKNOWN", "The action has no execution receipt.", state="UNKNOWN", side_effect=None)
        return _result(
            http,
            produced.get("code"),
            produced.get("detail") or "",
            reason=produced.get("reason"),
            state=produced.get("state"),
            forwarded=True,
            non_execution_proof=proof,
        )

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        os.chmod(self.path, 0o600)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(manual_actions)")}
        if existing and "operation_id" not in existing:
            conn.close()
            raise sqlite3.DatabaseError("unsafe manual action ledger")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS manual_actions (
                operation_id TEXT NOT NULL UNIQUE,
                fence_id TEXT NOT NULL,
                effect_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                principal_id TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                intent_json TEXT NOT NULL,
                state TEXT NOT NULL,
                result_json TEXT,
                fence_phase TEXT NOT NULL DEFAULT 'RESERVED',
                executor_kind TEXT NOT NULL DEFAULT 'custom',
                PRIMARY KEY (principal_id, key_hash)
            )
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(manual_actions)")}
        if "fence_id" not in columns:
            conn.execute("ALTER TABLE manual_actions ADD COLUMN fence_id TEXT NOT NULL DEFAULT ''")
            conn.execute("ALTER TABLE manual_actions ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
        if "effect_id" not in columns:
            conn.execute("ALTER TABLE manual_actions ADD COLUMN effect_id TEXT NOT NULL DEFAULT ''")
        if "fence_phase" not in columns:
            conn.execute("ALTER TABLE manual_actions ADD COLUMN fence_phase TEXT NOT NULL DEFAULT 'ENTERED'")
        if "executor_kind" not in columns:
            conn.execute("ALTER TABLE manual_actions ADD COLUMN executor_kind TEXT NOT NULL DEFAULT 'custom'")
        return conn

    def _reserve(self, principal_id: str, key_hash: str, payload_hash: str, intent: dict, fence_id: str) -> dict:
        effect_id = _effect_id(intent)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = _key_row(conn, principal_id, key_hash)
            if row is not None:
                if row["payload_hash"] != payload_hash:
                    conn.execute("COMMIT")
                    return {"kind": "mismatch"}
                recovered = self._recover(conn, row)
                conn.execute("COMMIT")
                return recovered
            unresolved = conn.execute(
                "SELECT * FROM manual_actions WHERE effect_id=? AND state IN ('PENDING','IN_FLIGHT','UNKNOWN') ORDER BY generation",
                (effect_id,),
            ).fetchone()
            if unresolved is not None:
                self._recover(conn, unresolved)
                still = conn.execute(
                    "SELECT 1 FROM manual_actions WHERE effect_id=? AND state IN ('PENDING','IN_FLIGHT','UNKNOWN')",
                    (effect_id,),
                ).fetchone()
                if still is not None:
                    conn.execute("COMMIT")
                    return {"kind": "blocked"}
            generation = conn.execute(
                "SELECT COALESCE(MAX(generation), 0) AS generation FROM manual_actions WHERE effect_id=?",
                (effect_id,),
            ).fetchone()["generation"] + 1
            operation_id = _operation_id(fence_id, generation)
            executor_kind = _executor_kind(self.executor)
            conn.execute(
                """
                INSERT INTO manual_actions(
                    operation_id, fence_id, effect_id, generation, principal_id, key_hash, payload_hash, intent_json,
                    state, result_json, fence_phase, executor_kind
                ) VALUES(?,?,?,?,?,?,?,?,'PENDING',NULL,'RESERVED',?)
                """,
                (operation_id, fence_id, effect_id, generation, principal_id, key_hash, payload_hash, _dump(intent), executor_kind),
            )
            conn.execute("COMMIT")
            return {"kind": "reserved", "operation_id": operation_id, "generation": generation, "executor_kind": executor_kind}
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            return {"kind": "blocked"}
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _recover(self, conn, row) -> dict:
        intent = json.loads(row["intent_json"])
        revision = intent.get("revision") if type(intent.get("revision")) is int else 0
        receipt = query_operation_receipt(
            self.root,
            operation_id=row["operation_id"],
            expected_revision=revision,
            candidate_digest=intent.get("digest") or "",
            repo_id=intent.get("repo_id") or "",
            app_id=intent.get("app_id") or "",
            authority=_execution_authority(intent),
        )
        if receipt.get("status") == "applied":
            body = receipt.get("result") if isinstance(receipt.get("result"), dict) else {}
            result = _result(
                int(body.get("http") or 200),
                body.get("code"),
                body.get("detail") or body.get("message") or "",
                state=body.get("state"),
                replayed=True,
                forwarded=True,
            )
            self._mark(conn, row, "SETTLED", result)
            return {"kind": "replay", "result": result}
        phase = row["fence_phase"] if "fence_phase" in row.keys() else ""
        kind = row["executor_kind"] if "executor_kind" in row.keys() else "custom"
        if row["state"] == "IN_FLIGHT" and phase == "RESERVED":
            self._rearm(conn, row)
            return {"kind": "retry", "operation_id": row["operation_id"], "generation": row["generation"], "executor_kind": kind}
        if (
            row["state"] == "IN_FLIGHT"
            and receipt.get("status") == "absent"
            and _execution_authority(intent) == "W04"
            and kind == "isolated"
            and self.executor is execute_isolated
        ):
            self._rearm(conn, row)
            return {"kind": "retry", "operation_id": row["operation_id"], "generation": row["generation"], "executor_kind": kind}
        if row["state"] == "IN_FLIGHT":
            return {"kind": "blocked"}
        if row["state"] == "SETTLED" and row["result_json"]:
            return {"kind": "replay", "result": json.loads(row["result_json"])}
        if receipt.get("status") == "absent" and row["state"] == "PENDING":
            return {"kind": "retry", "operation_id": row["operation_id"], "generation": row["generation"], "executor_kind": kind}
        if row["result_json"]:
            return {"kind": "replay", "result": json.loads(row["result_json"])}
        return {"kind": "replay", "result": _result(409, "SIDE_EFFECT_UNKNOWN", "The action result is unknown.", state="UNKNOWN", replayed=True, side_effect=None)}

    def _claim(self, principal_id: str, key_hash: str) -> bool:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE manual_actions SET state='IN_FLIGHT', fence_phase='RESERVED'
                WHERE principal_id=? AND key_hash=? AND state='PENDING'
                """,
                (principal_id, key_hash),
            )
            conn.execute("COMMIT")
            return cursor.rowcount == 1
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _enter(self, principal_id: str, key_hash: str) -> bool:
        """Record that the executor is about to be called.

        Until this commit, an IN_FLIGHT row is still RESERVED and is proof
        the executor was not entered.
        """

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE manual_actions SET fence_phase='ENTERED'
                WHERE principal_id=? AND key_hash=? AND state='IN_FLIGHT' AND fence_phase='RESERVED'
                """,
                (principal_id, key_hash),
            )
            conn.execute("COMMIT")
            return cursor.rowcount == 1
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _rearm(self, conn, row) -> None:
        conn.execute(
            """
            UPDATE manual_actions SET state='PENDING', fence_phase='RESERVED', result_json=NULL
            WHERE principal_id=? AND key_hash=? AND state='IN_FLIGHT'
            """,
            (row["principal_id"], row["key_hash"]),
        )

    def _mark(self, conn, row, state: str, result: dict) -> None:
        conn.execute(
            "UPDATE manual_actions SET state=?, result_json=? WHERE principal_id=? AND key_hash=?",
            (state, _dump(result), row["principal_id"], row["key_hash"]),
        )

    def _settle(self, principal_id: str, key_hash: str, produced: dict) -> dict:
        state = "UNKNOWN" if produced.get("state") == "UNKNOWN" or produced.get("code") == "SIDE_EFFECT_UNKNOWN" else "SETTLED"
        produced = dict(produced)
        produced["accepted_as_success"] = False
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE manual_actions SET state=?, result_json=? WHERE principal_id=? AND key_hash=? AND state IN ('PENDING','IN_FLIGHT')",
                (state, _dump(produced), principal_id, key_hash),
            )
            if cursor.rowcount != 1:
                current = _key_row(conn, principal_id, key_hash)
                conn.execute("COMMIT")
                if current is not None and current["result_json"]:
                    stored = json.loads(current["result_json"])
                    stored["replayed"] = True
                    stored["accepted_as_success"] = False
                    return stored
                return _result(409, "SIDE_EFFECT_UNKNOWN", "The action was not settled.", state="UNKNOWN")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return produced


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

        subject = payload.get("round_id") or payload.get("chain_id") or ""
        if not subject:
            return _result(409, "NOT_CONNECTED", "The action is not bound to a W04 object.", forwarded=False)
        try:
            scheduler = Scheduler(schedule, _ClosedPort(), _ClosedPort(), repo_id=principal.repo_id)
            body = scheduler.intervene(
                action=action,
                subject_id=subject,
                evidence_ref=payload.get("evidence_ref") or "",
                successor_id=payload.get("successor_id") or "",
                caller={
                    "id": principal.principal_id,
                    "kind": principal.kind,
                    "app_id": principal.app_id,
                    "task_id": payload["task_id"],
                },
                operation_id=payload.get("operation_id"),
                expected_revision=payload.get("expected_revision") if type(payload.get("expected_revision")) is int else None,
                candidate_digest=payload.get("digest"),
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
    authorize(principal, operation, _resource(principal, task_id, operation, scope))


def _resource(principal: Principal, task_id: str, operation: str, scope: str) -> ResourceBinding:
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


def _parse(body: dict) -> dict:
    if not isinstance(body, dict):
        return {"error": _result(422, "SCHEMA_UNSUPPORTED", "The action is not recognized.")}
    action = body.get("action")
    task_id = body.get("task_id")
    key = body.get("idempotency_key")
    revision = body.get("expected_revision")
    evidence = body.get("evidence_ref")
    if action not in _ACTIONS or not isinstance(task_id, str) or not task_id:
        return {"error": _result(422, "SCHEMA_UNSUPPORTED", "The action is not recognized.")}
    if not isinstance(key, str) or not key or len(key) > 200 or "\n" in key:
        return {"error": _result(422, "SCHEMA_UNSUPPORTED", "The idempotency key is invalid.")}
    if isinstance(revision, bool) or type(revision) is not int:
        return {"error": _result(422, "SCHEMA_UNSUPPORTED", "expected_revision must be an integer.")}
    if evidence is not None and (not isinstance(evidence, str) or not evidence or len(evidence) > 200):
        return {"error": _result(422, "SCHEMA_UNSUPPORTED", "evidence_ref is invalid.")}
    chain_id = body.get("chain_id")
    candidate_id = body.get("candidate_id")
    successor_id = body.get("successor_id")
    digest = body.get("digest")
    for name, value in (("chain_id", chain_id), ("candidate_id", candidate_id), ("successor_id", successor_id)):
        if value is not None and (not isinstance(value, str) or not value or len(value) > 200):
            return {"error": _result(422, "SCHEMA_UNSUPPORTED", f"{name} is invalid.")}
    if digest is not None and (not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
        return {"error": _result(422, "SCHEMA_UNSUPPORTED", "digest is invalid.")}
    return {
        "action": action,
        "task_id": task_id,
        "key": key,
        "revision": revision,
        "evidence": evidence or "",
        "chain_id": chain_id,
        "candidate_id": candidate_id,
        "successor_id": successor_id,
        "digest": digest,
    }


def _board(root: Path, principal: Principal, task_id: str) -> dict:
    schedule = Path(root) / "scheduler"
    database = schedule / "scheduler.sqlite3"
    if not database.is_file():
        return {"blocked": False, "chains": [], "rounds": []}
    from app.control_scheduler.errors import SchedulerError
    from app.control_scheduler.scheduler import Scheduler

    try:
        scheduler = Scheduler(schedule, _ClosedPort(), _ClosedPort(), repo_id=principal.repo_id)
        page = scheduler.query_board(app_id=principal.app_id, task_ids={task_id}, limit=50)
    except (OSError, ValueError, sqlite3.Error, SchedulerError):
        return {"blocked": True, "chains": [], "rounds": []}
    chains = list(page["chains"])
    cursor = page.get("next_cursor")
    while cursor:
        try:
            more = scheduler.query_board(app_id=principal.app_id, task_ids={task_id}, limit=50, after=cursor)
        except (OSError, ValueError, sqlite3.Error, SchedulerError):
            return {"blocked": True, "chains": [], "rounds": []}
        chains.extend(more["chains"])
        cursor = more.get("next_cursor")
    return {"blocked": False, "chains": chains, "rounds": page["rounds"]}


def _binding_for(action: str, board: dict):
    if board.get("blocked") or action not in _SCHEDULER:
        return None
    chains = {row["chain_id"]: row for row in board.get("chains") or []}
    if action in {"unblock", "retry_phase"}:
        matches = []
        for row in board.get("rounds") or []:
            chain = chains.get(row.get("chain_id"))
            held = row.get("auth_state") in _HELD or (chain and chain.get("queue") == "HUMAN_HOLD")
            if held and chain:
                matches.append(_object(chain, row.get("round_id")))
        if len(matches) == 1:
            return matches[0]
        return None
    rejected = [row for row in board.get("chains") or [] if row.get("state") == "REJECTED"]
    successors = [row for row in board.get("chains") or [] if row.get("candidate_id") and row.get("state") != "REJECTED"]
    if action == "authorize_repair" and len(rejected) == 1 and successors:
        chosen = _object(rejected[0], "")
        chosen["successor_id"] = successors[0]["chain_id"]
        return chosen
    return None


def _object(chain: dict, round_id: str) -> dict:
    body = {
        "repo_id": chain.get("repo_id"),
        "app_id": chain.get("app_id"),
        "task_id": chain.get("task_id"),
        "chain_id": chain.get("chain_id"),
        "candidate_id": chain.get("candidate_id") or "",
        "revision": chain.get("revision") if type(chain.get("revision")) is int else 0,
        "head": chain.get("head") or "",
        "base": chain.get("base") or "",
        "merge": chain.get("merge") or "",
        "tree": chain.get("tree") or "",
        "round_id": round_id or "",
    }
    body["digest"] = _object_digest(body)
    return body


def _object_digest(body: dict) -> str:
    return candidate_digest(body)


def _fence_id(intent: dict) -> str:
    encoded = {key: intent.get(key) for key in _FENCE_FIELDS}
    return hashlib.sha256(_dump(encoded).encode()).hexdigest()


def _effect_id(intent: dict) -> str:
    encoded = {key: intent.get(key) for key in _EFFECT_FIELDS}
    return hashlib.sha256(_dump(encoded).encode()).hexdigest()


def _execution_authority(intent: dict) -> str:
    if intent.get("action") in _SCHEDULER and intent.get("chain_id"):
        return "W04"
    return ""


def _operation_id(fence_id: str, generation: int) -> str:
    return hashlib.sha256(f"{fence_id}:{generation}".encode()).hexdigest()


def _named(parsed: dict, binding: dict, revision: bool = True):
    for field, detail in (
        ("chain_id", "The action is not bound to that chain."),
        ("candidate_id", "The action is not bound to that candidate."),
        ("digest", "The action digest does not match the ledger."),
    ):
        value = parsed.get(field)
        if not isinstance(value, str) or not value or value != binding.get(field):
            return _result(409, "OBJECT_MISMATCH", detail, forwarded=False)
    if binding.get("successor_id") and parsed.get("successor_id") != binding.get("successor_id"):
        return _result(409, "OBJECT_MISMATCH", "The action is not bound to that successor.", forwarded=False)
    if revision and parsed["revision"] != binding["revision"]:
        return _result(409, "CONFLICT_REVISION", "expected_revision does not match the bound object.", forwarded=False)
    return None


def _intent(principal: Principal, parsed: dict, binding) -> dict:
    if binding is None:
        body = {
            "repo_id": principal.repo_id,
            "app_id": principal.app_id,
            "task_id": parsed["task_id"],
            "chain_id": parsed["chain_id"] or "",
            "candidate_id": parsed["candidate_id"] or "",
            "revision": parsed["revision"],
            "head": "",
            "base": "",
            "merge": "",
            "tree": "",
            "round_id": "",
        }
        body["digest"] = parsed["digest"] or _object_digest(body)
        body["action"] = parsed["action"]
        return body
    intent = dict(binding)
    intent["action"] = parsed["action"]
    intent["revision"] = binding["revision"]
    return intent


def _button(action, label, task_id, enabled, code, reason, *, connected, binding):
    body = {
        "id": action,
        "label": label,
        "task_id": task_id,
        "enabled": bool(enabled),
        "code": code,
        "reason": reason,
        "calls_authority": True,
        "connected_executor": bool(connected),
    }
    if binding and enabled:
        body.update({
            "repo_id": binding["repo_id"],
            "app_id": binding["app_id"],
            "chain_id": binding["chain_id"],
            "candidate_id": binding["candidate_id"],
            "revision": binding["revision"],
            "digest": binding["digest"],
            "round_id": binding.get("round_id") or "",
        })
    return body


def _key_row(conn, principal_id, key_hash):
    return conn.execute(
        "SELECT * FROM manual_actions WHERE principal_id=? AND key_hash=?",
        (principal_id, key_hash),
    ).fetchone()


def _dump(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _digest_bytes(payload: dict) -> str:
    return hashlib.sha256(_dump(payload).encode()).hexdigest()


def _executor_kind(executor) -> str:
    return "isolated" if executor is execute_isolated else "custom"


def _non_execution_proof(receipt: dict, authority: str, executor_kind: str, executor) -> str | None:
    """Proof that the isolated scheduler commit did not happen.

    A non-success HTTP status is not this proof. A snapshot that failed,
    timed out, or hit its budget is ``unknown`` and is not this proof.
    """

    if receipt.get("status") != "absent" or authority != "W04":
        return None
    if executor_kind == "isolated" and executor is execute_isolated:
        return "w04_absent"
    return None


def _result(
    http: int,
    code,
    detail: str,
    *,
    reason=None,
    state=None,
    replayed=False,
    forwarded=False,
    side_effect=False,
    non_execution_proof=None,
) -> dict:
    return {
        "schema_version": 2,
        "http": http,
        "code": code,
        "detail": detail,
        "reason": reason,
        "state": state,
        "side_effect": side_effect,
        "non_execution_proof": non_execution_proof,
        "http_proves_no_side_effect": False,
        "accepted_as_success": False,
        "production_enabled": False,
        "replayed": replayed,
        "forwarded": forwarded,
        "connected_executor": False,
    }
