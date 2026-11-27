"""Report outbox. A channel is DELIVERED only after a trusted remote confirmation.

Claims are conditional updates, so two processes cannot both send the same
channel. An unknown result is reconciled before another send. Retrying is not
treated as exactly-once. Event-log rows are not report deliveries; the engine
marks those as not yet delivered instead of handing them to this consumer.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from .atomic import atomic_write
from .errors import BadRecord, ControlError, DeliveryUnknown
from .redact import redact

FAILURE_PHASES = {
    "setup-failed",
    "fetch-failed",
    "context-failed",
    "dependencies-failed",
    "pytest-failed",
    "compile-failed",
    "frontend-failed",
    "test-failed",
    "ref-changed",
}
REQUIRED = (
    "repository",
    "pr",
    "run_id",
    "head",
    "base",
    "merge",
    "tree",
    "phase",
    "outcome",
    "executor_id",
    "evidence_ref",
)
DEFAULT_CHANNELS = ("github_comment", "commit_status")
_SHA = set("0123456789abcdef")
_CLAIM_TTL_SECONDS = 30
_CANDIDATE_FIELDS = ("head", "base", "merge", "tree", "executor_id")


@dataclass(frozen=True)
class OutboxLimits:
    max_report_bytes: int = 12000
    max_outbox_reports: int = 100
    max_outbox_bytes: int = 1_200_000
    max_quarantine_reports: int = 20
    max_quarantine_bytes: int = 1_200_000
    max_event_outbox: int = 1000
    max_event_outbox_bytes: int = 5_000_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(value: object, field: str) -> None:
    if not isinstance(value, str) or len(value) != 40 or any(char not in _SHA for char in value):
        raise ControlError("SCHEMA_UNSUPPORTED", 422, f"report {field} is incomplete")


def _safe_channel(channel: object) -> str:
    if type(channel) is not str or not channel.isidentifier() or channel.startswith("_") or len(channel) > 64:
        raise ControlError("SCHEMA_UNSUPPORTED", 422, "delivery channel is invalid")
    return channel


def validate_report(report: dict, trusted_executors: set[str] | None = None) -> None:
    if not isinstance(report, dict):
        raise ControlError("SCHEMA_UNSUPPORTED", 422, "report must be an object")
    missing = [field for field in REQUIRED if report.get(field) in (None, "")]
    if missing:
        raise ControlError("SCHEMA_UNSUPPORTED", 422, "report is incomplete")
    if type(report["pr"]) is not int or report["pr"] <= 0:
        raise ControlError("SCHEMA_UNSUPPORTED", 422, "report pr is invalid")
    if not isinstance(report["run_id"], str) or len(report["run_id"]) != 32 or any(ch not in _SHA for ch in report["run_id"]):
        raise ControlError("SCHEMA_UNSUPPORTED", 422, "report run_id is invalid")
    for field in ("head", "base", "merge", "tree"):
        _sha(report[field], field)
    evidence = report["evidence_ref"]
    if not isinstance(evidence, str) or evidence.startswith(("/", "\\")) or ".." in evidence:
        raise ControlError("SCHEMA_UNSUPPORTED", 422, "report evidence is incomplete")
    allowed = trusted_executors if trusted_executors is not None else set()
    if report["executor_id"] not in allowed:
        raise ControlError("UNKNOWN_EXECUTOR", 409, "report executor is not trusted")
    outcome = report["outcome"]
    phase = report["phase"]
    if outcome == "success" and phase != "passed":
        raise ControlError("SCHEMA_UNSUPPORTED", 422, "success report is incomplete")
    if outcome == "failure" and (phase == "passed" or phase not in FAILURE_PHASES):
        raise ControlError("SCHEMA_UNSUPPORTED", 422, "failure report is incomplete")
    if outcome not in {"success", "failure"}:
        raise ControlError("SCHEMA_UNSUPPORTED", 422, "report outcome is incomplete")


def bound_confirmation(channel: str, report: dict) -> dict:
    """Remote success must name the run, the channel, and the full candidate."""

    return {
        "run_id": report.get("run_id"),
        "channel": channel,
        "head": report.get("head"),
        "base": report.get("base"),
        "merge": report.get("merge"),
        "tree": report.get("tree"),
        "executor_id": report.get("executor_id"),
    }


def confirmation_binds(result: object, channel: str, report: dict) -> bool:
    if not isinstance(result, dict):
        return False
    expected = bound_confirmation(channel, report)
    for key, value in expected.items():
        if not isinstance(value, str) or value == "" or result.get(key) != value:
            return False
    return True


def _reconcile(remote, channel: str, report: dict) -> str:
    probe = getattr(remote, "reconcile", None)
    if probe is None:
        return "unknown"
    verdict = probe(channel, report)
    if verdict == "absent":
        return "absent"
    if isinstance(verdict, dict) and confirmation_binds(verdict, channel, report):
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


class ReportOutbox:
    def __init__(self, root: Path, store, limits: OutboxLimits | None = None, *, trusted_executors: set[str] | None = None):
        self.root = root
        self.store = store
        self.limits = limits or OutboxLimits()
        self.trusted_executors = set(trusted_executors or ())
        self.directory = root / "report-outbox"
        self.archive = root / "report-archive"
        self.held = self.directory / "held"
        self.quarantine = root / "report-quarantine"
        self.receipts = root / "receipts"
        for folder in (self.directory, self.archive, self.held, self.quarantine, self.receipts):
            folder.mkdir(parents=True, mode=0o700, exist_ok=True)

    def submit(self, report: dict, channels: list[str], remote, *, crash: str | None = None, nonce_commit=None, replay_only: bool = False) -> dict:
        validate_report(report, self.trusted_executors)
        chosen = [_safe_channel(channel) for channel in channels]
        if not chosen or len(set(chosen)) != len(chosen):
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "delivery channel is invalid")
        unsigned = {"channels", "payload_hash", "actor", "proof"}
        canonical = json.dumps({key: value for key, value in report.items() if key not in unsigned}, sort_keys=True, separators=(",", ":")).encode()
        if len(canonical) > self.limits.max_report_bytes:
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "report exceeds size limit")
        digest = hashlib.sha256(canonical).hexdigest()
        envelope = redact({key: value for key, value in report.items() if key not in unsigned})
        envelope["channels"] = chosen
        envelope["payload_hash"] = digest
        file_bytes = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        path = self.directory / f"{report['run_id']}.json"
        archived_path = self.archive / f"{report['run_id']}.json"
        with self.store.transaction() as conn:
            row = conn.execute("SELECT payload_hash, channels_json, archived FROM reports WHERE run_id=?", (report["run_id"],)).fetchone()
            if row is not None:
                if row["payload_hash"] != digest or json.loads(row["channels_json"]) != chosen:
                    raise ControlError("IDEMPOTENCY_PAYLOAD_MISMATCH", 409, "report run_id already has a different payload")
            else:
                if replay_only:
                    raise ControlError("PROOF_REPLAY", 403, "nonce already used")
                if nonce_commit is not None:
                    nonce_commit(conn)
                pending = conn.execute("SELECT COUNT(*) AS n FROM reports WHERE archived=0").fetchone()["n"]
                if pending >= self.limits.max_outbox_reports:
                    raise ControlError("CAPACITY_EXHAUSTED", 409, "outbox capacity is exhausted")
                now = _now()
                conn.execute(
                    "INSERT INTO reports (run_id, payload_hash, channels_json, created_at, archived) VALUES (?, ?, ?, ?, 0)",
                    (report["run_id"], digest, json.dumps(chosen), now),
                )
                for channel in chosen:
                    self._upsert(conn, f"report-{report['run_id']}", channel, "DURABLE_LOCAL", report["run_id"], None, now)
        if crash == "after-commit":
            os._exit(94)
        if path.exists():
            if path.read_bytes() != file_bytes:
                raise ControlError("IDEMPOTENCY_PAYLOAD_MISMATCH", 409, "report run_id already has a different payload")
        elif not archived_path.exists():
            if not _create_exclusive(path, file_bytes):
                if path.read_bytes() != file_bytes:
                    raise ControlError("IDEMPOTENCY_PAYLOAD_MISMATCH", 409, "report run_id already has a different payload")
        if crash == "after-file":
            os._exit(92)
        if crash == "before-remote":
            os._exit(91)
        if remote is None:
            receipts = [self._pending_receipt(report, channel) for channel in chosen]
            return {
                "run_id": report["run_id"],
                "path": str(path if path.exists() else archived_path),
                "receipts": receipts,
                "complete": False,
                "connected": False,
            }
        receipts = [self._dispatch(report, channel, remote, crash=crash) for channel in chosen]
        return {
            "run_id": report["run_id"],
            "path": str(path if path.exists() else archived_path),
            "receipts": receipts,
            "complete": all(item["state"] == "DELIVERED" for item in receipts),
            "connected": True,
        }

    def deliver_pending(self, remote, channels: tuple[str, ...] = DEFAULT_CHANNELS, *, crash: str | None = None) -> list[dict]:
        self.recover(abandon_claims=False)
        receipts = []
        with self.store.transaction() as conn:
            rows = conn.execute(
                "SELECT event_id, channel, run_id, state FROM deliveries WHERE state IN ('DURABLE_LOCAL','OUTBOX','DELIVERY_UNKNOWN')"
            ).fetchall()
        for row in rows:
            if not str(row["event_id"]).startswith("report-"):
                continue
            if row["channel"] not in channels:
                continue
            report = self._load_report(row["run_id"])
            if report is None:
                continue
            receipts.append(self._dispatch(report, row["channel"], remote, crash=crash))
            if crash == "after-remote":
                break
        return receipts

    def recover(self, *, abandon_claims: bool = False) -> list[str]:
        pending = []
        if self.directory.exists():
            for path in sorted(self.directory.glob("*.json")):
                try:
                    report = json.loads(path.read_text(encoding="utf-8"))
                    validate_report(report, self.trusted_executors)
                    if path.name != f"{report['run_id']}.json":
                        raise ValueError("report name does not match run_id")
                    chosen = report.get("channels")
                    if not isinstance(chosen, list) or not chosen:
                        raise ValueError("report is missing its channel selection")
                    chosen = [_safe_channel(channel) for channel in chosen]
                except (OSError, ValueError, ControlError, KeyError):
                    self._isolate_path(path, "report", "corrupt report file")
                    continue
                self._adopt(report, chosen)
                pending.append(report["run_id"])
        self._retire_dead_claims(abandon_claims=abandon_claims)
        return pending

    def _retire_dead_claims(self, *, abandon_claims: bool) -> None:
        """Move a crashed claim to unknown. A live worker keeps its token.

        ``abandon_claims`` does not clear a claim whose worker is still alive.
        """

        now = int(time.time())
        with self.store.transaction() as conn:
            rows = conn.execute(
                """
                SELECT event_id, channel, claim_token, claim_pid, claim_expires_at
                FROM deliveries WHERE state='OUTBOX'
                """
            ).fetchall()
            for row in rows:
                if _claim_is_active(row["claim_pid"], row["claim_expires_at"], now):
                    continue
                if row["claim_token"] is None and not abandon_claims:
                    continue
                conn.execute(
                    """
                    UPDATE deliveries
                    SET state='DELIVERY_UNKNOWN', claim_token=NULL, claim_pid=NULL,
                        claim_expires_at=NULL, updated_at=?
                    WHERE event_id=? AND channel=? AND state='OUTBOX'
                      AND claim_token IS ?
                    """,
                    (_now(), row["event_id"], row["channel"], row["claim_token"]),
                )

    def health(self) -> dict:
        reports, report_bytes = self._usage(self.directory, skip_held=True)
        archived, archive_bytes = self._usage(self.archive, skip_held=False)
        quarantined, quarantine_bytes = self._usage(self.quarantine, skip_held=False)
        with self.store.transaction() as conn:
            backlog = conn.execute(
                "SELECT COUNT(*) AS n FROM deliveries WHERE state IN ('DURABLE_LOCAL','OUTBOX','DELIVERY_UNKNOWN') AND event_id LIKE 'report-%'"
            ).fetchone()["n"]
            oldest = conn.execute(
                "SELECT MIN(pending_since) AS at FROM deliveries WHERE state IN ('DURABLE_LOCAL','OUTBOX','DELIVERY_UNKNOWN') AND event_id LIKE 'report-%'"
            ).fetchone()["at"]
            by_state = conn.execute("SELECT channel, state, COUNT(*) AS n FROM deliveries GROUP BY channel, state").fetchall()
            overflow = conn.execute("SELECT value FROM counters WHERE name='quarantine_overflow'").fetchone()
        channels: dict[str, dict[str, int]] = {}
        for row in by_state:
            channels.setdefault(row["channel"], {})[row["state"]] = row["n"]
        return {
            "backlog": backlog,
            "oldest_pending_at": oldest,
            "channels": channels,
            "outbox_reports": reports,
            "outbox_bytes": report_bytes,
            "outbox_limit_reports": self.limits.max_outbox_reports,
            "outbox_limit_bytes": self.limits.max_outbox_bytes,
            "outbox_remaining_reports": max(0, self.limits.max_outbox_reports - reports),
            "archive_reports": archived,
            "archive_bytes": archive_bytes,
            "quarantine_reports": quarantined,
            "quarantine_bytes": quarantine_bytes,
            "quarantine_limit_reports": self.limits.max_quarantine_reports,
            "quarantine_limit_bytes": self.limits.max_quarantine_bytes,
            "quarantine_overflow": 0 if overflow is None else overflow["value"],
        }

    def _dispatch(self, report: dict, channel: str, remote, *, crash: str | None = None) -> dict:
        channel = _safe_channel(channel)
        event_id = f"report-{report['run_id']}"
        current = self._channel_state(event_id, channel)
        if current in {"DELIVERED", "QUARANTINED"}:
            found = self._latest_receipt(event_id, channel, current)
            return found if found is not None else self._write_receipt(report, channel, current)
        if remote is None:
            return self._pending_receipt(report, channel, current or "DURABLE_LOCAL")
        if current == "DELIVERY_UNKNOWN":
            if self._local_receipt_binds(event_id, channel, report):
                token = self._claim(event_id, channel, report["run_id"])
                if token and self._set_state(event_id, channel, "DELIVERED", report["run_id"], None, token):
                    self._maybe_archive(report["run_id"])
                found = self._latest_receipt(event_id, channel, "DELIVERED")
                return found if found is not None else self._pending_receipt(report, channel, "DELIVERY_UNKNOWN")
            token = self._claim(event_id, channel, report["run_id"])
            if not token:
                found = self._latest_receipt(event_id, channel, "DELIVERY_UNKNOWN")
                return found if found is not None else self._pending_receipt(report, channel, "DELIVERY_UNKNOWN")
            verdict = _reconcile(remote, channel, report)
            if verdict == "delivered":
                receipt = self._write_receipt(report, channel, "DELIVERED")
                if self._set_state(event_id, channel, "DELIVERED", report["run_id"], None, token):
                    self._maybe_archive(report["run_id"])
                return receipt
            if verdict != "absent":
                self._set_state(event_id, channel, "DELIVERY_UNKNOWN", report["run_id"], "delivery outcome is unknown", token)
                found = self._latest_receipt(event_id, channel, "DELIVERY_UNKNOWN")
                return found if found is not None else self._write_receipt(report, channel, "DELIVERY_UNKNOWN")
            return self._send(report, channel, remote, token, crash=crash)
        if current == "OUTBOX":
            return self._pending_receipt(report, channel, "OUTBOX")
        if current not in {None, "DURABLE_LOCAL", "DELIVERY_UNKNOWN"}:
            return self._pending_receipt(report, channel, current or "DURABLE_LOCAL")
        token = self._claim(event_id, channel, report["run_id"])
        if not token:
            raced = self._channel_state(event_id, channel) or "OUTBOX"
            found = self._latest_receipt(event_id, channel, raced)
            return found if found is not None else self._pending_receipt(report, channel, raced)
        return self._send(report, channel, remote, token, crash=crash)

    def _send(self, report: dict, channel: str, remote, token: str, *, crash: str | None) -> dict:
        event_id = f"report-{report['run_id']}"
        state = "DELIVERY_UNKNOWN"
        detail = "remote returned no trusted confirmation"
        try:
            result = remote(channel, report)
            if crash == "after-remote":
                os._exit(93)
            if confirmation_binds(result, channel, report):
                state = "DELIVERED"
                detail = None
        except BadRecord as exc:
            state = "QUARANTINED"
            detail = exc.message
            self._isolate_record(channel, report, exc.message)
        except (DeliveryUnknown, OSError, TimeoutError):
            state = "DELIVERY_UNKNOWN"
            detail = "delivery outcome is unknown"
        except Exception:
            state = "DELIVERY_UNKNOWN"
            detail = "delivery outcome is unknown"
        receipt = self._write_receipt(report, channel, state)
        if self._set_state(event_id, channel, state, report["run_id"], detail, token) and state == "DELIVERED":
            self._maybe_archive(report["run_id"])
        return receipt

    def _claim(self, event_id: str, channel: str, run_id: str) -> str | None:
        token = uuid.uuid4().hex
        now = int(time.time())
        expires = now + _CLAIM_TTL_SECONDS
        with self.store.transaction() as conn:
            before = conn.total_changes
            conn.execute(
                """
                UPDATE deliveries
                SET state='OUTBOX', detail=?, updated_at=?, claim_token=?, claim_pid=?, claim_expires_at=?
                WHERE event_id=? AND channel=? AND state IN ('DURABLE_LOCAL', 'DELIVERY_UNKNOWN')
                """,
                (token, _now(), token, os.getpid(), expires, event_id, channel),
            )
            if conn.total_changes == before:
                return None
        return token

    def _local_receipt_binds(self, event_id: str, channel: str, report: dict) -> bool:
        receipt = self._latest_receipt(event_id, channel, "DELIVERED")
        if receipt is None:
            return False
        return confirmation_binds(receipt, channel, report)

    def _touch(self, event_id: str, channel: str) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE deliveries SET updated_at=? WHERE event_id=? AND channel=?",
                (_now(), event_id, channel),
            )

    def _adopt(self, report: dict, channels: list[str]) -> None:
        event_id = f"report-{report['run_id']}"
        digest = report.get("payload_hash")
        if not isinstance(digest, str) or len(digest) != 64:
            digest = hashlib.sha256(json.dumps(redact(report), sort_keys=True).encode()).hexdigest()
        with self.store.transaction() as conn:
            found = conn.execute("SELECT payload_hash FROM reports WHERE run_id=?", (report["run_id"],)).fetchone()
            now = _now()
            if found is None:
                conn.execute(
                    "INSERT INTO reports (run_id, payload_hash, channels_json, created_at, archived) VALUES (?, ?, ?, ?, 0)",
                    (report["run_id"], digest, json.dumps(channels), now),
                )
            for channel in channels:
                existing = conn.execute(
                    "SELECT 1 FROM deliveries WHERE event_id=? AND channel=?",
                    (event_id, channel),
                ).fetchone()
                if existing is None:
                    self._upsert(conn, event_id, channel, "DURABLE_LOCAL", report["run_id"], None, now)

    def _maybe_archive(self, run_id: str) -> None:
        event_id = f"report-{run_id}"
        with self.store.transaction() as conn:
            rows = conn.execute("SELECT state FROM deliveries WHERE event_id=?", (event_id,)).fetchall()
            if not rows or any(row["state"] != "DELIVERED" for row in rows):
                return
            conn.execute("UPDATE reports SET archived=1 WHERE run_id=?", (run_id,))
        source = self.directory / f"{run_id}.json"
        if source.exists():
            target = self.archive / f"{run_id}.json"
            os.replace(source, target)

    def _load_report(self, run_id: str) -> dict | None:
        for folder in (self.directory, self.archive):
            path = folder / f"{run_id}.json"
            if path.exists():
                report = json.loads(path.read_text(encoding="utf-8"))
                return report
        return None

    def _channel_state(self, event_id: str, channel: str) -> str | None:
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT state FROM deliveries WHERE event_id=? AND channel=?", (event_id, channel)
            ).fetchone()
        return None if row is None else row["state"]

    def _set_state(self, event_id: str, channel: str, state: str, run_id: str, detail: str | None, claim_token: str) -> bool:
        with self.store.transaction() as conn:
            current = conn.execute(
                "SELECT state, claim_token FROM deliveries WHERE event_id=? AND channel=?",
                (event_id, channel),
            ).fetchone()
            if current and current["state"] in {"DELIVERED", "QUARANTINED"}:
                return current["state"] == state
            if current is None or current["claim_token"] != claim_token:
                return False
            cursor = conn.execute(
                """
                UPDATE deliveries
                SET state=?, detail=?, updated_at=?, claim_token=NULL, claim_pid=NULL, claim_expires_at=NULL
                WHERE event_id=? AND channel=? AND claim_token=?
                """,
                (state, detail, _now(), event_id, channel, claim_token),
            )
            return cursor.rowcount == 1

    def _upsert(self, conn, event_id: str, channel: str, state: str, run_id: str, detail: str | None, now: str | None = None) -> None:
        moment = now or _now()
        conn.execute(
            """
            INSERT INTO deliveries (
                event_id, channel, state, detail, updated_at, pending_since, run_id,
                claim_token, claim_pid, claim_expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
            ON CONFLICT(event_id, channel) DO UPDATE SET
                state=excluded.state, detail=excluded.detail, updated_at=excluded.updated_at
            WHERE deliveries.state NOT IN ('OUTBOX', 'DELIVERED', 'QUARANTINED')
            """,
            (event_id, channel, state, detail, moment, moment, run_id),
        )

    def _pending_receipt(self, report: dict, channel: str, state: str = "DURABLE_LOCAL") -> dict:
        return self._receipt_body(report, channel, state, receipt_id=None, persisted=False)

    def _latest_receipt(self, event_id: str, channel: str, state: str) -> dict | None:
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT body_json FROM receipts WHERE event_id=? AND channel=? AND state=? ORDER BY created_at DESC LIMIT 1",
                (event_id, channel, state),
            ).fetchone()
        return None if row is None else json.loads(row["body_json"])

    def _write_receipt(self, report: dict, channel: str, state: str) -> dict:
        receipt = self._receipt_body(report, channel, state, receipt_id="rcpt_" + uuid.uuid4().hex, persisted=True)
        atomic_write(self.receipts / f"{receipt['receipt_id']}.json", json.dumps(receipt, sort_keys=True).encode())
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO receipts (receipt_id, channel, run_id, event_id, state, body_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (receipt["receipt_id"], channel, report["run_id"], receipt["event_id"], state, json.dumps(receipt), receipt["created_at"]),
            )
        return receipt

    def _receipt_body(self, report: dict, channel: str, state: str, *, receipt_id: str | None, persisted: bool) -> dict:
        return redact({
            "schema_version": 2,
            "receipt_id": receipt_id,
            "run_id": report["run_id"],
            "event_id": f"report-{report['run_id']}",
            "channel": channel,
            "state": state,
            "outcome": report.get("outcome"),
            "phase": report.get("phase"),
            "head": report.get("head"),
            "base": report.get("base"),
            "merge": report.get("merge"),
            "tree": report.get("tree"),
            "executor_id": report.get("executor_id"),
            "evidence_ref": report.get("evidence_ref"),
            "candidate_bound": all(report.get(field) for field in ("head", "base", "merge", "tree")),
            "complete": state == "DELIVERED",
            "persisted": persisted,
            "exactly_once": False,
            "lease_token": None,
            "redacted": True,
            "created_at": _now(),
        })

    def _isolate_record(self, channel: str, report: dict, reason: str) -> None:
        target = self.quarantine / channel
        target.mkdir(parents=True, mode=0o700, exist_ok=True)
        payload = json.dumps({"run_id": report["run_id"], "channel": channel, "reason": reason, "isolated": True}).encode()
        count, total = self._usage(self.quarantine, skip_held=False)
        if count >= self.limits.max_quarantine_reports or total + len(payload) > self.limits.max_quarantine_bytes:
            self._bump_overflow()
            return
        atomic_write(target / f"{report['run_id']}.json", payload)

    def _isolate_path(self, path: Path, channel: str, reason: str) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        count, total = self._usage(self.quarantine, skip_held=False)
        destination = self.quarantine / channel
        destination.mkdir(parents=True, mode=0o700, exist_ok=True)
        if count >= self.limits.max_quarantine_reports or total + size > self.limits.max_quarantine_bytes:
            self.held.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.replace(path, self.held / path.name)
            self._bump_overflow()
            return
        os.replace(path, destination / f"bad-{uuid.uuid4().hex}.json")

    def _bump_overflow(self) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO counters (name, value) VALUES ('quarantine_overflow', 1)
                ON CONFLICT(name) DO UPDATE SET value = value + 1
                """
            )

    def _usage(self, folder: Path, *, skip_held: bool) -> tuple[int, int]:
        if not folder.exists():
            return 0, 0
        count = 0
        total = 0
        for path in folder.rglob("*"):
            if skip_held and self.held in path.parents:
                continue
            if path.is_file():
                count += 1
                total += path.stat().st_size
        return count, total


def _create_exclusive(path: Path, data: bytes) -> bool:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return True
