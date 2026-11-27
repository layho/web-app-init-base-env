"""Generic event outbox. Isolated from production delivery.

A written event is queued in the same SQLite transaction as the event.
A channel is DELIVERED only after an exact receipt: event, channel, payload
hash, stream, epoch, and producer sequence. A claim updates only the state
the worker observed. An unknown result is reconciled before another send.
Capacity rejects the write instead of growing without a bound. This consumer
does not call GitHub, the MacCI runner, or a production route.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
import uuid

from .atomic import atomic_write
from .errors import BadRecord, ControlError, DeliveryUnknown

DEFAULT_EVENT_CHANNELS = ("event_sink",)
_CLAIM_TTL_SECONDS = 30
_EXACT = ("event_id", "channel", "payload_hash", "stream_id", "stream_epoch", "producer_seq")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_channel(channel: object) -> str:
    if type(channel) is not str or not channel.isidentifier() or channel.startswith("_") or len(channel) > 64:
        raise ControlError("SCHEMA_UNSUPPORTED", 422, "event delivery channel is invalid")
    return channel


def exact_confirmation(channel: str, envelope: dict) -> dict:
    return {
        "event_id": envelope.get("event_id"),
        "channel": channel,
        "payload_hash": envelope.get("payload_hash"),
        "stream_id": envelope.get("stream_id"),
        "stream_epoch": envelope.get("stream_epoch"),
        "producer_seq": envelope.get("producer_seq"),
    }


def confirmation_binds(result: object, channel: str, envelope: dict) -> bool:
    if not isinstance(result, dict):
        return False
    expected = exact_confirmation(channel, envelope)
    for key in _EXACT:
        value = expected[key]
        if value in (None, "") or result.get(key) != value:
            return False
    return True


def _reconcile(remote, channel: str, envelope: dict) -> str:
    probe = getattr(remote, "reconcile", None)
    if probe is None:
        return "unknown"
    verdict = probe(channel, envelope)
    if verdict == "absent":
        return "absent"
    if isinstance(verdict, dict) and confirmation_binds(verdict, channel, envelope):
        return "delivered"
    return "unknown"


def _pid_alive(pid: object) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _claim_is_active(pid: object, expires_at: object, now: int) -> bool:
    if isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at <= now:
        return False
    return _pid_alive(pid)


class EventOutbox:
    def __init__(self, root: Path, store, limits, channels: tuple[str, ...] | None = None):
        self.root = root
        self.store = store
        self.limits = limits
        chosen = tuple(_safe_channel(channel) for channel in (channels or DEFAULT_EVENT_CHANNELS))
        if not chosen or len(set(chosen)) != len(chosen):
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "event delivery channel is invalid")
        self.channels = chosen
        self.receipts = root / "event-receipts"
        self.quarantine = root / "event-quarantine"
        self.receipts.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.quarantine.mkdir(parents=True, mode=0o700, exist_ok=True)

    def enqueue(self, conn, event: dict, digest: str) -> None:
        envelope = _envelope(event, digest, self.channels[0])
        size = len(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode())
        pending = conn.execute(
            """
            SELECT COUNT(*) AS n, COALESCE(SUM(payload_bytes), 0) AS b
            FROM event_outbox WHERE state IN ('DURABLE_LOCAL', 'OUTBOX', 'DELIVERY_UNKNOWN')
            """
        ).fetchone()
        added = len(self.channels)
        if pending["n"] + added > self.limits.max_event_outbox or pending["b"] + size * added > self.limits.max_event_outbox_bytes:
            raise ControlError("CAPACITY_EXHAUSTED", 409, "event outbox capacity is exhausted")
        moment = _now()
        for channel in self.channels:
            conn.execute(
                """
                INSERT INTO event_outbox (
                    event_id, channel, payload_hash, stream_id, stream_epoch, producer_seq,
                    state, detail, updated_at, pending_since, claim_token, claim_pid,
                    claim_expires_at, payload_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, 'DURABLE_LOCAL', NULL, ?, ?, NULL, NULL, NULL, ?)
                """,
                (
                    event["event_id"], channel, digest, event["stream_id"], event["stream_epoch"],
                    event["producer_seq"], moment, moment, size,
                ),
            )

    def deliver_pending(self, remote, *, crash: str | None = None) -> list[dict]:
        self.recover(abandon_claims=False)
        receipts = []
        with self.store.transaction() as conn:
            rows = conn.execute(
                """
                SELECT event_id, channel FROM event_outbox
                WHERE state IN ('DURABLE_LOCAL', 'OUTBOX', 'DELIVERY_UNKNOWN')
                ORDER BY pending_since, event_id, channel
                """
            ).fetchall()
        seen = {(row["event_id"], row["channel"]) for row in rows}
        for event_id, channel in sorted(seen):
            envelope = self._load(event_id, channel)
            if envelope is None:
                continue
            receipts.append(self._dispatch(envelope, channel, remote, crash=crash))
        return receipts

    def recover(self, *, abandon_claims: bool = False) -> int:
        now = int(time.time())
        moved = 0
        with self.store.transaction() as conn:
            rows = conn.execute(
                """
                SELECT event_id, channel, claim_token, claim_pid, claim_expires_at
                FROM event_outbox WHERE state='OUTBOX'
                """
            ).fetchall()
            for row in rows:
                if _claim_is_active(row["claim_pid"], row["claim_expires_at"], now):
                    continue
                if row["claim_token"] is None and not abandon_claims:
                    continue
                cursor = conn.execute(
                    """
                    UPDATE event_outbox
                    SET state='DELIVERY_UNKNOWN', claim_token=NULL, claim_pid=NULL,
                        claim_expires_at=NULL, detail='claim holder is gone', updated_at=?
                    WHERE event_id=? AND channel=? AND state='OUTBOX' AND claim_token IS ?
                    """,
                    (_now(), row["event_id"], row["channel"], row["claim_token"]),
                )
                moved += cursor.rowcount
        return moved

    def health(self) -> dict:
        with self.store.transaction() as conn:
            backlog = conn.execute(
                "SELECT COUNT(*) AS n FROM event_outbox WHERE state IN ('DURABLE_LOCAL', 'OUTBOX', 'DELIVERY_UNKNOWN')"
            ).fetchone()["n"]
            oldest = conn.execute(
                "SELECT MIN(pending_since) AS at FROM event_outbox WHERE state IN ('DURABLE_LOCAL', 'OUTBOX', 'DELIVERY_UNKNOWN')"
            ).fetchone()["at"]
            by_state = conn.execute("SELECT channel, state, COUNT(*) AS n FROM event_outbox GROUP BY channel, state").fetchall()
            used = conn.execute("SELECT COALESCE(SUM(payload_bytes), 0) AS b FROM event_outbox WHERE state IN ('DURABLE_LOCAL', 'OUTBOX', 'DELIVERY_UNKNOWN')").fetchone()["b"]
        channels: dict[str, dict[str, int]] = {}
        for row in by_state:
            channels.setdefault(row["channel"], {})[row["state"]] = row["n"]
        disposition = "idle" if backlog == 0 else "DURABLE_LOCAL"
        return {
            "event_backlog": backlog,
            "event_oldest_pending_at": oldest,
            "event_channels": channels,
            "event_outbox_bytes": used,
            "event_outbox_limit": self.limits.max_event_outbox,
            "event_outbox_remaining": max(0, self.limits.max_event_outbox - backlog),
            "event_delivery_disposition": disposition,
            "event_outbox_consumer": "generic",
        }

    def _dispatch(self, envelope: dict, channel: str, remote, *, crash: str | None) -> dict:
        observed = envelope["state"]
        for _ in range(3):
            current = observed
            if current in {"DELIVERED", "QUARANTINED"}:
                found = self._latest_receipt(envelope["event_id"], channel, current)
                return found if found is not None else self._pending(envelope, channel, current)
            if remote is None:
                return self._pending(envelope, channel, current)
            if current == "OUTBOX":
                return self._pending(envelope, channel, "OUTBOX")
            if current == "DELIVERY_UNKNOWN":
                finished = self._finish_unknown(envelope, channel, remote, crash=crash)
                if finished is not None:
                    return finished
                observed = self._state(envelope["event_id"], channel) or "OUTBOX"
                if observed == "DELIVERY_UNKNOWN":
                    return self._pending(envelope, channel, "DELIVERY_UNKNOWN")
                continue
            if current != "DURABLE_LOCAL":
                fresh = self._state(envelope["event_id"], channel) or current
                if fresh != current:
                    observed = fresh
                    continue
                return self._pending(envelope, channel, current)
            token = self._claim(envelope["event_id"], channel, "DURABLE_LOCAL")
            if token:
                return self._send(envelope, channel, remote, token, crash=crash)
            observed = self._state(envelope["event_id"], channel) or "OUTBOX"
            if observed == "DURABLE_LOCAL":
                found = self._latest_receipt(envelope["event_id"], channel, "DURABLE_LOCAL")
                return found if found is not None else self._pending(envelope, channel, "DURABLE_LOCAL")
            continue
        fresh = self._state(envelope["event_id"], channel) or "OUTBOX"
        found = self._latest_receipt(envelope["event_id"], channel, fresh)
        return found if found is not None else self._pending(envelope, channel, fresh)

    def _finish_unknown(self, envelope: dict, channel: str, remote, *, crash: str | None) -> dict | None:
        """Reconcile an unknown row. None means the expected state was no longer current."""

        if self._local_receipt_binds(envelope, channel):
            token = self._claim(envelope["event_id"], channel, "DELIVERY_UNKNOWN")
            if not token:
                return None
            self._set_state(envelope["event_id"], channel, "DELIVERED", None, token)
            found = self._latest_receipt(envelope["event_id"], channel, "DELIVERED")
            return found if found is not None else self._pending(envelope, channel, "DELIVERY_UNKNOWN")
        token = self._claim(envelope["event_id"], channel, "DELIVERY_UNKNOWN")
        if not token:
            return None
        verdict = _reconcile(remote, channel, envelope)
        if verdict == "delivered":
            receipt = self._write_receipt(envelope, channel, "DELIVERED")
            self._set_state(envelope["event_id"], channel, "DELIVERED", None, token)
            return receipt
        if verdict != "absent":
            self._set_state(envelope["event_id"], channel, "DELIVERY_UNKNOWN", "delivery outcome is unknown", token)
            found = self._latest_receipt(envelope["event_id"], channel, "DELIVERY_UNKNOWN")
            return found if found is not None else self._write_receipt(envelope, channel, "DELIVERY_UNKNOWN")
        return self._send(envelope, channel, remote, token, crash=crash)

    def _send(self, envelope: dict, channel: str, remote, token: str, *, crash: str | None) -> dict:
        state = "DELIVERY_UNKNOWN"
        detail = "remote returned no exact confirmation"
        try:
            result = remote(channel, envelope)
            if crash == "after-event-remote":
                os._exit(95)
            if confirmation_binds(result, channel, envelope):
                state = "DELIVERED"
                detail = None
        except BadRecord as exc:
            state = "QUARANTINED"
            detail = exc.message
            self._isolate(channel, envelope, exc.message)
        except (DeliveryUnknown, OSError, TimeoutError):
            state = "DELIVERY_UNKNOWN"
            detail = "delivery outcome is unknown"
        except Exception:
            state = "DELIVERY_UNKNOWN"
            detail = "delivery outcome is unknown"
        receipt = self._write_receipt(envelope, channel, state)
        self._set_state(envelope["event_id"], channel, state, detail, token)
        return receipt

    def _claim(self, event_id: str, channel: str, expected: str) -> str | None:
        if expected not in {"DURABLE_LOCAL", "DELIVERY_UNKNOWN"}:
            return None
        token = uuid.uuid4().hex
        now = int(time.time())
        with self.store.transaction() as conn:
            before = conn.total_changes
            conn.execute(
                """
                UPDATE event_outbox
                SET state='OUTBOX', detail=?, updated_at=?, claim_token=?, claim_pid=?, claim_expires_at=?
                WHERE event_id=? AND channel=? AND state=?
                """,
                (token, _now(), token, os.getpid(), now + _CLAIM_TTL_SECONDS, event_id, channel, expected),
            )
            if conn.total_changes == before:
                return None
        return token

    def _set_state(self, event_id: str, channel: str, state: str, detail: str | None, claim_token: str) -> bool:
        with self.store.transaction() as conn:
            current = conn.execute(
                "SELECT state, claim_token FROM event_outbox WHERE event_id=? AND channel=?",
                (event_id, channel),
            ).fetchone()
            if current and current["state"] in {"DELIVERED", "QUARANTINED"}:
                return current["state"] == state
            if current is None or current["claim_token"] != claim_token:
                return False
            cursor = conn.execute(
                """
                UPDATE event_outbox
                SET state=?, detail=?, updated_at=?, claim_token=NULL, claim_pid=NULL, claim_expires_at=NULL
                WHERE event_id=? AND channel=? AND claim_token=?
                """,
                (state, detail, _now(), event_id, channel, claim_token),
            )
            return cursor.rowcount == 1

    def _load(self, event_id: str, channel: str) -> dict | None:
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM event_outbox WHERE event_id=? AND channel=?", (event_id, channel)).fetchone()
        if row is None:
            return None
        return _envelope_from_row(row)

    def _state(self, event_id: str, channel: str) -> str | None:
        loaded = self._load(event_id, channel)
        return None if loaded is None else loaded["state"]

    def _local_receipt_binds(self, envelope: dict, channel: str) -> bool:
        receipt = self._latest_receipt(envelope["event_id"], channel, "DELIVERED")
        return confirmation_binds(receipt, channel, envelope) if receipt is not None else False

    def _latest_receipt(self, event_id: str, channel: str, state: str) -> dict | None:
        with self.store.transaction() as conn:
            row = conn.execute(
                """
                SELECT body_json FROM receipts
                WHERE event_id=? AND channel=? AND state=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (event_id, channel, state),
            ).fetchone()
        return None if row is None else json.loads(row["body_json"])

    def _write_receipt(self, envelope: dict, channel: str, state: str) -> dict:
        receipt = self._receipt_body(envelope, channel, state, receipt_id="ercpt_" + uuid.uuid4().hex, persisted=True)
        atomic_write(self.receipts / f"{receipt['receipt_id']}.json", json.dumps(receipt, sort_keys=True).encode())
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO receipts (receipt_id, channel, run_id, event_id, state, body_json, created_at)
                VALUES (?, ?, NULL, ?, ?, ?, ?)
                """,
                (receipt["receipt_id"], channel, envelope["event_id"], state, json.dumps(receipt, sort_keys=True), receipt["created_at"]),
            )
        return receipt

    def _pending(self, envelope: dict, channel: str, state: str) -> dict:
        return self._receipt_body(envelope, channel, state, receipt_id=None, persisted=False)

    def _receipt_body(self, envelope: dict, channel: str, state: str, *, receipt_id: str | None, persisted: bool) -> dict:
        body = exact_confirmation(channel, envelope)
        body.update({
            "schema_version": 2,
            "receipt_id": receipt_id,
            "state": state,
            "complete": state == "DELIVERED",
            "persisted": persisted,
            "exactly_once": False,
            "exact": True,
            "lease_token": None,
            "created_at": _now(),
        })
        return body

    def _isolate(self, channel: str, envelope: dict, reason: str) -> None:
        target = self.quarantine / channel
        target.mkdir(parents=True, mode=0o700, exist_ok=True)
        payload = json.dumps({"event_id": envelope["event_id"], "channel": channel, "reason": reason, "isolated": True}).encode()
        atomic_write(target / f"{envelope['event_id']}.json", payload)


def _envelope(event: dict, digest: str, channel: str) -> dict:
    return {
        "event_id": event["event_id"],
        "channel": channel,
        "payload_hash": digest,
        "stream_id": event["stream_id"],
        "stream_epoch": event["stream_epoch"],
        "producer_seq": event["producer_seq"],
        "state": "DURABLE_LOCAL",
    }


def _envelope_from_row(row) -> dict:
    return {
        "event_id": row["event_id"],
        "channel": row["channel"],
        "payload_hash": row["payload_hash"],
        "stream_id": row["stream_id"],
        "stream_epoch": row["stream_epoch"],
        "producer_seq": row["producer_seq"],
        "state": row["state"],
    }
