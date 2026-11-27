"""MacCI report outbox. Bytes are durable before any remote status call.

Each delivery channel has its own state and acknowledgement. A bad record is
isolated on that channel; capacity and overflow stay visible in health().
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
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
REQUIRED = ("repository", "pr", "run_id", "head", "base", "merge", "phase", "outcome")
DEFAULT_CHANNELS = ("github_comment", "commit_status")


@dataclass(frozen=True)
class OutboxLimits:
    max_report_bytes: int = 12000
    max_outbox_reports: int = 100
    max_outbox_bytes: int = 1_200_000
    max_quarantine_reports: int = 20
    max_quarantine_bytes: int = 1_200_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_report(report: dict) -> None:
    if not isinstance(report, dict):
        raise ControlError("SCHEMA_UNSUPPORTED", 422, "report must be an object")
    missing = [field for field in REQUIRED if report.get(field) in (None, "")]
    if missing:
        raise ControlError("SCHEMA_UNSUPPORTED", 422, "report is incomplete")
    if type(report["pr"]) is not int or report["pr"] <= 0:
        raise ControlError("SCHEMA_UNSUPPORTED", 422, "report pr is invalid")
    if not isinstance(report["run_id"], str) or len(report["run_id"]) != 32 or any(ch not in "0123456789abcdef" for ch in report["run_id"]):
        raise ControlError("SCHEMA_UNSUPPORTED", 422, "report run_id is invalid")
    for field in ("head", "base", "merge"):
        value = report[field]
        if not isinstance(value, str) or len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
            raise ControlError("SCHEMA_UNSUPPORTED", 422, f"report {field} is incomplete")
    outcome = report["outcome"]
    phase = report["phase"]
    if outcome == "success" and phase != "passed":
        raise ControlError("SCHEMA_UNSUPPORTED", 422, "success report is incomplete")
    if outcome == "failure" and (phase == "passed" or phase not in FAILURE_PHASES):
        raise ControlError("SCHEMA_UNSUPPORTED", 422, "failure report is incomplete")
    if outcome not in {"success", "failure"}:
        raise ControlError("SCHEMA_UNSUPPORTED", 422, "report outcome is incomplete")


class ReportOutbox:
    def __init__(self, root: Path, store, limits: OutboxLimits | None = None):
        self.root = root
        self.store = store
        self.limits = limits or OutboxLimits()
        self.directory = root / "report-outbox"
        self.quarantine = root / "report-quarantine"
        self.receipts = root / "receipts"
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.quarantine.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.receipts.mkdir(parents=True, mode=0o700, exist_ok=True)

    def submit(self, report: dict, channels: list[str], remote, *, crash: str | None = None) -> dict:
        validate_report(report)
        encoded = json.dumps(redact(report), sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > self.limits.max_report_bytes:
            raise ControlError("SCHEMA_UNSUPPORTED", 422, "report exceeds size limit")
        path = self.directory / f"{report['run_id']}.json"
        existing = self._existing_payload(path)
        if existing is None:
            self._reserve_capacity(len(encoded))
            atomic_write(path, encoded)
            if crash == "after-file":
                os._exit(92)
            self._record_durable(report, channels)
        elif existing != encoded:
            raise ControlError("IDEMPOTENCY_PAYLOAD_MISMATCH", 409, "report run_id already has a different payload")
        if crash == "before-remote":
            os._exit(91)
        stored = json.loads(path.read_text(encoding="utf-8"))
        receipts = [self._dispatch(stored, channel, remote) for channel in channels]
        return {"run_id": report["run_id"], "path": str(path), "receipts": receipts, "complete": True}

    def deliver_pending(self, remote, channels: tuple[str, ...] = DEFAULT_CHANNELS) -> list[dict]:
        self.recover()
        receipts = []
        with self.store.transaction() as conn:
            rows = conn.execute(
                "SELECT event_id, channel, run_id, state FROM deliveries WHERE state IN ('DURABLE_LOCAL','OUTBOX','DELIVERY_UNKNOWN')"
            ).fetchall()
        for row in rows:
            if row["channel"] not in channels and not row["event_id"].startswith("report-"):
                continue
            if not str(row["event_id"]).startswith("report-"):
                continue
            path = self.directory / f"{row['run_id']}.json"
            if not path.exists():
                continue
            report = json.loads(path.read_text(encoding="utf-8"))
            receipts.append(self._dispatch(report, row["channel"], remote))
        return receipts

    def recover(self) -> list[str]:
        pending = []
        if not self.directory.exists():
            return pending
        for path in sorted(self.directory.glob("*.json")):
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
                validate_report(report)
                if path.name != f"{report['run_id']}.json":
                    raise ValueError("report name does not match run_id")
            except (OSError, ValueError, ControlError):
                self._isolate_path(path, "report", "corrupt report file")
                continue
            self._adopt(report)
            pending.append(report["run_id"])
        return pending

    def health(self) -> dict:
        reports, report_bytes = self._usage(self.directory)
        quarantined, quarantine_bytes = self._usage(self.quarantine)
        with self.store.transaction() as conn:
            backlog = conn.execute(
                "SELECT COUNT(*) AS n FROM deliveries WHERE state IN ('DURABLE_LOCAL','OUTBOX','DELIVERY_UNKNOWN')"
            ).fetchone()["n"]
            oldest = conn.execute(
                "SELECT MIN(updated_at) AS at FROM deliveries WHERE state IN ('DURABLE_LOCAL','OUTBOX','DELIVERY_UNKNOWN')"
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
            "quarantine_reports": quarantined,
            "quarantine_bytes": quarantine_bytes,
            "quarantine_limit_reports": self.limits.max_quarantine_reports,
            "quarantine_limit_bytes": self.limits.max_quarantine_bytes,
            "quarantine_overflow": 0 if overflow is None else overflow["value"],
        }

    def _dispatch(self, report: dict, channel: str, remote) -> dict:
        event_id = f"report-{report['run_id']}"
        current = self._channel_state(event_id, channel)
        if current in {"DELIVERED", "QUARANTINED"}:
            found = self._latest_receipt(event_id, channel, current)
            return found if found is not None else self._write_receipt(report, channel, current)
        self._set_state(event_id, channel, "OUTBOX", report["run_id"])
        state = "DELIVERED"
        detail = None
        try:
            if remote is not None:
                remote(channel, report)
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
        self._set_state(event_id, channel, state, report["run_id"], detail)
        return self._write_receipt(report, channel, state)

    def _record_durable(self, report: dict, channels: list[str]) -> None:
        event_id = f"report-{report['run_id']}"
        with self.store.transaction() as conn:
            found = conn.execute("SELECT 1 FROM deliveries WHERE event_id=?", (event_id,)).fetchone()
            if found:
                return
            for channel in channels:
                self._upsert(conn, event_id, channel, "DURABLE_LOCAL", report["run_id"], None)

    def _adopt(self, report: dict) -> None:
        event_id = f"report-{report['run_id']}"
        with self.store.transaction() as conn:
            found = conn.execute("SELECT 1 FROM deliveries WHERE event_id=?", (event_id,)).fetchone()
            if found:
                return
            for channel in DEFAULT_CHANNELS:
                self._upsert(conn, event_id, channel, "DURABLE_LOCAL", report["run_id"], None)

    def _channel_state(self, event_id: str, channel: str) -> str | None:
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT state FROM deliveries WHERE event_id=? AND channel=?", (event_id, channel)
            ).fetchone()
        return None if row is None else row["state"]

    def _set_state(self, event_id: str, channel: str, state: str, run_id: str, detail: str | None = None) -> None:
        with self.store.transaction() as conn:
            current = conn.execute(
                "SELECT state FROM deliveries WHERE event_id=? AND channel=?", (event_id, channel)
            ).fetchone()
            if current and current["state"] in {"DELIVERED", "QUARANTINED"} and state != current["state"]:
                return
            if state == "OUTBOX" and current and current["state"] not in {"DURABLE_LOCAL", "DELIVERY_UNKNOWN", "OUTBOX"}:
                return
            self._upsert(conn, event_id, channel, state, run_id, detail)

    def _upsert(self, conn, event_id: str, channel: str, state: str, run_id: str, detail: str | None) -> None:
        conn.execute(
            """
            INSERT INTO deliveries (event_id, channel, state, detail, updated_at, run_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id, channel) DO UPDATE SET
                state=excluded.state, detail=excluded.detail, updated_at=excluded.updated_at
            """,
            (event_id, channel, state, detail, _now(), run_id),
        )

    def _latest_receipt(self, event_id: str, channel: str, state: str) -> dict | None:
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT body_json FROM receipts WHERE event_id=? AND channel=? AND state=? ORDER BY created_at DESC LIMIT 1",
                (event_id, channel, state),
            ).fetchone()
        return None if row is None else json.loads(row["body_json"])

    def _write_receipt(self, report: dict, channel: str, state: str) -> dict:
        receipt = redact({
            "schema_version": 2,
            "receipt_id": "rcpt_" + uuid.uuid4().hex,
            "run_id": report["run_id"],
            "event_id": f"report-{report['run_id']}",
            "channel": channel,
            "state": state,
            "outcome": report["outcome"],
            "phase": report["phase"],
            "complete": True,
            "lease_token": None,
            "redacted": True,
            "created_at": _now(),
        })
        atomic_write(self.receipts / f"{receipt['receipt_id']}.json", json.dumps(receipt, sort_keys=True).encode())
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO receipts (receipt_id, channel, run_id, event_id, state, body_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (receipt["receipt_id"], channel, report["run_id"], receipt["event_id"], state, json.dumps(receipt), receipt["created_at"]),
            )
        return receipt

    def _reserve_capacity(self, extra: int) -> None:
        reports, report_bytes = self._usage(self.directory)
        if reports >= self.limits.max_outbox_reports or report_bytes + extra > self.limits.max_outbox_bytes:
            raise ControlError("CAPACITY_EXHAUSTED", 409, json.dumps(self.health(), sort_keys=True))

    def _isolate_record(self, channel: str, report: dict, reason: str) -> None:
        target = self.quarantine / channel
        target.mkdir(parents=True, mode=0o700, exist_ok=True)
        payload = json.dumps({"run_id": report["run_id"], "channel": channel, "reason": reason, "isolated": True}).encode()
        count, total = self._usage(self.quarantine)
        if count >= self.limits.max_quarantine_reports or total + len(payload) > self.limits.max_quarantine_bytes:
            self._bump_overflow()
            return
        atomic_write(target / f"{report['run_id']}.json", payload)

    def _isolate_path(self, path: Path, channel: str, reason: str) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        count, total = self._usage(self.quarantine)
        destination = self.quarantine / channel
        destination.mkdir(parents=True, mode=0o700, exist_ok=True)
        if count >= self.limits.max_quarantine_reports or total + size > self.limits.max_quarantine_bytes:
            path.unlink(missing_ok=True)
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

    def _existing_payload(self, path: Path) -> bytes | None:
        if not path.exists():
            return None
        return path.read_bytes()

    def _usage(self, folder: Path) -> tuple[int, int]:
        if not folder.exists():
            return 0, 0
        count = 0
        total = 0
        for path in folder.rglob("*"):
            if path.is_file():
                count += 1
                total += path.stat().st_size
        return count, total
