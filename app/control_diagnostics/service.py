"""Unified read-only diagnostics over a durable journal and explicit ports.

Failure logs are read only from the server-authorized application directory,
from the same file descriptor, with a byte cap. External reports are not
opened. Untrusted objects keep an allowlist; this package does not detect
every unknown secret. Journal commits share one cross-process lock. A torn
line or a missing receipt stays visible, and this package does not fabricate
records to make a sequence look continuous.
"""

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.control_diagnostics.extract import external_report_evidence, summarize_failure
from app.control_diagnostics.journal import Journal, Limits, commit_fields, project_stream
from app.control_diagnostics.observations import normalize_observation
from app.control_diagnostics.ports import (
    BlockedAuthorizationPort,
    BlockedDeploymentPort,
    BlockedEventPort,
    BlockedSchedulePort,
    SideEffectProbe,
)
from app.control_diagnostics.redaction import contains_secret, redact_value
from app.control_diagnostics.safe_io import SafeRoot, UnsafePath

PATH = "/api/v1/control/diagnostics"
METHOD = "GET"
MODULE_NAME = "app.control_diagnostics"
ROUTE_MODULE = "app.control_routes_w06"
MOUNTED_IN_APP_MAIN = False
PRODUCTION_ENABLEMENT = False
PRODUCTION_VERIFICATION_ENABLED = False
PRODUCTION_DB_ACCESS = False
DISPATCH_EXPANSION = False
SUBSCRIBE_PUSH = False
_SUCCESS_WORDS = {"success", "passed", "succeeded", "commit", "ok", "healthy"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,80}\Z")
_APP = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_STREAM = re.compile(r"[A-Za-z0-9_.-]{1,40}:diagnostic\Z")

_REASONS = {
    "pytest_failure": "A pytest failure identifier was extracted. No receipt shows an executor claimed and fixed it.",
    "typescript_error": "A TypeScript diagnostic was extracted. No receipt shows an executor claimed and fixed it.",
    "compile_failure": "A compile failure was extracted. The summary is the compiler code and location, not a repair.",
    "dependency_failure": "A dependency failure was extracted. Resolution was not retried.",
    "container_failure": "A container failure was extracted. The container was not rebuilt by this read.",
    "timeout": "A timeout was extracted. The attempt was not retried.",
    "startup_failure": "A startup failure was extracted. The service was not restarted.",
    "unattributed": "The failure phase did not match a known grammar. The outcome is unknown, not success.",
    "missing": "Required evidence is missing. Absence is not a pass.",
    "expired": "Evidence is expired under the server clock. It is not a pass.",
    "unknown": "Evidence is unknown. It is not a pass.",
    "blocked_w07": "The W07 deployment event interface is pending. No deployment outcome is available.",
    "unsupported_operation": "The operation is outside the read-only set. It was not dispatched.",
    "unsupported_platform": "The service inventory is unsupported on this platform. No launchctl command was run.",
}
_GUIDANCE = (
    "This read did not deploy, retry, repair, or write an Issue or comment.",
    "Automatic executor claim is not proven (UNKNOWN_EXECUTOR).",
    "A human decides the next step. A repair, if any, uses a new run_id after a separate authorization.",
)


def intervention_for(category):
    reason = _REASONS.get(category, _REASONS["unknown"])
    return {
        "required": True,
        "automatic_executor": "not_proven",
        "automatic_claim": False,
        "automatic_repair": False,
        "code": "UNKNOWN_EXECUTOR",
        "category": category,
        "reason": reason,
        "repair_guidance": list(_GUIDANCE),
        "writes_issue": False,
        "writes_comment": False,
        "side_effect": False,
    }


class DiagnosticService:
    """Isolated diagnostics. Production enablement flags stay false."""

    def __init__(self, journal, *, clock, authz, events, schedule, deployment, probe, log_root=None):
        self.journal = journal
        self.clock = clock
        self.authz = authz
        self.events = events
        self.schedule = schedule
        self.deployment = deployment
        self.probe = probe
        self.log_root = log_root

    @classmethod
    def open(cls, root, *, clock, authz=None, events=None, schedule=None, deployment=None,
             probe=None, limits=None, writable=True, log_root=None):
        journal = Journal.open(Path(root), writable=writable, limits=limits or Limits())
        authorized = None
        if log_root is not None:
            authorized = SafeRoot.open(log_root)
        return cls(
            journal,
            clock=clock,
            authz=authz or BlockedAuthorizationPort(),
            events=events or BlockedEventPort(),
            schedule=schedule or BlockedSchedulePort(),
            deployment=deployment or BlockedDeploymentPort(),
            probe=probe or SideEffectProbe(),
            log_root=authorized,
        )

    def flags(self):
        return {
            "module": "app.control_diagnostics",
            "route_module": "app.control_routes_w06",
            "mounted_in_app_main": MOUNTED_IN_APP_MAIN,
            "production_enablement": PRODUCTION_ENABLEMENT,
            "production_verification_enabled": PRODUCTION_VERIFICATION_ENABLED,
            "production_db_access": PRODUCTION_DB_ACCESS,
            "dispatch_expansion": DISPATCH_EXPANSION,
            "subscribe_push": SUBSCRIBE_PUSH,
            "side_effect": False,
        }

    def enable_production(self):
        return self._closed("POLICY_DENIED", "Production enablement stays false in this package.")

    def read_production_database(self):
        return {
            "schema_version": 2,
            "http": 403,
            "code": "PRODUCTION_DB_FORBIDDEN",
            "side_effect": False,
            "evidence_state": "missing",
            "rows": None,
            "reason": "This package does not open a production database.",
        }

    def external_report(self, ref):
        report = external_report_evidence(ref)
        report["human_intervention"] = intervention_for("missing")
        return report

    def summarize(self, raw, phase):
        summary = summarize_failure(raw, phase)
        category = _summary_category(summary)
        if category:
            summary["human_intervention"] = intervention_for(category)
        return summary

    def ingest_failure_log(self, principal, app_id, path, envelope):
        decision = self._guard(principal, app_id, "diagnostics.ingest")
        if not decision["allowed"]:
            return self._denied(decision)
        if not isinstance(envelope, dict):
            return self._reject("SCHEMA_UNSUPPORTED", "Failure log envelope is missing.")
        if self.log_root is None:
            return self._reject("POLICY_DENIED", "No server log root is authorized for this service.")
        try:
            raw = self.log_root.read_app_log(app_id, path, self.journal.limits.max_log_bytes)
        except FileNotFoundError:
            return self._reject("SCHEMA_UNSUPPORTED", "Failure log path is not a regular file.")
        except UnsafePath as exc:
            if exc.kind == "bound":
                return self._reject("SCHEMA_UNSUPPORTED", "Failure log exceeds the bounded read.")
            return self._reject("POLICY_DENIED", "Failure log path is outside the server log root.")
        summary = self.summarize(raw, envelope.get("phase"))
        record = self._base_record(app_id, envelope, summary)
        record["diagnostic_class"] = "failure"
        record["severity"] = "error"
        record["body"] = {
            "summary": summary,
            "origin": "synthetic_fixture",
            "runtime_evidence": False,
        }
        record["intervention"] = summary.get("human_intervention")
        return self._commit(principal, app_id, envelope, record, decision)

    def ingest_event(self, principal, app_id, event):
        decision = self._guard(principal, app_id, "diagnostics.ingest")
        if not decision["allowed"]:
            return self._denied(decision)
        checked = _validate_event(event, app_id)
        if checked is not None:
            return self._reject(checked[0], checked[1])
        try:
            body = redact_value(event.get("body") if isinstance(event.get("body"), dict) else {})
        except ValueError:
            return self._reject("SCHEMA_UNSUPPORTED", "Event body could not be redacted.")
        if contains_secret(json.dumps(body, ensure_ascii=True)):
            return self._reject("SCHEMA_UNSUPPORTED", "Event body could not be redacted.")
        evidence_state = event.get("evidence_state", "present")
        if evidence_state not in {"present", "missing", "expired", "unknown"}:
            evidence_state = "unknown"
        record = self._base_record(app_id, event, None)
        record.update({
            "stream_id": event["stream_id"],
            "stream_epoch": event["stream_epoch"],
            "producer_seq": event["producer_seq"],
            "local_seq": False,
            "diagnostic_class": event.get("diagnostic_class", "log"),
            "severity": event.get("severity", "info"),
            "evidence_state": evidence_state,
            "expires_at": event.get("expires_at"),
            "body": body,
            "disposition": "stream",
        })
        if event.get("channel") != "diagnostic" or event.get("event_class") != "diagnostic":
            record["disposition"] = "quarantine"
            record["event_class"] = event.get("event_class")
            record["channel"] = event.get("channel")
            record["body"] = {
                "reason": "A diagnostic record cannot apply an authorization, deployment, or candidate fact.",
                "rejected_class": event.get("event_class"),
                "rejected_channel": event.get("channel"),
                "outcome": None,
                "runtime_evidence": False,
            }
            record["intervention"] = intervention_for("unknown")
        elif event.get("classification") == "never_existed":
            record["body"] = {
                "kind": "never_existed",
                "evidence_state": "missing",
                "outcome": None,
                "runtime_evidence": False,
                "reason": "This diagnostic sequence is declared never to have existed.",
            }
            record["evidence_state"] = "missing"
            record["diagnostic_class"] = "gap"
        return self._commit(principal, app_id, event, record, decision)

    def ingest_observation(self, principal, app_id, observation, envelope):
        decision = self._guard(principal, app_id, "diagnostics.ingest")
        if not decision["allowed"]:
            return self._denied(decision)
        if not isinstance(envelope, dict):
            return self._reject("SCHEMA_UNSUPPORTED", "Observation envelope is missing.")
        normalized = normalize_observation(observation)
        record = self._base_record(app_id, envelope, None)
        record.update({
            "diagnostic_class": normalized["diagnostic_class"],
            "severity": normalized["severity"],
            "evidence_state": normalized["evidence_state"],
            "body": normalized["body"],
            "disposition": normalized["disposition"],
        })
        if normalized["intervention"]:
            record["intervention"] = intervention_for(normalized["intervention"]["category"])
        stored = self._commit(principal, app_id, envelope, record, decision)
        stored["executed"] = False
        stored["code"] = normalized["code"] or stored.get("code")
        if normalized["disposition"] == "quarantine" and stored.get("http") == 200:
            stored["http"] = normalized["http"]
        return stored

    def poll(self, principal, app_id, *, subject, after_seq=0, stream_id=None, cursors=None, route="isolated"):
        if route == "real":
            return self._closed(
                "NOT_AUTHORIZED",
                "A client-supplied test proof does not enter real routes while production verification is disabled.",
            )
        decision = self._guard(principal, app_id, "diagnostics.read")
        if not decision["allowed"]:
            return self._denied(decision)
        records, missing_segments, corrupt = self._visible(app_id)
        selected = [
            record for record in records
            if subject in {record.get("subject_id"), record.get("task_id"), record.get("run_id")}
        ]
        stream_ids = [stream_id] if stream_id else sorted({record["stream_id"] for record in selected})
        cursor_map = dict(cursors or {})
        ignored = None
        epoch_count = 0
        projected = {current: _project_app_stream(selected, current) for current in stream_ids}
        for view in projected.values():
            epoch_count += len(view["epochs"])
        single = len(stream_ids) == 1 and epoch_count == 1 and not cursor_map
        if single:
            cursor_map[stream_ids[0]] = after_seq
        elif after_seq and (len(stream_ids) > 1 or epoch_count > 1):
            ignored = after_seq
        items = []
        streams = {}
        gap = bool(missing_segments or corrupt)
        now = self.clock.now()
        for current in stream_ids:
            view = projected[current]
            epochs = {}
            stream_gap = False
            for epoch_view in view["epochs"]:
                cursor = _epoch_cursor(
                    cursor_map, current, epoch_view["epoch"], single, after_seq, len(view["epochs"]),
                )
                if epoch_view["missing"]:
                    stream_gap = True
                    gap = True
                epochs[str(epoch_view["epoch"])] = {
                    "watermark_seq": epoch_view["watermark"],
                    "gap": bool(epoch_view["missing"]),
                    "after_seq": cursor,
                    "missing_seq": list(epoch_view["missing"]),
                }
                items.extend(_public_slice(epoch_view, cursor, now))
            for record in view["quarantined"]:
                items.append(_public_record(record, "quarantined", now))
            latest = view["epochs"][-1]["watermark"] if view["epochs"] else 0
            streams[current] = {
                "watermark_seq": latest if len(view["epochs"]) == 1 else None,
                "gap": stream_gap,
                "after_seq": cursor_map.get(current, 0) if single else 0,
                "epochs": epochs,
            }
        if len(stream_ids) == 1 and epoch_count == 1:
            watermark = streams[stream_ids[0]]["watermark_seq"]
        elif not stream_ids:
            watermark = 0
            gap = bool(missing_segments or corrupt)
        else:
            watermark = None
        return {
            "schema_version": 2,
            "http": 200,
            "method": METHOD,
            "path": PATH,
            "mode": "poll_cursor",
            "subscribe": False,
            "side_effect": False,
            "host_mutation": False,
            "watermark_seq": watermark,
            "gap": gap,
            "items": items,
            "streams": streams,
            "segment_gaps": missing_segments,
            "corrupt_lines": corrupt,
            "after_seq_applied_globally": False,
            "ignored_after_seq": ignored,
            "runtime_evidence": False,
        }

    def subscribe(self, principal, app_id, **kwargs):
        decision = self._guard(principal, app_id, "diagnostics.read")
        if not decision["allowed"]:
            return self._denied(decision)
        return {
            "schema_version": 2,
            "http": 405,
            "code": "SCHEMA_UNSUPPORTED",
            "side_effect": False,
            "subscribe": False,
            "mode": "poll_cursor",
            "items": [],
            "reason": "Diagnostic.subscribe is not provided. Poll with after_seq.",
        }

    def list_records(self, principal, app_id, *, run_id=None, deployment_id=None, page_size=20, page_token=None):
        decision = self._guard(principal, app_id, "diagnostics.read")
        if not decision["allowed"]:
            return self._denied(decision)
        size = _page_size(page_size)
        if size is None:
            return self._reject("SCHEMA_UNSUPPORTED", "page_size is invalid.")
        start = _page_start(page_token)
        if start is None and page_token is not None:
            return self._reject("SCHEMA_UNSUPPORTED", "page_token is invalid.")
        records, missing_segments, corrupt = self._visible(app_id)
        classes = _classifications(records)
        filtered = []
        for record in records:
            if run_id is not None and record.get("run_id") != run_id:
                continue
            if deployment_id is not None and record.get("deployment_id") != deployment_id:
                continue
            if start is not None and record.get("receipt_seq", 0) <= start:
                continue
            filtered.append(record)
        page = filtered[:size]
        next_token = None
        if len(filtered) > size and page:
            next_token = _page_token(page[-1]["receipt_seq"])
        now = self.clock.now()
        return {
            "schema_version": 2,
            "http": 200,
            "side_effect": False,
            "host_mutation": False,
            "items": [_public_record(record, classes.get(record.get("record_id"), "quarantined"), now) for record in page],
            "next_page_token": next_token,
            "segment_gaps": missing_segments,
            "corrupt_lines": corrupt,
            "gap": bool(missing_segments or corrupt),
            "runtime_evidence": False,
        }

    def query_run(self, principal, app_id, run_id, *, page_size=20, page_token=None):
        decision = self._guard(principal, app_id, "diagnostics.read")
        if not decision["allowed"]:
            return self._denied(decision)
        page = self.list_records(
            principal, app_id, run_id=run_id, page_size=page_size, page_token=page_token,
        )
        if page.get("http") != 200:
            return page
        records, _, _ = self._visible(app_id)
        related = [record for record in records if record.get("run_id") == run_id]
        association = _association(related)
        w01 = self.events.read(app_id=app_id, run_id=run_id)
        w04 = self.schedule.read(app_id=app_id, run_id=run_id)
        w07 = self.deployment.read(app_id=app_id, run_id=run_id, deployment_id=association["deployment_id"]["value"])
        advisory = _advisory_claims(related)
        deployment_view = _port_fact(w07, advisory.get("deployment"))
        candidate_view = _port_fact(
            {"status": w01["status"], "state": _class_state(w01, "candidate"), "evidence_state": w01["evidence_state"],
             "runtime_evidence": False, "synthetic": w01.get("synthetic", False), "reason": w01.get("reason")},
            advisory.get("candidate"),
        )
        response = {
            "schema_version": 2,
            "http": 200,
            "side_effect": False,
            "host_mutation": False,
            "method": METHOD,
            "path": PATH,
            "run_id": run_id,
            "task": association["task_id"],
            "candidate": association["candidate_id"],
            "release": association["release_id"],
            "deployment": association["deployment_id"],
            "deployment_outcome": deployment_view,
            "candidate_outcome": candidate_view,
            "schedule": _schedule_view(w04),
            "interfaces": {"w01": w01, "w02": {"interface": decision["interface"], "runtime_evidence": False}, "w04": w04, "w07": w07},
            "advisory_claims": advisory,
            "items": page["items"],
            "next_page_token": page["next_page_token"],
            "gap": page["gap"],
            "segment_gaps": page["segment_gaps"],
            "runtime_evidence": False,
        }
        category = _intervention_category(related, w07)
        response["human_intervention"] = intervention_for(category) if category else None
        for field in (
            "interfaces",
            "advisory_claims",
            "items",
            "deployment_outcome",
            "candidate_outcome",
            "schedule",
            "human_intervention",
        ):
            response[field] = _redact_public(response.get(field))
        return response

    def _visible(self, app_id):
        records, missing_segments, corrupt = self.journal.records()
        visible = [record for record in records if record.get("app_id") == app_id]
        return visible, missing_segments, corrupt

    def _guard(self, principal, app_id, action):
        if not isinstance(app_id, str) or _APP.fullmatch(app_id) is None:
            return {"allowed": False, "http": 422, "code": "SCHEMA_UNSUPPORTED", "evidence_state": "unknown", "interface": "local"}
        authenticated = self.authz.authenticate(principal)
        if not authenticated["allowed"]:
            return authenticated
        return self.authz.authorize(authenticated["principal_id"], app_id, action)

    def _base_record(self, app_id, envelope, summary):
        now = self.clock.now().isoformat()
        return {
            "schema_version": 2,
            "record_id": "rec_" + uuid.uuid4().hex,
            "app_id": app_id,
            "journal_epoch": self.journal.meta["journal_epoch"],
            "stream_id": envelope.get("stream_id") or "w06:diagnostic",
            "stream_epoch": envelope.get("stream_epoch") or self.journal.meta["journal_epoch"],
            "producer_seq": envelope.get("producer_seq"),
            "local_seq": envelope.get("producer_seq") is None,
            "occurred_at": envelope.get("occurred_at") or now,
            "received_at": now,
            "subject_id": envelope.get("subject_id"),
            "task_id": envelope.get("task_id"),
            "run_id": envelope.get("run_id"),
            "candidate_id": envelope.get("candidate_id"),
            "release_id": envelope.get("release_id"),
            "deployment_id": envelope.get("deployment_id"),
            "expires_at": envelope.get("expires_at"),
            "evidence_state": None if summary is None else summary["evidence_state"],
            "origin": "synthetic_fixture",
            "runtime_evidence": False,
            "synthetic": True,
            "authoritative": False,
            "disposition": "stream",
            "channel": "diagnostic",
            "event_class": "diagnostic",
        }

    def _commit(self, principal, app_id, envelope, record, decision):
        if "received_at" in envelope:
            return self._reject("SCHEMA_UNSUPPORTED", "received_at is server-filled.")
        for field in ("subject_id", "task_id", "run_id", "candidate_id", "release_id", "deployment_id"):
            value = record.get(field)
            if value is not None and (not isinstance(value, str) or _ID.fullmatch(value) is None):
                return self._reject("SCHEMA_UNSUPPORTED", f"{field} is invalid.")
        key = envelope.get("idempotency_key")
        if not isinstance(key, str) or not key:
            return self._reject("SCHEMA_UNSUPPORTED", "idempotency_key is required.")
        if contains_secret(key):
            return self._reject("SCHEMA_UNSUPPORTED", "idempotency_key is not safe to store.")
        try:
            record["intervention"] = redact_value(record.get("intervention"))
            record["body"] = redact_value(record.get("body"))
        except ValueError:
            return self._reject("SCHEMA_UNSUPPORTED", "Event body could not be redacted.")
        scope = "|".join([decision.get("principal_id") or "", app_id, "diagnostic.ingest", key])
        basis = _hash_basis(record)
        if record.get("local_seq"):
            basis["producer_seq"] = None
            basis["stream_epoch"] = None
            basis["stream_id"] = "w06:diagnostic"
        payload_hash = hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()
        record["idempotency_scope"] = scope
        record["payload_hash"] = payload_hash
        try:
            with self.journal.exclusive():
                previous = self.journal.find_idempotency(scope)
                if previous:
                    if previous.get("conflict") or previous.get("hash") != payload_hash:
                        outcome = "mismatch"
                        stored = None
                        replay_id = None
                    else:
                        outcome = "replay"
                        stored = None
                        replay_id = previous.get("record_id")
                else:
                    if record.get("local_seq"):
                        record["stream_id"] = "w06:diagnostic"
                        record["stream_epoch"] = self.journal.meta["journal_epoch"]
                        record["producer_seq"] = self.journal.meta["next_local_producer_seq"]
                    problem = commit_fields(record)
                    if problem:
                        return self._reject("SCHEMA_UNSUPPORTED", problem)
                    record["receipt_seq"] = self.journal.meta["next_receipt_seq"]
                    stored = self.journal.write_locked(record)
                    outcome = "stored"
                    replay_id = None
        except UnsafePath:
            return self._reject("POLICY_DENIED", "Diagnostic journal path is not a regular file inside the journal root.")
        except OSError:
            return self._reject("POLICY_DENIED", "Diagnostic journal path is not a regular file inside the journal root.")
        except ValueError:
            return self._reject("SCHEMA_UNSUPPORTED", "Event body could not be redacted.")
        if outcome == "mismatch":
            return self._reject("IDEMPOTENCY_PAYLOAD_MISMATCH", "The idempotency key was reused with a different payload.")
        if outcome == "replay":
            return {
                "schema_version": 2,
                "http": 200,
                "code": "IDEMPOTENCY_REPLAY",
                "side_effect": False,
                "host_mutation": False,
                "replayed": True,
                "record_id": replay_id,
            }
        return {
            "schema_version": 2,
            "http": 200,
            "code": None,
            "side_effect": True,
            "host_mutation": False,
            "deployed": False,
            "retried": False,
            "repaired": False,
            "record_id": stored["record_id"],
            "disposition": stored["disposition"],
            "body": stored["body"],
            "intervention": stored.get("intervention"),
            "evidence_state": stored.get("evidence_state"),
            "runtime_evidence": False,
        }

    def _denied(self, decision):
        return {
            "schema_version": 2,
            "http": decision.get("http", 403),
            "code": decision.get("code"),
            "side_effect": False,
            "host_mutation": False,
            "items": None,
            "watermark_seq": None,
            "gap": None,
            "evidence_state": decision.get("evidence_state", "missing"),
            "reason": decision.get("reason"),
            "runtime_evidence": False,
        }

    def _reject(self, code, reason):
        if code in {"POLICY_DENIED", "NOT_AUTHORIZED"}:
            http = 403
        elif code in {"IDEMPOTENCY_PAYLOAD_MISMATCH", "EVIDENCE_MISSING"}:
            http = 409
        else:
            http = 422
        return {
            "schema_version": 2,
            "http": http,
            "code": code,
            "side_effect": False,
            "host_mutation": False,
            "reason": reason,
            "runtime_evidence": False,
        }

    def _closed(self, code, reason):
        return {
            "schema_version": 2,
            "http": 403,
            "code": code,
            "side_effect": False,
            "host_mutation": False,
            "enabled": False,
            "items": None,
            "reason": reason,
            "runtime_evidence": False,
        }


def _redact_public(value):
    try:
        return redact_value(value)
    except ValueError:
        return {"redaction": "failed_closed", "retained": False}


def _validate_event(event, app_id):
    if not isinstance(event, dict):
        return "SCHEMA_UNSUPPORTED", "Event is not an object."
    if event.get("schema_version") not in (None, 2):
        return "SCHEMA_UNSUPPORTED", "schema_version is unsupported."
    if event.get("app_id") not in (None, app_id):
        return "POLICY_DENIED", "Event application does not match the authorized application."
    if "received_at" in event:
        return "SCHEMA_UNSUPPORTED", "received_at is server-filled."
    return None


def _hash_basis(record):
    return {
        "app_id": record.get("app_id"),
        "stream_id": record.get("stream_id"),
        "stream_epoch": record.get("stream_epoch"),
        "producer_seq": record.get("producer_seq"),
        "subject_id": record.get("subject_id"),
        "task_id": record.get("task_id"),
        "run_id": record.get("run_id"),
        "body": record.get("body"),
        "disposition": record.get("disposition"),
        "diagnostic_class": record.get("diagnostic_class"),
    }


def _summary_category(summary):
    if summary["evidence_state"] == "unknown":
        return "unattributed"
    if summary["outcome"] != "failed":
        return None
    categories = [item.get("category") for item in summary["items"] if item.get("category")]
    return categories[0] if categories else "unattributed"


def _project_app_stream(records, stream_id):
    grouped = {}
    quarantined = []
    for record in records:
        if record.get("stream_id") != stream_id:
            continue
        foreign = record.get("disposition") == "quarantine" and (
            record.get("event_class") != "diagnostic" or record.get("channel") != "diagnostic"
        )
        if foreign:
            quarantined.append(record)
            continue
        grouped.setdefault(record.get("stream_epoch"), []).append(record)
    views = []
    for epoch, group in sorted(grouped.items(), key=lambda item: item[0] or 0):
        watermark, applied, held, missing, duplicates = project_stream(group)
        views.append({
            "epoch": epoch,
            "watermark": watermark,
            "applied": applied,
            "held": held,
            "missing": missing,
            "duplicates": duplicates,
        })
    return {"epochs": views, "quarantined": quarantined}


def _classifications(records):
    classes = {}
    stream_ids = {record.get("stream_id") for record in records}
    for stream_id in stream_ids:
        view = _project_app_stream(records, stream_id)
        for epoch_view in view["epochs"]:
            for record in epoch_view["applied"]:
                classes[record.get("record_id")] = "quarantined" if record.get("disposition") == "quarantine" else "applied"
            for record in epoch_view["held"]:
                classes[record.get("record_id")] = "held"
            for record in epoch_view["duplicates"]:
                classes[record.get("record_id")] = "quarantined"
    return classes


def _epoch_cursor(cursor_map, stream_id, epoch, single, after_seq, epoch_count):
    compound = f"{stream_id}#{epoch}"
    if compound in cursor_map:
        return cursor_map[compound]
    if single:
        return after_seq
    if epoch_count == 1 and stream_id in cursor_map:
        return cursor_map[stream_id]
    return 0


def _public_slice(view, cursor, now):
    items = []
    exemplars = view["applied"] or view["held"] or view["duplicates"]
    stream_id = exemplars[0]["stream_id"] if exemplars else None
    epoch = exemplars[0]["stream_epoch"] if exemplars else view.get("epoch")
    for record in view["applied"]:
        if record["producer_seq"] > cursor:
            kind = "quarantined" if record.get("disposition") == "quarantine" else "applied"
            items.append(_public_record(record, kind, now))
    for seq in view["missing"]:
        if seq > cursor:
            items.append({
                "classification": "gap",
                "stream_id": stream_id,
                "stream_epoch": epoch,
                "producer_seq": seq,
                "evidence_state": "missing",
                "outcome": None,
                "success": False,
                "runtime_evidence": False,
                "side_effect": False,
                "body": {"missing_seq": seq},
            })
    for record in view["held"]:
        if record["producer_seq"] > cursor:
            items.append(_public_record(record, "held", now))
    for record in view["duplicates"]:
        items.append(_public_record(record, "quarantined", now))
    return items


def _public_record(record, classification, now):
    evidence_state = record.get("evidence_state") or "unknown"
    expires_at = _parse_time(record.get("expires_at")) if record.get("expires_at") else None
    if record.get("expires_at") and expires_at is None:
        evidence_state = "unknown"
    elif expires_at is not None and expires_at <= now:
        evidence_state = "expired"
    body = redact_value(record.get("body") or {})
    outcome = None
    if isinstance(body, dict):
        outcome = body.get("outcome")
        summary = body.get("summary")
        if isinstance(summary, dict):
            outcome = summary.get("outcome")
    claimed = outcome
    if evidence_state in {"missing", "expired", "unknown"} and _is_success(claimed):
        outcome = None
    elif evidence_state in {"missing", "expired", "unknown"} and claimed in _SUCCESS_WORDS:
        outcome = None
    kind = body.get("kind") if isinstance(body, dict) else None
    if kind == "never_existed":
        evidence_state = "missing"
        outcome = None
    public = {
        "record_id": record.get("record_id"),
        "app_id": record.get("app_id"),
        "stream_id": record.get("stream_id"),
        "stream_epoch": record.get("stream_epoch"),
        "producer_seq": record.get("producer_seq"),
        "receipt_seq": record.get("receipt_seq"),
        "subject_id": record.get("subject_id"),
        "task_id": record.get("task_id"),
        "run_id": record.get("run_id"),
        "candidate_id": record.get("candidate_id"),
        "release_id": record.get("release_id"),
        "deployment_id": record.get("deployment_id"),
        "severity": record.get("severity"),
        "diagnostic_class": record.get("diagnostic_class"),
        "classification": classification,
        "evidence_state": evidence_state,
        "outcome": outcome,
        "success": False,
        "runtime_evidence": False,
        "synthetic": True,
        "authoritative": False,
        "occurred_at": record.get("occurred_at"),
        "received_at": record.get("received_at"),
        "expires_at": record.get("expires_at"),
        "body": body,
        "intervention": record.get("intervention"),
        "side_effect": False,
    }
    if _is_success(claimed) and evidence_state in {"missing", "expired", "unknown"}:
        public["rejected_outcome"] = claimed
    return public


def _is_success(value):
    return isinstance(value, str) and value.lower() in _SUCCESS_WORDS


def _association(records):
    fields = ("task_id", "candidate_id", "release_id", "deployment_id")
    found = {field: [] for field in fields}
    for record in records:
        for field in fields:
            value = record.get(field)
            if value and value not in found[field]:
                found[field].append(value)
    result = {}
    for field, values in found.items():
        if not values:
            result[field] = {"value": None, "values": [], "evidence_state": "missing", "authority": "not_confirmed", "runtime_evidence": False}
        elif len(values) == 1:
            result[field] = {"value": values[0], "values": values, "evidence_state": "present", "authority": "log_only", "runtime_evidence": False}
        else:
            result[field] = {"value": None, "values": values, "evidence_state": "unknown", "authority": "conflict", "runtime_evidence": False}
    return result


def _advisory_claims(records):
    claims = {}
    for record in records:
        body = record.get("body") or {}
        if not isinstance(body, dict):
            continue
        for key in ("authorization", "deployment", "candidate"):
            if key in body:
                claims[key] = {"claim": body[key], "authoritative": False, "runtime_evidence": False, "can_override": False}
        summary = body.get("summary")
        if isinstance(summary, dict) and summary.get("outcome"):
            claims.setdefault("diagnostic", {"claim": summary.get("outcome"), "authoritative": False, "can_override": False})
    return claims


def _port_fact(port, advisory):
    status = port.get("status")
    if status == "BLOCKED" or port.get("evidence_state") in {None, "missing"} and port.get("state") is None:
        return {
            "state": None,
            "evidence_state": "missing",
            "runtime_evidence": False,
            "synthetic": bool(port.get("synthetic")),
            "interface": port.get("interface") or status,
            "advisory_ignored": advisory,
            "success": False,
            "reason": port.get("reason"),
        }
    evidence_state = port.get("evidence_state") or "unknown"
    state = port.get("state")
    if evidence_state in {"missing", "expired", "unknown"}:
        state_out = state if state in {None, "UNKNOWN"} else None
        if state not in {None, "UNKNOWN"}:
            evidence_state = "unknown"
            state_out = None
    else:
        state_out = state
    return {
        "state": state_out,
        "evidence_state": evidence_state,
        "runtime_evidence": False,
        "synthetic": bool(port.get("synthetic")),
        "interface": port.get("interface"),
        "advisory_ignored": advisory,
        "success": False,
        "reason": port.get("reason"),
    }


def _class_state(port, event_class):
    events = port.get("events") or []
    for event in events:
        if event.get("event_class") == event_class:
            return event.get("state")
    return None


def _schedule_view(port):
    if port.get("status") == "BLOCKED":
        return {"state": None, "evidence_state": "missing", "runtime_evidence": False, "interface": "pending", "success": False, "reason": port.get("reason")}
    evidence_state = port.get("evidence_state") or "unknown"
    state = port.get("state")
    if evidence_state in {"missing", "expired", "unknown"} and state not in {None, "UNKNOWN"}:
        return {"state": None, "evidence_state": "unknown", "runtime_evidence": False, "interface": port.get("interface"), "success": False, "rejected_state": state}
    return {
        "state": state,
        "evidence_state": evidence_state,
        "runtime_evidence": False,
        "synthetic": True,
        "interface": port.get("interface"),
        "success": False,
    }


def _intervention_category(records, deployment_port):
    for record in records:
        intervention = record.get("intervention") or {}
        if intervention.get("category"):
            return intervention["category"]
        if record.get("evidence_state") in {"missing", "expired", "unknown"} and record.get("diagnostic_class") == "failure":
            return record["evidence_state"]
    if deployment_port.get("status") == "BLOCKED":
        return "blocked_w07"
    if deployment_port.get("evidence_state") in {"missing", "expired", "unknown"}:
        return deployment_port["evidence_state"]
    return None


def _page_size(page_size):
    if isinstance(page_size, bool) or not isinstance(page_size, int):
        return None
    if page_size < 1 or page_size > 100:
        return None
    return page_size


def _page_start(token):
    if token is None:
        return 0
    if not isinstance(token, str) or not token.startswith("seq:"):
        return None
    number = token[4:]
    if not number.isdigit():
        return None
    return int(number)


def _page_token(receipt_seq):
    return f"seq:{receipt_seq}"


def _parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)
