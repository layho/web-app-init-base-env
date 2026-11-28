"""Isolated schema 2 event engine.

Authentication and authorization go through the W02 ``app.control_authz``
package. This package does not keep a second implementation. Client actor,
trusted_source, authorizer, test-proof, and guard strings are not authority.
Binding fields come from the event or the named resource. This process does
not mount production routes.

A write commits its nonce in the same SQLite transaction as the event,
authorization row, report row, or cursor. The registry file is not a second
commit for that nonce. The generic event outbox queues the event in that
same transaction and is not wired to production delivery.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

from app.control_authz import AuthnError, AuthzError, AuthzService, Principal, PrincipalRegistry, ResourceBinding
from app.control_authz.proof import checked_message
from app.feedback_progress import STAGES, STATUSES

from .auth_bind import POLICY_REVISION, STREAM_REPAIR, document_for_event, document_for_report, document_for_resource, evidence_key, guard_key, host_text, report_business_digest
from .historical_auth import historical_authorization
from .auth_state import load_state
from .errors import ControlError
from .event_outbox import EventOutbox
from .identity import PRODUCTION_PROTOCOL_ENABLED, RepositoryIdentity
from .inbox import decide
from .legacy import parse_comments, read_operations
from .outbox import DEFAULT_CHANNELS, OutboxLimits, ReportOutbox
from .redact import redact
from .reducer import check_transition, object_key, prefer_fact, progress_rank
from .schema import authorize_source, split_request
from .store import Store

_REVISION_CLASSES = {"task", "candidate", "deployment", "resource", "authorization"}
_OPERATION = "event_append"
_PAGE_LIMIT = 50
TRUSTED_EXECUTORS = frozenset({"macci-isolated"})
PACKAGE_COMPLETE = True
FOLLOW_UPS = ()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identity(event: dict) -> str:
    visible = {key: value for key, value in event.items() if not str(key).startswith("_") and key != "guard"}
    encoded = json.dumps(visible, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _signed_fields(event: dict) -> dict:
    return {
        "repo_id": event.get("repo_id") or "",
        "app_id": event.get("app_id") or "",
        "task_id": event.get("task_id") or "",
        "candidate_id": event.get("candidate_id") or "",
        "head": event.get("head") or "",
        "base": event.get("base") or "",
        "merge": event.get("merge") or "",
        "tree": event.get("tree") or "",
        "stream_id": event.get("stream_id") or "",
        "producer_id": event.get("producer_id") or "",
    }


def _authority_record(event, digest, principal, proof, server_source, message, document_fields) -> str:
    proof = proof if isinstance(proof, dict) else {}
    return json.dumps(
        {
            "signature": proof.get("signature") or "",
            "message_hex": message.hex(),
            "principal_id": principal,
            "signer_id": proof.get("signer_id") or "",
            "key_id": proof.get("key_id") or "",
            "server_source": server_source,
            "authoritative": bool(event.get("_authoritative")),
            "authorized": True,
            "event_digest": digest,
            "signed": document_fields,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _scope_key(scope: dict) -> str:
    return json.dumps(scope, sort_keys=True, separators=(",", ":"))


def _tasks(principal) -> set[str]:
    return {item.removeprefix("task:") for item in principal.scopes if item.startswith("task:")}


class Engine:
    def __init__(
        self,
        root: Path,
        identity: RepositoryIdentity,
        store: Store,
        outbox: ReportOutbox,
        authorizer=None,
        *,
        authz: AuthzService,
        proven_guards: dict | None = None,
        evidence_bindings: dict | None = None,
    ):
        self.root = root
        self.identity = identity
        self.store = store
        self.outbox = outbox
        self.events = EventOutbox(root, store, outbox.limits)
        self.authorizer = authorizer
        self.authz = authz
        self.proven_guards = {key: frozenset(value) for key, value in (proven_guards or {}).items()}
        self.evidence_bindings = dict(evidence_bindings or {})
        self.auth_path = root / "auth-state.json"
        self.auth_state = load_state(self.auth_path)
        self._auth_lock = threading.Lock()
        self.isolated = True
        self.production_protocol_enabled = False

    @classmethod
    def open(
        cls,
        root: Path,
        bound_token: str,
        *,
        limits: OutboxLimits | None = None,
        authorizer=None,
        production: bool = False,
        authz: AuthzService | None = None,
        proven_guards: dict | None = None,
        evidence_bindings: dict | None = None,
        trusted_executors: set[str] | None = None,
        abandon_claims: bool = True,
        event_channels: tuple[str, ...] | None = None,
    ) -> "Engine":
        if production or PRODUCTION_PROTOCOL_ENABLED:
            raise ControlError("NOT_AUTHORIZED", 403, "unverified schema 2 protocol cannot be enabled in production")
        identity = RepositoryIdentity.open(root, bound_token)
        if authz is None:
            authz = AuthzService(
                registry=PrincipalRegistry(isolated=True),
                isolated=True,
                policy_revision=POLICY_REVISION,
            )
        elif not isinstance(authz, AuthzService):
            raise ControlError("NOT_AUTHORIZED", 403, "authorizer configuration is not server-side")
        store = Store(root / "events.sqlite3")
        executors = set(TRUSTED_EXECUTORS if trusted_executors is None else trusted_executors)
        engine = cls(
            root,
            identity,
            store,
            ReportOutbox(root, store, limits, trusted_executors=executors),
            authorizer,
            authz=authz,
            proven_guards=proven_guards,
            evidence_bindings=evidence_bindings,
        )
        if event_channels is not None:
            engine.events = EventOutbox(root, store, engine.outbox.limits, event_channels)
        engine._import_file_nonces()
        engine.outbox.recover(abandon_claims=abandon_claims)
        engine.events.recover(abandon_claims=abandon_claims)
        return engine

    @property
    def repo_id(self) -> str:
        return self.identity.repo_id

    def reload_auth_state(self) -> None:
        """Reload the server principal file. The request body is not a registry."""

        with self._auth_lock:
            self.auth_state = load_state(self.auth_path)
            self._import_file_nonces()

    def enable_production_protocol(self) -> None:
        raise ControlError("NOT_AUTHORIZED", 403, "unverified schema 2 protocol cannot be enabled in production")

    def append(self, body: dict, *, crash: str | None = None) -> dict:
        event = split_request(body)
        if "policy_revision" in event and event["policy_revision"] != POLICY_REVISION:
            raise ControlError("NOT_AUTHORIZED", 403, "policy revision does not match the server")
        replay_only = False
        try:
            principal, proof, document = self._authenticate(body, event=event, operation="event.append", write=True)
        except ControlError as exc:
            if exc.code != "PROOF_REPLAY":
                raise
            principal, proof, document = self._authenticate(body, event=event, operation="event.append", write=False)
            replay_only = True
        self._authorize_append(principal, event)
        self.identity.assert_same(event["repo_id"])
        if event.get("state") == "UNKNOWN":
            raise ControlError("NOT_AUTHORIZED", 403, "a caller cannot write UNKNOWN")
        server_source = self.authz.trusted_source_for(principal)
        claimed = body.get("trusted_source", None) if isinstance(body, dict) and "trusted_source" in body else None
        if "trusted_source" in body and claimed not in (None, "", server_source):
            raise ControlError("NOT_AUTHORIZED", 403, "trusted source does not match the server principal")
        event.pop("guard", None)
        event["trusted_source"] = server_source or None
        event["_authoritative"] = authorize_source(event, server_source or None)
        if event["event_type"] == "authorization.epoch_closed" and not event["_authoritative"]:
            raise ControlError("NOT_AUTHORIZED", 403, "stream close requires the policy_service source")
        if self.authorizer is not None and self.authorizer(event) is False:
            raise ControlError("POLICY_DENIED", 403, "policy denied before idempotency")
        digest = _identity(event)
        message = checked_message("event.append", document, proof)
        scope = _scope_key(body["scope"])
        with self.store.transaction() as conn:
            existing = conn.execute(
                "SELECT payload_hash, result_json FROM idempotency WHERE principal_id=? AND scope=? AND operation=? AND idempotency_key=?",
                (principal.principal_id, scope, _OPERATION, event["idempotency_key"]),
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] != digest:
                    outcome = _error("IDEMPOTENCY_PAYLOAD_MISMATCH", 409, "idempotency key was already used with a different payload")
                else:
                    outcome = _replay(json.loads(existing["result_json"]), self._current_state(conn, event))
            elif replay_only:
                raise ControlError("PROOF_REPLAY", 403, "nonce already used")
            else:
                self._commit_nonce(conn, principal, proof, digest, "event.append")
                outcome = self._attempt(
                    conn, event, digest, principal.principal_id, scope,
                    proof=proof, server_source=server_source or "", message=message,
                )
                if outcome.get("_wrote") and not outcome.get("replayed"):
                    self.events.enqueue(conn, event, digest)
            if crash == "before-commit":
                os._exit(90)
        if crash == "after-commit":
            os._exit(94)
        return redact(_public(outcome))

    def submit_report(self, report: dict, channels: list[str] | None = None, remote=None, *, crash: str | None = None) -> dict:
        if self.production_protocol_enabled:
            raise ControlError("NOT_AUTHORIZED", 403, "production protocol is disabled")
        self._authorize_report(report)
        principal, proof, replay_only = self._authenticate_report(report)
        digest = report_business_digest(report)

        def nonce_commit(conn) -> None:
            self._commit_nonce(conn, principal, proof, digest, "report.submit")

        chosen = list(channels or DEFAULT_CHANNELS)
        return self.outbox.submit(report, chosen, remote, crash=crash, nonce_commit=nonce_commit, replay_only=replay_only)

    def deliver_pending(self, remote, *, crash: str | None = None) -> list[dict]:
        return self.outbox.deliver_pending(remote, crash=crash)

    def deliver_events(self, remote, *, crash: str | None = None) -> list[dict]:
        return self.events.deliver_pending(remote, crash=crash)

    def declare_counter_loss(self, stream_id: str) -> dict:
        with self.store.transaction() as conn:
            rows = conn.execute(
                "SELECT DISTINCT stream_epoch FROM events WHERE stream_id=? ORDER BY stream_epoch",
                (stream_id,),
            ).fetchall()
            sealed = []
            for row in rows:
                conn.execute(
                    "INSERT OR IGNORE INTO seals (stream_id, stream_epoch) VALUES (?, ?)",
                    (stream_id, row["stream_epoch"]),
                )
                sealed.append(row["stream_epoch"])
            next_epoch = (max(sealed) + 1) if sealed else 1
        return {"stream_id": stream_id, "sealed": sealed, "next_epoch": next_epoch, "event_written": False}

    def query(self, *, request: dict, stream_id: str, stream_epoch: int, after_seq: int = 0, task_id: str | None = None, limit: int = _PAGE_LIMIT) -> dict:
        principal, _proof = self._authorize_read(request, "event.read", stream_id=stream_id, task_id=task_id)
        allowed = _tasks(principal)
        selected = task_id or request.get("task_id") or (next(iter(allowed)) if len(allowed) == 1 else None)
        limit = _page_limit(limit)
        with self.store.transaction() as conn:
            watermark = self._watermark(conn, stream_id, stream_epoch)["seq"]
            items, next_after, truncated = self._items(
                conn, stream_id, stream_epoch, after_seq, event_class=None, subject=None, limit=limit, allowed_tasks=allowed
            )
            gap = after_seq > watermark or self._has_unapplied(conn, stream_id, stream_epoch)
        return {
            "schema_version": 2,
            "http": 200,
            "side_effect": False,
            "stream_id": stream_id,
            "stream_epoch": stream_epoch,
            "watermark_seq": watermark,
            "gap": gap,
            "items": items,
            "next_after_seq": next_after,
            "truncated": truncated,
            "lease_token": None,
        }

    def poll_diagnostics(self, *, request: dict, subject: str, stream_id: str, stream_epoch: int, after_seq: int, task_id: str | None = None, limit: int = _PAGE_LIMIT) -> dict:
        if type(after_seq) is not int or after_seq < 0 or type(stream_epoch) is not int:
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "diagnostic cursor is invalid")
        principal, _proof = self._authorize_read(request, "diagnostic.read", stream_id=stream_id, task_id=task_id)
        allowed = _tasks(principal)
        selected = task_id or request.get("task_id") or (next(iter(allowed)) if len(allowed) == 1 else None)
        limit = _page_limit(limit)
        if not isinstance(stream_id, str) or not stream_id.endswith(":diagnostic"):
            raise ControlError("NOT_AUTHORIZED", 403, "diagnostic read is not authorized for this stream")
        with self.store.transaction() as conn:
            watermark = self._watermark(conn, stream_id, stream_epoch)["seq"]
            gap = after_seq > watermark or self._has_unapplied(conn, stream_id, stream_epoch)
            items, next_after, truncated = self._items(
                conn, stream_id, stream_epoch, after_seq, event_class="diagnostic", subject=subject, limit=limit, allowed_tasks=allowed
            )
        return {
            "schema_version": 2,
            "http": 200,
            "side_effect": False,
            "business_side_effect": False,
            "cursor_persisted": False,
            "subject": subject,
            "stream_id": stream_id,
            "stream_epoch": stream_epoch,
            "watermark_seq": watermark,
            "gap": gap,
            "items": items,
            "next_after_seq": next_after,
            "truncated": truncated,
            "cursor_after_seq": after_seq,
            "lease_token": None,
        }

    def commit_cursor(self, *, request: dict, subject: str, stream_id: str, stream_epoch: int, after_seq: int, task_id: str | None = None) -> dict:
        if type(after_seq) is not int or after_seq < 0 or type(stream_epoch) is not int:
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "diagnostic cursor is invalid")
        signed = dict(request)
        signed["expected_revision"] = after_seq
        principal, proof = self._authorize_read(signed, "diagnostic.cursor", stream_id=stream_id, task_id=task_id, method="POST", write=True)
        allowed = _tasks(principal)
        selected = task_id or signed.get("task_id") or (next(iter(allowed)) if len(allowed) == 1 else None)
        if not stream_id.endswith(":diagnostic"):
            raise ControlError("NOT_AUTHORIZED", 403, "diagnostic cursor is not authorized for this stream")
        digest = hashlib.sha256(json.dumps({
            "operation": "diagnostic.cursor",
            "principal_id": principal.principal_id,
            "stream_id": stream_id,
            "stream_epoch": stream_epoch,
            "after_seq": after_seq,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self.store.transaction() as conn:
            watermark = self._watermark(conn, stream_id, stream_epoch)["seq"]
            if after_seq > watermark:
                raise ControlError("EVIDENCE_MISSING", 409, "cursor is past the watermark")
            self._commit_nonce(conn, principal, proof, digest, "diagnostic.cursor")
            row = conn.execute(
                "SELECT after_seq FROM cursors WHERE principal_id=? AND stream_id=? AND stream_epoch=?",
                (principal.principal_id, stream_id, stream_epoch),
            ).fetchone()
            current = 0 if row is None else row["after_seq"]
            stored = current
            if after_seq >= current:
                conn.execute(
                    """
                    INSERT INTO cursors (principal_id, stream_id, stream_epoch, after_seq)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(principal_id, stream_id, stream_epoch) DO UPDATE SET after_seq=excluded.after_seq
                    """,
                    (principal.principal_id, stream_id, stream_epoch, after_seq),
                )
                stored = after_seq
        return {
            "schema_version": 2,
            "http": 200,
            "side_effect": True,
            "business_side_effect": False,
            "cursor_persisted": True,
            "subject": subject,
            "principal_id": principal.principal_id,
            "stream_id": stream_id,
            "stream_epoch": stream_epoch,
            "cursor_after_seq": stored,
            "lease_token": None,
        }

    def cursor(self, stream_id: str, stream_epoch: int, principal_id: str | None = None) -> int | None:
        with self.store.transaction() as conn:
            if principal_id is None:
                row = conn.execute(
                    "SELECT after_seq FROM cursors WHERE stream_id=? AND stream_epoch=? ORDER BY after_seq DESC LIMIT 1",
                    (stream_id, stream_epoch),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT after_seq FROM cursors WHERE principal_id=? AND stream_id=? AND stream_epoch=?",
                    (principal_id, stream_id, stream_epoch),
                ).fetchone()
        return None if row is None else row["after_seq"]

    def dual_read(self, operations_path: Path, comments) -> dict:
        before_events = self.store.count("events")
        before_auth = self.store.count("authorizations")
        operations = read_operations(operations_path)
        parsed = parse_comments(comments or [])
        result = {
            "schema_version": 2,
            "operations": operations,
            "comments": parsed,
            "schema2_rounds": self.current_rounds(),
            "authorizations": self.authorizations(),
            "authorizations_from_legacy": [row for row in parsed["records"] if row.get("authorizes")],
            "legacy_in_watermark": False,
            "watermarks": self.watermarks(),
            "production_protocol_enabled": False,
        }
        if self.store.count("events") != before_events or self.store.count("authorizations") != before_auth:
            raise ControlError("NOT_AUTHORIZED", 403, "legacy read must not write schema 2 facts")
        return result

    def current_rounds(self) -> list[dict]:
        with self.store.transaction() as conn:
            rows = conn.execute("SELECT * FROM rounds").fetchall()
        grouped = {}
        for row in rows:
            key = (row["task_id"], row["candidate_id"])
            current = grouped.get(key)
            if current is None or row["attempt"] > current["attempt"]:
                grouped[key] = row
        return [
            {
                "task_id": row["task_id"],
                "candidate_id": row["candidate_id"],
                "run_id": row["run_id"],
                "attempt": row["attempt"],
                "status": row["status"],
                "blocked": bool(row["blocked"]),
                "source": row["source"],
                "stage_times": json.loads(row["stage_times_json"]),
            }
            for row in grouped.values()
        ]

    def authorizations(self) -> list[dict]:
        with self.store.transaction() as conn:
            rows = conn.execute("SELECT * FROM authorizations").fetchall()
            stored = conn.execute("SELECT * FROM events WHERE applied=1").fetchall()
            grouped = self._verified_authorization_groups(conn, stored)
        found = []
        for row in rows:
            key = (row["task_id"], row["candidate_id"], row["operation"])
            verdict = historical_authorization(grouped.get(key, []))
            if verdict.get("event_id") != row["event_id"] or verdict.get("revision") != row["revision"]:
                verdict = {"decision": "unknown", "reason": "projection_not_bound"}
            if verdict["decision"] == "authorized":
                revoked = False
            elif verdict["decision"] == "revoked":
                revoked = True
            else:
                revoked = True
            found.append({
                "task_id": row["task_id"],
                "candidate_id": row["candidate_id"],
                "operation": row["operation"],
                "revision": row["revision"],
                "event_id": row["event_id"],
                "revoked": revoked,
                "historical_decision": verdict["decision"],
                "historical_reason": verdict["reason"],
                "source": row["source"],
                "production_effective": False,
            })
        return found

    def _verified_authorization_groups(self, conn, rows) -> dict:
        # Use the same W01 proof verifier as the trusted board read. A stored
        # source label, applied bit or mutable authorization projection is not proof.
        from .trusted_read import _verdict

        groups = {}
        registry = self.auth_state.get("principals") or {}
        revoked = self.auth_state.get("revoked_signers") or []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
                if not isinstance(payload, dict):
                    continue
                if payload.get("event_type") not in {"authorization.granted", "authorization.revoked"}:
                    continue
                key = (payload.get("task_id"), payload.get("candidate_id"), payload.get("operation"))
                if not all(isinstance(part, str) and part for part in key):
                    continue
                bucket = groups.setdefault(key, [])
                digest = row["payload_hash"]
                event = dict(payload)
                event.pop("received_at", None)
                authority = json.loads(row["authority_json"] or "{}")
                if not isinstance(authority, dict):
                    bucket.append({"_invalid_authorization": True})
                    continue
                proof = _verdict(conn, row, event, registry, digest) if _identity(event) == digest else None
                if (
                    proof is None or proof != ("policy_service", True)
                    or authority.get("signer_id") in revoked
                    or event.get("repo_id") != self.repo_id
                    or any(event.get(field) != row[field] for field in ("event_id", "stream_id", "stream_epoch", "producer_seq"))
                ):
                    bucket.append({"_invalid_authorization": True})
                    continue
                event["_authoritative"] = True
                event["signature"] = authority.get("signature")
                if not self._evidence_proven(event):
                    bucket.append({"_invalid_authorization": True})
                    continue
                bucket.append(event)
            except (ValueError, TypeError, ControlError, KeyError, IndexError):
                # A malformed row cannot prove any grant. Fail closed for
                # all projected decisions in this snapshot.
                return {}
        return {
            key: events for key, events in groups.items()
            if not any(event.get("_invalid_authorization") for event in events)
            and len({(event.get("repo_id"), event.get("app_id"), event.get("stream_id"), event.get("stream_epoch")) for event in events}) == 1
        }

    def fact(self, key: str) -> dict | None:
        with self.store.transaction() as conn:
            return self._fact(conn, key)

    def watermarks(self) -> dict:
        with self.store.transaction() as conn:
            rows = conn.execute(
                """
                SELECT stream_id, stream_epoch, COUNT(*) AS n, COALESCE(MAX(producer_seq), 0) AS seq,
                       COALESCE(MAX(epoch_closed), 0) AS closed
                FROM events WHERE applied=1 GROUP BY stream_id, stream_epoch
                """
            ).fetchall()
        return {
            f"{row['stream_id']}@{row['stream_epoch']}": {"seq": row["seq"], "closed": bool(row["closed"]), "count": row["n"]}
            for row in rows
        }

    def health(self) -> dict:
        snapshot = self.outbox.health()
        with self.store.transaction() as conn:
            gaps = conn.execute(
                """
                SELECT stream_id, stream_epoch, COUNT(*) AS held
                FROM events WHERE applied=0 GROUP BY stream_id, stream_epoch
                """
            ).fetchall()
        snapshot["gaps"] = [
            {"stream_id": row["stream_id"], "stream_epoch": row["stream_epoch"], "held": row["held"]}
            for row in gaps
        ]
        snapshot["production_protocol_enabled"] = False
        snapshot.update(self.events.health())
        snapshot["package_complete"] = PACKAGE_COMPLETE
        snapshot["follow_ups"] = list(FOLLOW_UPS)
        snapshot["stream_repair"] = STREAM_REPAIR
        return snapshot

    def _authenticate(self, body: dict, *, event: dict | None, operation: str, write: bool, resource: dict | None = None, method: str = "POST"):
        authorizer = body.get("authorizer") if isinstance(body.get("authorizer"), str) else ""
        if event is not None:
            document = document_for_event(event, operation=operation, authorizer=authorizer, path="/api/v1/control/events")
        else:
            document = document_for_resource(
                resource or {},
                operation=operation,
                authorizer=authorizer,
                path="/api/v1/control/events",
                method=method,
            )
        if isinstance(body.get("actor"), dict):
            document["actor"] = body["actor"]
        if isinstance(body.get("proof"), dict):
            document["proof"] = body["proof"]
        principal, proof = self._authenticate_document(document, operation=operation, write=write)
        return principal, proof, document

    def _authenticate_document(self, document: dict, *, operation: str, write: bool):
        try:
            principal = self.authz.authenticate(
                operation=operation,
                body=document,
                route_class="isolated",
                now=int(time.time()),
                state=self._state_for_auth(),
                write=write,
            )
        except AuthnError as exc:
            code = "PROOF_REPLAY" if exc.reason == "replay" else "NOT_AUTHORIZED"
            raise ControlError(code, 403, exc.detail) from exc
        return principal, document.get("proof") if isinstance(document.get("proof"), dict) else {}

    def _authenticate_report(self, report: dict):
        if not isinstance(report.get("proof"), dict) or not isinstance(report.get("actor"), dict):
            raise ControlError("NOT_AUTHORIZED", 403, "report proof is required")
        document = document_for_report(report)
        document["actor"] = report["actor"]
        document["proof"] = report["proof"]
        try:
            principal, proof = self._authenticate_document(document, operation="report.submit", write=True)
            return principal, proof, False
        except ControlError as exc:
            if exc.code != "PROOF_REPLAY":
                raise
        principal, proof = self._authenticate_document(document, operation="report.submit", write=False)
        return principal, proof, True

    def _state_for_auth(self) -> dict:
        with self.store.transaction() as conn:
            nonces = self._load_nonces(conn)
        return {
            "policy": self.auth_state.get("policy") or {"revision": POLICY_REVISION},
            "principals": self.auth_state.get("principals") or {},
            "revoked_signers": list(self.auth_state.get("revoked_signers") or []),
            "nonces": nonces,
        }

    def _load_nonces(self, conn) -> dict:
        rows = conn.execute("SELECT slot, principal_id, expires_at FROM consumed_nonces").fetchall()
        return {row["slot"]: {"principal_id": row["principal_id"], "expires_at": row["expires_at"]} for row in rows}

    def _import_file_nonces(self) -> None:
        """Copy registry-file nonce slots into SQLite. SQLite remains the authority."""

        file_nonces = self.auth_state.get("nonces") or {}
        if not file_nonces:
            return
        with self.store.transaction() as conn:
            for slot, row in file_nonces.items():
                conn.execute(
                    """
                    INSERT OR IGNORE INTO consumed_nonces (slot, principal_id, expires_at, payload_hash, operation)
                    VALUES (?, ?, ?, '', 'imported')
                    """,
                    (slot, row["principal_id"], int(row["expires_at"])),
                )

    def _commit_nonce(self, conn, principal, proof: dict, payload_hash: str, operation: str) -> None:
        """Insert the nonce in the caller's transaction. A conflict rolls that transaction back."""

        if not proof:
            raise ControlError("NOT_AUTHORIZED", 403, "write proof is required")
        current = self._load_nonces(conn)
        state = {
            "policy": self.auth_state.get("policy") or {"revision": POLICY_REVISION},
            "principals": self.auth_state.get("principals") or {},
            "revoked_signers": list(self.auth_state.get("revoked_signers") or []),
            "nonces": {slot: dict(row) for slot, row in current.items()},
        }
        try:
            self.authz.consume_nonce(state, principal, proof, int(time.time()), max_nonces=10000)
        except AuthnError as exc:
            if exc.reason == "replay":
                raise ControlError("PROOF_REPLAY", 403, exc.detail) from exc
            code = exc.code or "NOT_AUTHORIZED"
            http = 429 if exc.reason == "capacity" else 403
            raise ControlError(code, http, exc.detail) from exc
        for slot, row in state["nonces"].items():
            if slot in current:
                continue
            try:
                conn.execute(
                    """
                    INSERT INTO consumed_nonces (slot, principal_id, expires_at, payload_hash, operation)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (slot, row["principal_id"], int(row["expires_at"]), payload_hash, operation),
                )
            except sqlite3.IntegrityError as exc:
                raise ControlError("PROOF_REPLAY", 403, "nonce already used") from exc
        for slot in current:
            if slot not in state["nonces"]:
                conn.execute("DELETE FROM consumed_nonces WHERE slot=?", (slot,))

    def _authorize_read(self, request: dict, operation: str, *, stream_id: str, task_id: str | None, method: str = "GET", write: bool = False):
        if not isinstance(request, dict):
            raise ControlError("NOT_AUTHORIZED", 403, "reader is not authenticated")
        if request.get("stream_id") != stream_id:
            raise ControlError("NOT_AUTHORIZED", 403, "stream is outside the signed request")
        if task_id and request.get("task_id") not in (None, "", task_id):
            raise ControlError("NOT_AUTHORIZED", 403, "task is outside the signed request")
        resource = dict(request)
        resource["stream_id"] = stream_id
        if task_id:
            resource["task_id"] = task_id
        principal, proof, _document = self._authenticate(request, event=None, operation=operation, write=write, resource=resource, method=method)
        self._bind(
            principal,
            operation,
            repo_id=_text_field(resource, "repo_id"),
            app_id=_text_field(resource, "app_id"),
            host_id=resource.get("host_id"),
            producer_id=_text_field(resource, "producer_id"),
            stream_id=stream_id,
            task_id=_text_field(resource, "task_id"),
            scope_name=operation,
        )
        return principal, proof

    def _bind(
        self,
        principal,
        operation: str,
        *,
        repo_id: str,
        app_id: str,
        host_id: object,
        producer_id: str,
        stream_id: str,
        task_id: str,
        scope_name: str,
    ) -> None:
        if app_id == "" or producer_id == "" or stream_id == "" or repo_id == "":
            raise ControlError("NOT_AUTHORIZED", 403, "resource binding is incomplete")
        from .auth_bind import SCOPE_FOR_OPERATION

        scope = SCOPE_FOR_OPERATION[scope_name]
        binding = ResourceBinding(
            repo_id=repo_id,
            app_id=app_id,
            producer_id=producer_id,
            stream_id=stream_id,
            operation=operation,
            scope=scope,
            task_id=task_id,
            target_app_id=app_id,
        )
        try:
            self.authz.authorize(principal, operation, binding)
        except AuthzError as exc:
            raise ControlError("NOT_AUTHORIZED", 403, exc.detail) from exc
        self._check_host(principal, host_id)

    def _check_host(self, principal, host_id: object) -> None:
        record = self.auth_state.get("principals", {}).get(principal.principal_id)
        if not isinstance(record, dict):
            raise ControlError("NOT_AUTHORIZED", 403, "principal is not in the server registry")
        if host_text(host_id) != host_text(record.get("host_id")):
            raise ControlError("NOT_AUTHORIZED", 403, "host is outside the server binding")

    def _authorize_append(self, principal, event: dict) -> None:
        task_id = event.get("task_id") if isinstance(event.get("task_id"), str) else ""
        if event["event_class"] == "authorization" and event["event_type"] != "authorization.epoch_closed":
            business = event.get("operation")
            if not isinstance(business, str) or business not in principal.scopes:
                raise ControlError("NOT_AUTHORIZED", 403, "authorization operation is outside the server principal")
        self._bind(
            principal,
            "event.append",
            repo_id=event.get("repo_id") if isinstance(event.get("repo_id"), str) else "",
            app_id=event.get("app_id") if isinstance(event.get("app_id"), str) else "",
            host_id=event.get("host_id"),
            producer_id=event.get("producer_id") if isinstance(event.get("producer_id"), str) else "",
            stream_id=event.get("stream_id") if isinstance(event.get("stream_id"), str) else "",
            task_id=task_id,
            scope_name="event.append",
        )

    def _authorize_report(self, report: dict) -> None:
        if not isinstance(report, dict):
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "report must be an object")
        executor = report.get("executor_id")
        record = None
        for item in self.auth_state.get("principals", {}).values():
            if isinstance(item, dict) and item.get("producer_id") == executor and item.get("kind") == "macci":
                record = item
                break
        if record is None:
            raise ControlError("UNKNOWN_EXECUTOR", 409, "report executor is not a server principal")
        try:
            principal = Principal(
                principal_id=str(record.get("principal_id") or ""),
                kind=str(record.get("kind") or ""),
                repo_id=str(record.get("repo_id") or ""),
                app_id=str(record.get("app_id") or ""),
                producer_id=str(record.get("producer_id") or ""),
                stream_id=str(record.get("stream_id") or ""),
                scopes=frozenset(str(item) for item in record.get("scopes") or []),
                trusted_source="macci",
            )
            self.authz.trusted_source_for(principal)
        except (AuthzError, TypeError) as exc:
            raise ControlError("UNKNOWN_EXECUTOR", 409, "report executor is not trusted") from exc
        for field in ("head", "base", "merge", "tree"):
            if not isinstance(report.get(field), str) or report.get(field) == "":
                raise ControlError("SCHEMA_UNSUPPORTED", 422, "report candidate is incomplete")
        self._bind(
            principal,
            "report.submit",
            repo_id=report.get("repo_id") if isinstance(report.get("repo_id"), str) else "",
            app_id=report.get("app_id") if isinstance(report.get("app_id"), str) else "",
            host_id=report.get("host_id"),
            producer_id=report.get("executor_id") if isinstance(report.get("executor_id"), str) else "",
            stream_id=report.get("stream_id") if isinstance(report.get("stream_id"), str) else "",
            task_id=report.get("task_id") if isinstance(report.get("task_id"), str) else "",
            scope_name="report.submit",
        )

    def _guards(self, event: dict) -> set[str]:
        return set(self.proven_guards.get(guard_key(event), frozenset()))

    def _attempt(self, conn, event: dict, digest: str, principal: str, scope: str, *, proof: dict, server_source: str, message: bytes) -> dict:
        received_at = _now()
        event["received_at"] = received_at
        watermarks = self._watermarks(conn, event["stream_id"])
        seals = self._seals(conn, event["stream_id"])
        later = self._has_later(conn, event["stream_id"], event["stream_epoch"], event["producer_seq"])
        action, reason = decide(event, watermarks, seals, later=later)
        history = False
        if action == "reject":
            return self._save(conn, event, digest, principal, scope, _error(reason or "EVIDENCE_MISSING", 409, "stream proof was rejected"), wrote=False)
        if action == "apply":
            failure = self._precheck(conn, event)
            if isinstance(failure, dict):
                return self._save(conn, event, digest, principal, scope, failure, wrote=False)
            history = failure in {"history", "stream-only"}
        clash = conn.execute(
            """
            SELECT event_id FROM events
            WHERE event_id=? OR (stream_id=? AND stream_epoch=? AND producer_seq=?)
            """,
            (event["event_id"], event["stream_id"], event["stream_epoch"], event["producer_seq"]),
        ).fetchone()
        if clash is not None:
            return self._save(
                conn,
                event,
                digest,
                principal,
                scope,
                _error("IDEMPOTENCY_PAYLOAD_MISMATCH", 409, "event identity or stream sequence already exists"),
                wrote=False,
            )
        stored = _stored(event)
        authority = _authority_record(event, digest, principal, proof, server_source, message, document_fields=_signed_fields(event))
        conn.execute(
            """
            INSERT INTO events (
                event_id, stream_id, stream_epoch, producer_seq, payload_hash, payload_json,
                received_at, applied, hold_reason, epoch_closed, authority_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["event_id"],
                event["stream_id"],
                event["stream_epoch"],
                event["producer_seq"],
                digest,
                json.dumps(stored, sort_keys=True),
                received_at,
                1 if history else 0,
                None if history or action == "apply" else reason,
                1 if history and event.get("epoch_closed") else 0,
                authority,
            ),
        )
        projected = False
        if history:
            applied = True
            self._record_history(conn, event)
            self._drain(conn, event["stream_id"])
        elif action == "apply":
            self._drain(conn, event["stream_id"])
            row = conn.execute("SELECT applied, hold_reason FROM events WHERE event_id=?", (event["event_id"],)).fetchone()
            applied = bool(row["applied"])
            reason = row["hold_reason"]
            projected = applied
        else:
            self._drain(conn, event["stream_id"])
            row = conn.execute("SELECT applied, hold_reason FROM events WHERE event_id=?", (event["event_id"],)).fetchone()
            applied = bool(row["applied"])
            reason = row["hold_reason"]
            projected = applied
        result = {
            "schema_version": 2,
            "http": 200,
            "fact_state": "ACCEPTED" if applied else None,
            "delivery_state": "DURABLE_LOCAL",
            "delivery_disposition": "DURABLE_LOCAL",
            "outbox_consumer": "generic",
            "applied": applied,
            "projected": projected,
            "hold_reason": None if applied else reason,
            "event_id": event["event_id"],
            "received_at": received_at,
            "replayed": False,
            "lease_token": None,
            "current_state": self._current_state(conn, event),
            "production_protocol_enabled": False,
        }
        return self._save(conn, event, digest, principal, scope, result, wrote=True)

    def _drain(self, conn, stream_id: str) -> None:
        while True:
            rows = conn.execute(
                "SELECT event_id, payload_json, hold_reason FROM events WHERE stream_id=? AND applied=0 ORDER BY stream_epoch, producer_seq",
                (stream_id,),
            ).fetchall()
            if not rows:
                return
            watermarks = self._watermarks(conn, stream_id)
            seals = self._seals(conn, stream_id)
            progressed = False
            for row in rows:
                event = _rehydrate(json.loads(row["payload_json"]))
                later = self._has_later(conn, stream_id, event["stream_epoch"], event["producer_seq"])
                action, reason = decide(event, watermarks, seals, later=later)
                if action != "apply":
                    if action == "reject":
                        conn.execute("UPDATE events SET hold_reason=? WHERE event_id=?", (reason, event["event_id"]))
                    elif row["hold_reason"] != reason:
                        conn.execute("UPDATE events SET hold_reason=? WHERE event_id=?", (reason, event["event_id"]))
                    continue
                verdict = self._precheck(conn, event)
                if isinstance(verdict, dict):
                    conn.execute("UPDATE events SET hold_reason=? WHERE event_id=?", (verdict.get("code"), event["event_id"]))
                    continue
                if verdict in {"history", "stream-only"}:
                    if verdict == "history":
                        self._record_history(conn, event)
                    conn.execute(
                        "UPDATE events SET applied=1, hold_reason=NULL, epoch_closed=? WHERE event_id=?",
                        (1 if event.get("epoch_closed") else 0, event["event_id"]),
                    )
                    progressed = True
                    break
                self._reduce(conn, event)
                conn.execute(
                    "UPDATE events SET applied=1, hold_reason=NULL, epoch_closed=? WHERE event_id=?",
                    (1 if event.get("epoch_closed") else 0, event["event_id"]),
                )
                progressed = True
                break
            if not progressed:
                return

    def _precheck(self, conn, event: dict):
        if event["event_type"] == "authorization.epoch_closed":
            if not self._epoch_authorizations_proven(conn, event):
                return _error("EVIDENCE_MISSING", 409, "epoch close requires a proven authorization prefix")
            return "stream-only"
        if event["event_class"] not in _REVISION_CLASSES:
            return None
        if not event.get("_authoritative"):
            return "history"
        key = object_key(event)
        current = self._fact(conn, key)
        expected = event.get("expected_revision")
        revision = event.get("revision")
        try:
            if current is None or not current["authoritative"]:
                if expected not in (0, revision):
                    return _error("CONFLICT_REVISION", 409, "expected_revision does not match the stored revision")
                check_transition(event["event_class"], None if current is None else current["state"], event.get("state"), self._guards(event))
                return None
            allowed = {current["owner_producer"]}
            if current.get("successor_producer"):
                allowed.add(current["successor_producer"])
            if event["producer_id"] not in allowed:
                return _error("CONFLICT_REVISION", 409, "cross-stream revision was rejected")
            if revision < current["revision"] or not event.get("_authoritative"):
                return "history"
            if expected != current["revision"]:
                return _error("CONFLICT_REVISION", 409, "expected_revision does not match the stored revision")
            if revision == current["revision"]:
                return _error("CONFLICT_REVISION", 409, "same revision conflicts with the current fact")
            check_transition(event["event_class"], current["state"], event.get("state"), self._guards(event))
        except ControlError as exc:
            body = exc.public()
            body["applied"] = False
            return body
        return None

    def _epoch_authorizations_proven(self, conn, event: dict) -> bool:
        rows = conn.execute(
            "SELECT payload_json FROM events WHERE stream_id=? AND stream_epoch=? AND applied=1",
            (event["stream_id"], event["stream_epoch"]),
        ).fetchall()
        for row in rows:
            payload = _rehydrate(json.loads(row["payload_json"]))
            if payload.get("event_class") != "authorization":
                continue
            if payload.get("event_type") not in {"authorization.granted", "authorization.revoked"}:
                continue
            if not payload.get("_authoritative") or not self._evidence_proven(payload):
                return False
        return True

    def _evidence_proven(self, event: dict) -> bool:
        if not event.get("_authoritative"):
            return False
        bound = self.evidence_bindings.get(evidence_key(event))
        if bound is None:
            return True
        return event.get("evidence_ref") == bound

    def _reduce(self, conn, event: dict) -> None:
        if event.get("classification") == "never_existed":
            return
        if event["event_class"] == "diagnostic":
            self._write_diagnostic(conn, event)
            return
        if event["event_class"] in _REVISION_CLASSES:
            if not event.get("_authoritative"):
                self._record_history(conn, event)
                return
            current = self._fact(conn, object_key(event))
            if current is not None and not _wins(current, event):
                self._record_history(conn, event)
                return
            self._write_fact(conn, event)
            self._record_history(conn, event)
        if event["event_class"] == "task":
            self._write_round(conn, event)
        if event["event_type"] == "authorization.granted" and self._evidence_proven(event):
            self._write_authorization(conn, event, revoked=0)
        elif event["event_type"] == "authorization.revoked" and self._evidence_proven(event):
            self._write_authorization(conn, event, revoked=1)

    def _write_fact(self, conn, event: dict) -> None:
        key = object_key(event)
        current = self._fact(conn, key)
        successor = event.get("successor_producer") if isinstance(event.get("successor_producer"), str) else None
        owner = event["producer_id"]
        if current is not None and current["authoritative"]:
            if event["producer_id"] == current["owner_producer"]:
                owner = current["owner_producer"]
            elif event["producer_id"] == current.get("successor_producer"):
                owner = event["producer_id"]
                successor = successor if event.get("successor_producer") else None
        incoming = {
            "event_class": event["event_class"],
            "revision": event.get("revision") or 0,
            "authoritative": bool(event.get("_authoritative")),
            "producer_id": event["producer_id"],
            "event_id": event["event_id"],
            "state": event.get("state"),
        }
        if current is not None:
            chosen = prefer_fact(current, incoming, incoming["authoritative"])
            if chosen.get("event_id") == current["event_id"]:
                return
        conn.execute(
            """
            INSERT INTO facts (
                object_key, revision, event_class, authoritative, producer_id, owner_producer,
                successor_producer, event_id, state, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(object_key) DO UPDATE SET
                revision=excluded.revision, event_class=excluded.event_class, authoritative=excluded.authoritative,
                producer_id=excluded.producer_id, owner_producer=excluded.owner_producer,
                successor_producer=excluded.successor_producer, event_id=excluded.event_id, state=excluded.state,
                payload_json=excluded.payload_json
            """,
            (
                key,
                incoming["revision"],
                incoming["event_class"],
                1 if incoming["authoritative"] else 0,
                incoming["producer_id"],
                owner,
                successor,
                incoming["event_id"],
                incoming["state"],
                json.dumps(_stored(event), sort_keys=True),
            ),
        )

    def _record_history(self, conn, event: dict) -> None:
        key = object_key(event)
        if not key or event["event_class"] not in _REVISION_CLASSES:
            return
        conn.execute(
            """
            INSERT INTO fact_history (object_key, revision, authoritative, producer_id, event_id, state, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                event.get("revision") or 0,
                1 if event.get("_authoritative") else 0,
                event["producer_id"],
                event["event_id"],
                event.get("state"),
                event.get("received_at") or _now(),
            ),
        )

    def _write_diagnostic(self, conn, event: dict) -> None:
        key = object_key(event)
        conn.execute(
            """
            INSERT INTO facts (
                object_key, revision, event_class, authoritative, producer_id, owner_producer,
                successor_producer, event_id, state, payload_json
            ) VALUES (?, ?, 'diagnostic', ?, ?, ?, NULL, ?, NULL, ?)
            ON CONFLICT(object_key) DO UPDATE SET
                revision=excluded.revision, producer_id=excluded.producer_id, owner_producer=excluded.owner_producer,
                event_id=excluded.event_id, payload_json=excluded.payload_json, authoritative=excluded.authoritative
            """,
            (
                key,
                event.get("revision") or 0,
                1 if event.get("_authoritative") else 0,
                event["producer_id"],
                event["producer_id"],
                event["event_id"],
                json.dumps(_stored(event), sort_keys=True),
            ),
        )

    def _write_round(self, conn, event: dict) -> None:
        status = event.get("progress_status")
        if status not in STATUSES:
            return
        candidate = event.get("candidate_id") or ""
        row = conn.execute(
            "SELECT * FROM rounds WHERE task_id=? AND candidate_id=? AND run_id=?",
            (event["task_id"], candidate, event["run_id"]),
        ).fetchone()
        times = {} if row is None else json.loads(row["stage_times_json"])
        current = None if row is None else row["status"]
        blocked = 0 if row is None else row["blocked"]
        if status in STAGES and STAGES[status] not in times:
            times[STAGES[status]] = event["received_at"]
        if status == "blocked":
            blocked = 1
        if progress_rank(status) > progress_rank(current):
            current = status
        attempt = event["attempt"] if row is None else row["attempt"]
        conn.execute(
            """
            INSERT INTO rounds (task_id, candidate_id, run_id, attempt, status, stage_times_json, blocked, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'schema2')
            ON CONFLICT(task_id, candidate_id, run_id) DO UPDATE SET
                status=excluded.status, stage_times_json=excluded.stage_times_json, blocked=excluded.blocked
            """,
            (event["task_id"], candidate, event["run_id"], attempt, current, json.dumps(times), blocked),
        )

    def _write_authorization(self, conn, event: dict, *, revoked: int) -> None:
        # The current event has been inserted but not marked applied yet.
        # Verify it and the applied history inside the same transaction.
        rows = conn.execute(
            "SELECT * FROM events WHERE applied=1 OR event_id=?",
            (event["event_id"],),
        ).fetchall()
        key = (event.get("task_id"), event.get("candidate_id"), event.get("operation"))
        verdict = historical_authorization(self._verified_authorization_groups(conn, rows).get(key, []))
        if verdict.get("decision") not in {"authorized", "revoked"}:
            return
        if verdict.get("event_id") != event["event_id"] or verdict.get("revision") != event["revision"]:
            return
        revoked = int(verdict["decision"] == "revoked")
        existing = conn.execute(
            "SELECT revision, revoked FROM authorizations WHERE task_id=? AND candidate_id=? AND operation=?",
            (event["task_id"], event["candidate_id"], event["operation"]),
        ).fetchone()
        if existing is not None:
            if event["revision"] < existing["revision"]:
                return
            if event["revision"] == existing["revision"] and existing["revoked"] and not revoked:
                return
        conn.execute(
            """
            INSERT INTO authorizations (task_id, candidate_id, operation, revision, event_id, revoked, source)
            VALUES (?, ?, ?, ?, ?, ?, 'isolated')
            ON CONFLICT(task_id, candidate_id, operation) DO UPDATE SET
                revision=excluded.revision, event_id=excluded.event_id, revoked=excluded.revoked, source='isolated'
            """,
            (event["task_id"], event["candidate_id"], event["operation"], event["revision"], event["event_id"], revoked),
        )

    def _save(self, conn, event: dict, digest: str, principal: str, scope: str, result: dict, *, wrote: bool) -> dict:
        public = redact(_public(result))
        conn.execute(
            """
            INSERT INTO idempotency (principal_id, scope, operation, idempotency_key, payload_hash, result_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (principal, scope, _OPERATION, event["idempotency_key"], digest, json.dumps(public, sort_keys=True)),
        )
        public["_wrote"] = wrote
        return public

    def _fact(self, conn, key: str | None) -> dict | None:
        if not key:
            return None
        row = conn.execute("SELECT * FROM facts WHERE object_key=?", (key,)).fetchone()
        if row is None:
            return None
        return {
            "object_key": row["object_key"],
            "revision": row["revision"],
            "event_class": row["event_class"],
            "authoritative": bool(row["authoritative"]),
            "producer_id": row["producer_id"],
            "owner_producer": row["owner_producer"],
            "successor_producer": row["successor_producer"],
            "event_id": row["event_id"],
            "state": row["state"],
        }

    def _current_state(self, conn, event: dict) -> str | None:
        fact = self._fact(conn, object_key(event))
        if fact and fact.get("state"):
            return fact["state"]
        if event.get("event_class") == "task" and event.get("progress_status"):
            row = conn.execute(
                "SELECT status FROM rounds WHERE task_id=? AND run_id=?",
                (event.get("task_id"), event.get("run_id")),
            ).fetchone()
            if row is not None:
                return row["status"]
        return event.get("state")

    def _watermarks(self, conn, stream_id: str) -> dict:
        rows = conn.execute(
            """
            SELECT stream_epoch, COUNT(*) AS n, COALESCE(MAX(producer_seq), 0) AS seq,
                   COALESCE(MAX(epoch_closed), 0) AS closed
            FROM events WHERE stream_id=? AND applied=1 GROUP BY stream_epoch
            """,
            (stream_id,),
        ).fetchall()
        found = {}
        for row in rows:
            if row["n"] != row["seq"]:
                raise ControlError("EVIDENCE_MISSING", 409, "applied prefix is not contiguous")
            found[(stream_id, row["stream_epoch"])] = {"seq": row["seq"], "closed": bool(row["closed"])}
        return found

    def _watermark(self, conn, stream_id: str, epoch: int) -> dict:
        return self._watermarks(conn, stream_id).get((stream_id, epoch), {"seq": 0, "closed": False})

    def _seals(self, conn, stream_id: str) -> set:
        rows = conn.execute("SELECT stream_epoch FROM seals WHERE stream_id=?", (stream_id,)).fetchall()
        return {(stream_id, row["stream_epoch"]) for row in rows}

    def _has_later(self, conn, stream_id: str, epoch: int, seq: int) -> bool:
        row = conn.execute(
            "SELECT 1 FROM events WHERE stream_id=? AND stream_epoch=? AND producer_seq>?",
            (stream_id, epoch, seq),
        ).fetchone()
        return row is not None

    def _has_unapplied(self, conn, stream_id: str, epoch: int) -> bool:
        row = conn.execute(
            "SELECT 1 FROM events WHERE stream_id=? AND stream_epoch=? AND applied=0",
            (stream_id, epoch),
        ).fetchone()
        return row is not None

    def _items(self, conn, stream_id: str, epoch: int, after_seq: int, *, event_class: str | None, subject: str | None, limit: int, allowed_tasks: set[str]):
        rows = conn.execute(
            """
            SELECT payload_json FROM events
            WHERE stream_id=? AND stream_epoch=? AND applied=1 AND producer_seq>?
            ORDER BY producer_seq
            """,
            (stream_id, epoch, after_seq),
        ).fetchall()
        items = []
        truncated = False
        for row in rows:
            payload = redact(json.loads(row["payload_json"]))
            if event_class is not None and payload.get("event_class") != event_class:
                continue
            if subject is not None and payload.get("subject_id") != subject:
                continue
            task_id = payload.get("task_id")
            if task_id and task_id not in allowed_tasks:
                continue
            subject_id = payload.get("subject_id")
            if isinstance(subject_id, str) and subject_id.startswith("tsk_") and subject_id not in allowed_tasks:
                continue
            payload.pop("_authoritative", None)
            if len(items) >= limit:
                truncated = True
                break
            items.append(payload)
        next_after = items[-1]["producer_seq"] if truncated and items else None
        return items, next_after, truncated


def _wins(current: dict, event: dict) -> bool:
    if not event.get("_authoritative"):
        return False
    revision = event.get("revision") or 0
    if revision < current["revision"]:
        return False
    if revision == current["revision"]:
        return not current["authoritative"]
    owner = current.get("owner_producer") or current.get("producer_id")
    allowed = {owner}
    if current.get("successor_producer"):
        allowed.add(current["successor_producer"])
    if event.get("producer_id") not in allowed:
        return False
    return True


def _text_field(resource: dict, name: str) -> str:
    value = resource.get(name)
    return value if isinstance(value, str) else ""


def _historical_groups(rows) -> dict:
    grouped = {}
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("event_type") not in {"authorization.granted", "authorization.revoked"}:
            continue
        try:
            event = _rehydrate(payload)
        except ControlError:
            continue
        if not event.get("_authoritative"):
            continue
        key = (event.get("task_id"), event.get("candidate_id"), event.get("operation"))
        grouped.setdefault(key, []).append(event)
    return grouped


def _rehydrate(payload: dict) -> dict:
    event = dict(payload)
    source = event.get("trusted_source") or None
    event["_authoritative"] = authorize_source(event, source)
    return event


def _stored(event: dict) -> dict:
    visible = {key: value for key, value in event.items() if not str(key).startswith("_") and key != "guard"}
    return redact(visible)


def _page_limit(limit: int) -> int:
    if type(limit) is not int or limit < 1 or limit > 100:
        raise ControlError("SCHEMA_UNSUPPORTED", 422, "page limit is invalid")
    return limit


def _public(result: dict) -> dict:
    return {key: value for key, value in result.items() if not key.startswith("_")}


def _error(code: str, http: int, message: str) -> dict:
    return {
        "schema_version": 2,
        "http": http,
        "code": code,
        "message": message,
        "lease_token": None,
        "replayed": False,
        "applied": False,
        "projected": False,
        "side_effect": False,
        "production_protocol_enabled": False,
    }


def _replay(stored: dict, current_state: str | None) -> dict:
    original = {key: value for key, value in stored.items() if key not in {"original_result", "current_state", "replayed"}}
    result = dict(stored)
    result["replayed"] = True
    result["lease_token"] = None
    result["original_result"] = original
    result["current_state"] = current_state if current_state is not None else stored.get("current_state")
    if result.get("http", 200) == 200 and result.get("code") in (None, "IDEMPOTENCY_REPLAY"):
        result["code"] = "IDEMPOTENCY_REPLAY"
        result["http"] = 200
    return result
