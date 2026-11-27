"""Isolated schema 2 event engine.

The store, inbox, outbox, and reducer live here. This process does not mount
production routes, does not enable the new protocol in production, and does
not treat legacy records as authorization.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from app.feedback_progress import STAGES, STATUSES

from .errors import ControlError
from .identity import PRODUCTION_PROTOCOL_ENABLED, RepositoryIdentity
from .inbox import decide
from .legacy import parse_comments, read_operations
from .outbox import DEFAULT_CHANNELS, OutboxLimits, ReportOutbox
from .redact import redact
from .reducer import check_transition, object_key, prefer_fact, progress_rank
from .schema import authorize_source, channel_of, split_request
from .store import Store

_REVISION_CLASSES = {"task", "candidate", "deployment", "resource", "authorization"}
_OPERATION = "event_append"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(event: dict) -> str:
    encoded = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _scope_key(scope: dict) -> str:
    return json.dumps(scope, sort_keys=True, separators=(",", ":"))


class Engine:
    def __init__(self, root: Path, identity: RepositoryIdentity, store: Store, outbox: ReportOutbox, authorizer=None):
        self.root = root
        self.identity = identity
        self.store = store
        self.outbox = outbox
        self.authorizer = authorizer
        self.isolated = True
        self.production_protocol_enabled = False

    @classmethod
    def open(cls, root: Path, bound_token: str, *, limits: OutboxLimits | None = None, authorizer=None, production: bool = False) -> "Engine":
        if production or PRODUCTION_PROTOCOL_ENABLED:
            raise ControlError("NOT_AUTHORIZED", 403, "unverified schema 2 protocol cannot be enabled in production")
        identity = RepositoryIdentity.open(root, bound_token)
        store = Store(root / "events.sqlite3")
        engine = cls(root, identity, store, ReportOutbox(root, store, limits), authorizer)
        engine.outbox.recover()
        return engine

    @property
    def repo_id(self) -> str:
        return self.identity.repo_id

    def enable_production_protocol(self) -> None:
        raise ControlError("NOT_AUTHORIZED", 403, "unverified schema 2 protocol cannot be enabled in production")

    def append(self, body: dict, *, crash: str | None = None) -> dict:
        self._authenticate(body)
        event = split_request(body)
        self.identity.assert_same(event["repo_id"])
        if event.get("state") == "UNKNOWN":
            raise ControlError("NOT_AUTHORIZED", 403, "a caller cannot write UNKNOWN")
        event["_authoritative"] = authorize_source(event)
        if self.authorizer is not None and self.authorizer(event) is False:
            raise ControlError("POLICY_DENIED", 403, "policy denied before idempotency")
        event = redact(event)
        digest = _hash(event)
        principal = body["actor"]["id"]
        scope = _scope_key(body["scope"])
        with self.store.transaction() as conn:
            existing = conn.execute(
                "SELECT payload_hash, result_json FROM idempotency WHERE principal_id=? AND scope=? AND operation=? AND idempotency_key=?",
                (principal, scope, _OPERATION, event["idempotency_key"]),
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] != digest:
                    outcome = _error("IDEMPOTENCY_PAYLOAD_MISMATCH", 409, "idempotency key was already used with a different payload")
                else:
                    outcome = _replay(json.loads(existing["result_json"]), self._current_state(conn, event))
            else:
                outcome = self._attempt(conn, event, digest, principal, scope)
            if crash == "before-commit":
                os._exit(90)
        return redact(_public(outcome))

    def submit_report(self, report: dict, channels: list[str] | None = None, remote=None, *, crash: str | None = None) -> dict:
        if self.production_protocol_enabled:
            raise ControlError("NOT_AUTHORIZED", 403, "production protocol is disabled")
        chosen = list(channels or DEFAULT_CHANNELS)
        return self.outbox.submit(report, chosen, remote, crash=crash)

    def deliver_pending(self, remote) -> list[dict]:
        return self.outbox.deliver_pending(remote)

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

    def query(self, *, stream_id: str, stream_epoch: int, after_seq: int = 0) -> dict:
        with self.store.transaction() as conn:
            watermark = self._watermark(conn, stream_id, stream_epoch)["seq"]
            items = self._items(conn, stream_id, stream_epoch, after_seq, event_class=None, subject=None)
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
            "lease_token": None,
        }

    def poll_diagnostics(self, *, subject: str, stream_id: str, stream_epoch: int, after_seq: int) -> dict:
        if type(after_seq) is not int or after_seq < 0 or type(stream_epoch) is not int:
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "diagnostic cursor is invalid")
        if not isinstance(stream_id, str) or not stream_id.endswith(":diagnostic"):
            return {
                "schema_version": 2,
                "http": 200,
                "side_effect": False,
                "business_side_effect": False,
                "stream_id": stream_id,
                "stream_epoch": stream_epoch,
                "watermark_seq": 0,
                "gap": False,
                "items": [],
                "cursor_after_seq": None,
                "lease_token": None,
            }
        with self.store.transaction() as conn:
            watermark = self._watermark(conn, stream_id, stream_epoch)["seq"]
            gap = after_seq > watermark or self._has_unapplied(conn, stream_id, stream_epoch)
            items = self._items(conn, stream_id, stream_epoch, after_seq, event_class="diagnostic", subject=subject)
            stored = conn.execute(
                "SELECT after_seq FROM cursors WHERE stream_id=? AND stream_epoch=?",
                (stream_id, stream_epoch),
            ).fetchone()
            current = 0 if stored is None else stored["after_seq"]
            if after_seq <= watermark and after_seq >= current:
                conn.execute(
                    """
                    INSERT INTO cursors (stream_id, stream_epoch, after_seq) VALUES (?, ?, ?)
                    ON CONFLICT(stream_id, stream_epoch) DO UPDATE SET after_seq=excluded.after_seq
                    """,
                    (stream_id, stream_epoch, after_seq),
                )
                current = after_seq
        return {
            "schema_version": 2,
            "http": 200,
            "side_effect": False,
            "business_side_effect": False,
            "subject": subject,
            "stream_id": stream_id,
            "stream_epoch": stream_epoch,
            "watermark_seq": watermark,
            "gap": gap,
            "items": items,
            "cursor_after_seq": current,
            "lease_token": None,
        }

    def cursor(self, stream_id: str, stream_epoch: int) -> int | None:
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT after_seq FROM cursors WHERE stream_id=? AND stream_epoch=?",
                (stream_id, stream_epoch),
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
        return [
            {
                "task_id": row["task_id"],
                "candidate_id": row["candidate_id"],
                "operation": row["operation"],
                "revision": row["revision"],
                "event_id": row["event_id"],
                "revoked": bool(row["revoked"]),
                "source": row["source"],
                "production_effective": False,
            }
            for row in rows
        ]

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
        return snapshot

    def _authenticate(self, body: dict) -> None:
        if not self.isolated or self.production_protocol_enabled:
            raise ControlError("NOT_AUTHORIZED", 403, "isolated test identity is required")
        actor = body.get("actor") if isinstance(body, dict) else None
        if not isinstance(actor, dict) or actor.get("kind") != "test" or not isinstance(actor.get("id"), str):
            raise ControlError("NOT_AUTHORIZED", 403, "test identity was not accepted")
        proof = actor.get("proof_ref")
        if not isinstance(proof, str) or not proof.startswith("test-proof://"):
            raise ControlError("NOT_AUTHORIZED", 403, "test proof is not accepted on this route")
        if body.get("production_verification_enabled") is True:
            raise ControlError("NOT_AUTHORIZED", 403, "production verification is disabled")

    def _attempt(self, conn, event: dict, digest: str, principal: str, scope: str) -> dict:
        received_at = _now()
        event["received_at"] = received_at
        watermarks = self._watermarks(conn, event["stream_id"])
        seals = self._seals(conn, event["stream_id"])
        later = self._has_later(conn, event["stream_id"], event["stream_epoch"], event["producer_seq"])
        action, reason = decide(event, watermarks, seals, later=later)
        if action == "reject":
            return self._save(conn, event, digest, principal, scope, _error(reason or "EVIDENCE_MISSING", 409, "stream proof was rejected"), wrote=False)
        if action == "apply":
            failure = self._precheck(conn, event)
            if failure is not None:
                return self._save(conn, event, digest, principal, scope, failure, wrote=False)
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
        conn.execute(
            """
            INSERT INTO events (
                event_id, stream_id, stream_epoch, producer_seq, payload_hash, payload_json,
                received_at, applied, hold_reason, epoch_closed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 0)
            """,
            (
                event["event_id"],
                event["stream_id"],
                event["stream_epoch"],
                event["producer_seq"],
                digest,
                json.dumps(event, sort_keys=True),
                received_at,
                reason,
            ),
        )
        for name in event.get("delivery_channels") or [channel_of(event)]:
            conn.execute(
                """
                INSERT INTO deliveries (event_id, channel, state, detail, updated_at, run_id)
                VALUES (?, ?, 'DURABLE_LOCAL', NULL, ?, ?)
                """,
                (event["event_id"], name, received_at, event.get("run_id")),
            )
        self._drain(conn, event["stream_id"])
        row = conn.execute("SELECT applied, hold_reason FROM events WHERE event_id=?", (event["event_id"],)).fetchone()
        applied = bool(row["applied"])
        result = {
            "schema_version": 2,
            "http": 200,
            "fact_state": "ACCEPTED" if applied else None,
            "delivery_state": "DURABLE_LOCAL",
            "applied": applied,
            "hold_reason": row["hold_reason"],
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
                event = json.loads(row["payload_json"])
                later = self._has_later(conn, stream_id, event["stream_epoch"], event["producer_seq"])
                action, reason = decide(event, watermarks, seals, later=later)
                if action != "apply":
                    if row["hold_reason"] != reason:
                        conn.execute("UPDATE events SET hold_reason=? WHERE event_id=?", (reason, event["event_id"]))
                    continue
                if self._precheck(conn, event) is not None:
                    conn.execute("UPDATE events SET hold_reason=? WHERE event_id=?", ("CONFLICT_REVISION", event["event_id"]))
                    continue
                self._reduce(conn, event)
                conn.execute(
                    "UPDATE events SET applied=1, hold_reason=NULL, epoch_closed=? WHERE event_id=?",
                    (1 if event.get("epoch_closed") else 0, event["event_id"]),
                )
                progressed = True
                break
            if not progressed:
                return

    def _precheck(self, conn, event: dict) -> dict | None:
        if event["event_class"] not in _REVISION_CLASSES:
            return None
        key = object_key(event)
        current = self._fact(conn, key)
        expected = event.get("expected_revision")
        revision = event.get("revision")
        try:
            if current is None:
                if expected not in (0, revision):
                    return _error("CONFLICT_REVISION", 409, "expected_revision does not match the stored revision")
                check_transition(event["event_class"], None, event.get("state"), event.get("guard"))
            elif expected != current["revision"] or (revision is not None and revision < current["revision"]):
                return _error("CONFLICT_REVISION", 409, "expected_revision does not match the stored revision")
            elif revision is not None and revision > current["revision"]:
                check_transition(event["event_class"], current["state"], event.get("state"), event.get("guard"))
        except ControlError as exc:
            return exc.public()
        return None

    def _reduce(self, conn, event: dict) -> None:
        if event.get("classification") == "never_existed":
            return
        if event["event_class"] == "diagnostic":
            self._write_diagnostic(conn, event)
            return
        if event["event_class"] in _REVISION_CLASSES:
            self._write_fact(conn, event)
        if event["event_class"] == "task":
            self._write_round(conn, event)
        if event["event_type"] == "authorization.granted":
            self._write_authorization(conn, event, revoked=0)
        elif event["event_type"] == "authorization.revoked":
            self._write_authorization(conn, event, revoked=1)

    def _write_fact(self, conn, event: dict) -> None:
        key = object_key(event)
        current = self._fact(conn, key)
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
            INSERT INTO facts (object_key, revision, event_class, authoritative, producer_id, event_id, state, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(object_key) DO UPDATE SET
                revision=excluded.revision, event_class=excluded.event_class, authoritative=excluded.authoritative,
                producer_id=excluded.producer_id, event_id=excluded.event_id, state=excluded.state,
                payload_json=excluded.payload_json
            """,
            (
                key,
                incoming["revision"],
                incoming["event_class"],
                1 if incoming["authoritative"] else 0,
                incoming["producer_id"],
                incoming["event_id"],
                incoming["state"],
                json.dumps(redact(event), sort_keys=True),
            ),
        )

    def _write_diagnostic(self, conn, event: dict) -> None:
        # Diagnostic facts use their own key. They never update authorization, deployment, or candidate rows.
        key = object_key(event)
        conn.execute(
            """
            INSERT INTO facts (object_key, revision, event_class, authoritative, producer_id, event_id, state, payload_json)
            VALUES (?, ?, 'diagnostic', ?, ?, ?, NULL, ?)
            ON CONFLICT(object_key) DO UPDATE SET
                revision=excluded.revision, producer_id=excluded.producer_id, event_id=excluded.event_id,
                payload_json=excluded.payload_json, authoritative=excluded.authoritative
            """,
            (
                key,
                event.get("revision") or 0,
                1 if event.get("_authoritative") else 0,
                event["producer_id"],
                event["event_id"],
                json.dumps(redact(event), sort_keys=True),
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

    def _items(self, conn, stream_id: str, epoch: int, after_seq: int, *, event_class: str | None, subject: str | None) -> list[dict]:
        rows = conn.execute(
            """
            SELECT payload_json FROM events
            WHERE stream_id=? AND stream_epoch=? AND applied=1 AND producer_seq>?
            ORDER BY producer_seq
            """,
            (stream_id, epoch, after_seq),
        ).fetchall()
        items = []
        for row in rows:
            payload = redact(json.loads(row["payload_json"]))
            if event_class is not None and payload.get("event_class") != event_class:
                continue
            if subject is not None and payload.get("subject_id") != subject:
                continue
            payload.pop("_authoritative", None)
            items.append(payload)
        return items


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
