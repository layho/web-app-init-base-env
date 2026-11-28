"""Isolated W06 diagnostics. Synthetic logs and a temporary directory only.

Command:
PYTHONPATH=. python3 -m unittest tests.test_v060_w06_isolated_diagnostics -v
"""

import hashlib
import json
import tempfile
import unittest
from datetime import timedelta, timezone
from pathlib import Path

from app.control_diagnostics import (
    DISPATCH_EXPANSION,
    MOUNTED_IN_APP_MAIN,
    PRODUCTION_DB_ACCESS,
    PRODUCTION_ENABLEMENT,
    PRODUCTION_VERIFICATION_ENABLED,
    SUBSCRIBE_PUSH,
    BlockedDeploymentPort,
    BlockedEventPort,
    BlockedSchedulePort,
    DiagnosticService,
    FixtureAuthorizationPort,
    FixtureDeploymentPort,
    FixtureEventPort,
    FixtureSchedulePort,
    Grant,
    Limits,
    ServerClock,
    SideEffectProbe,
    external_report_evidence,
    summarize_failure,
)
from app.control_diagnostics.journal import Journal

ROOT = Path(__file__).resolve().parents[1]
NOW = __import__("datetime").datetime(2026, 11, 28, 1, 0, tzinfo=timezone.utc)
PACKAGE = ROOT / "app" / "control_diagnostics"


def grants():
    return [
        Grant("fixture-app-a", "app-a", "diagnostics.read"),
        Grant("fixture-app-a", "app-a", "diagnostics.ingest"),
        Grant("fixture-app-b", "app-b", "diagnostics.read"),
        Grant("fixture-app-b", "app-b", "diagnostics.ingest"),
        Grant("fixture-reader", "app-a", "diagnostics.read"),
    ]


def principal(app_id="app-a", pid=None, expires_at=None):
    item = {
        "kind": "test",
        "id": pid or f"fixture-{app_id}",
        "proof_ref": f"test-proof://w06/{app_id}",
        "app_id": app_id,
    }
    if expires_at is not None:
        item["expires_at"] = expires_at
    return item


def event(seq, **overrides):
    item = {
        "schema_version": 2,
        "event_class": "diagnostic",
        "channel": "diagnostic",
        "stream_id": "fixture:diagnostic",
        "stream_epoch": 1,
        "producer_seq": seq,
        "occurred_at": "2026-11-28T01:00:00+00:00",
        "idempotency_key": f"idem-w06-{seq}",
        "app_id": "app-a",
        "subject_id": "tsk_1",
        "task_id": "tsk_1",
        "run_id": "run_1",
        "candidate_id": "cand_1",
        "release_id": "rel_1",
        "deployment_id": "dep_1",
        "severity": "info",
        "diagnostic_class": "log",
        "synthetic": True,
        "body": {"message": f"note-{seq}"},
    }
    item.update(overrides)
    return item


def fingerprint(root):
    found = {}
    base = Path(root)
    if not base.exists():
        return found
    for path in sorted(base.rglob("*")):
        if path.is_file() and not path.is_symlink():
            found[str(path.relative_to(base))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def journal_text(root):
    parts = []
    for path in Path(root).rglob("*"):
        if path.is_file() and not path.is_symlink():
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


class IsolatedDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "journal"
        self.clock = ServerClock(NOW)
        self.probe = SideEffectProbe()
        self.authz = FixtureAuthorizationPort(grants(), self.clock)
        self.events = BlockedEventPort()
        self.schedule = BlockedSchedulePort()
        self.deployment = BlockedDeploymentPort()
        self.service = self.open_service()

    def tearDown(self):
        self.tmp.cleanup()

    def open_service(self, **kwargs):
        return DiagnosticService.open(
            kwargs.pop("root", self.root),
            clock=self.clock,
            authz=kwargs.pop("authz", self.authz),
            events=kwargs.pop("events", self.events),
            schedule=kwargs.pop("schedule", self.schedule),
            deployment=kwargs.pop("deployment", self.deployment),
            probe=kwargs.pop("probe", self.probe),
            limits=kwargs.pop("limits", Limits(max_records_per_segment=10)),
        )

    def write_log(self, name, text):
        path = Path(self.tmp.name) / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_u02_pytest_and_typescript_extract_and_other_phases_are_not_empty(self):
        pytest_summary = summarize_failure(
            "FAILED tests/test_alpha.py::test_one - boom\n",
            "pytest-failed",
        )
        typescript_summary = summarize_failure(
            "frontend/src/App.tsx(10,2): error TS2322: Type is wrong\n",
            "frontend-failed",
        )
        compile_summary = summarize_failure(
            "COMPILE src/app.c:12: error C2065: missing\n",
            "compile-failed",
        )
        self.assertEqual(pytest_summary["items"][0]["category"], "pytest_failure")
        self.assertEqual(pytest_summary["items"][0]["test"], "test_one")
        self.assertEqual(typescript_summary["items"][0]["category"], "typescript_error")
        self.assertEqual(typescript_summary["items"][0]["code"], "TS2322")
        self.assertEqual(compile_summary["items"][0]["category"], "compile_failure")
        self.assertFalse(pytest_summary["success"])
        self.assertFalse(compile_summary["success"])
        self.assertNotEqual(compile_summary["items"], [])
        passed = summarize_failure("", "passed")
        self.assertFalse(passed["success"])
        self.assertIsNone(passed["outcome"])
        self.assertEqual(passed["evidence_state"], "missing")

    def test_u02_external_report_missing_is_not_empty_success(self):
        report = external_report_evidence("panel#13")
        self.assertEqual(report["evidence_state"], "missing")
        self.assertIsNone(report["failures"])
        self.assertIsNone(report["outcome"])
        self.assertFalse(report["success"])
        self.assertFalse(report["runtime_evidence"])
        self.assertNotEqual(report["failures"], [])
        service_report = self.service.external_report("panel#13")
        self.assertTrue(service_report["human_intervention"]["required"])
        self.assertTrue(service_report["human_intervention"]["reason"])
        self.assertTrue(service_report["human_intervention"]["repair_guidance"])

    def test_d01_each_failure_class_has_a_summary_and_unparsed_is_unknown(self):
        cases = {
            "compile-failed": ("COMPILE src/app.c:12: error C2065: missing\n", "compile_failure"),
            "dependencies-failed": ("DEPENDENCY libfoo missing\n", "dependency_failure"),
            "container-failed": ("CONTAINER build image_missing\n", "container_failure"),
            "timeout-failed": ("TIMEOUT test 30\n", "timeout"),
            "startup-failed": ("STARTUP web port_in_use\n", "startup_failure"),
        }
        for phase, (line, category) in cases.items():
            summary = summarize_failure(line, phase)
            self.assertEqual(summary["items"][0]["category"], category, phase)
            self.assertEqual(summary["outcome"], "failed", phase)
            self.assertFalse(summary["success"], phase)
            self.assertFalse(summary["runtime_evidence"], phase)
        empty = summarize_failure("this line is not a known grammar\n", "compile-failed")
        self.assertEqual(empty["evidence_state"], "unknown")
        self.assertIsNone(empty["outcome"])
        self.assertEqual(empty["items"][0]["category"], "unattributed")
        self.assertFalse(empty["success"])
        mixed = summarize_failure(
            "FAILED tests/test_alpha.py::test_one - no\nCOMPILE src/app.c:12: error C2065: x\n",
            "compile-failed",
        )
        self.assertEqual([item["category"] for item in mixed["items"]], ["compile_failure"])
        self.assertGreaterEqual(mixed["unparsed_count"], 1)

    def test_d03_retains_more_than_one_hundred_records_for_one_deployment(self):
        service = self.open_service(limits=Limits(max_records_per_segment=10))
        for number in range(1, 121):
            deployment = "dep_keep" if number <= 30 else "dep_other"
            stored = service.ingest_event(
                principal(),
                "app-a",
                event(number, deployment_id=deployment, idempotency_key=f"idem-bulk-{number}"),
            )
            self.assertEqual(stored["http"], 200, stored)
        page = service.list_records(principal(), "app-a", page_size=100)
        self.assertEqual(len(page["items"]), 100)
        self.assertIsNotNone(page["next_page_token"])
        rest = service.list_records(principal(), "app-a", page_size=100, page_token=page["next_page_token"])
        self.assertEqual(len(rest["items"]), 20)
        self.assertIsNone(rest["next_page_token"])
        kept = service.list_records(principal(), "app-a", deployment_id="dep_keep", page_size=100)
        self.assertEqual(len(kept["items"]), 30)
        self.assertTrue(list((self.root / "segments").glob("seg-*.jsonl")))

    def test_d03_rotation_keeps_early_records_and_a_missing_segment_is_a_gap(self):
        service = self.open_service(limits=Limits(max_records_per_segment=2))
        for number in range(1, 7):
            service.ingest_event(principal(), "app-a", event(number, idempotency_key=f"idem-rot-{number}"))
        listed = service.list_records(principal(), "app-a", page_size=20)
        self.assertEqual([item["producer_seq"] for item in listed["items"]], [1, 2, 3, 4, 5, 6])
        self.assertTrue((self.root / "segments" / "seg-000001.jsonl").exists())
        self.assertTrue((self.root / "segments" / "seg-000002.jsonl").exists())
        (self.root / "segments" / "seg-000002.jsonl").unlink()
        after = service.list_records(principal(), "app-a", page_size=20)
        self.assertIn(2, after["segment_gaps"])
        self.assertTrue(after["gap"])
        sequences = [item["producer_seq"] for item in after["items"]]
        self.assertEqual(sequences, [1, 2, 5, 6])
        self.assertNotIn(3, sequences)
        self.assertNotIn(4, sequences)

    def test_d04_unknown_operations_are_not_dispatched(self):
        denied = self.service.ingest_observation(
            principal(),
            "app-a",
            {"operation": "service.restart", "parameters": {}, "result": {"state": "running"}},
            {"idempotency_key": "idem-restart", "subject_id": "tsk_1", "run_id": "run_1"},
        )
        self.assertEqual(denied["http"], 422)
        self.assertFalse(denied["executed"])
        self.assertFalse(denied["body"]["dispatch_expansion"])
        self.assertFalse(denied["body"]["launchctl_invoked"])
        self.assertNotEqual(denied["body"].get("state"), "running")
        self.assertEqual(denied["intervention"]["category"], "unsupported_operation")
        self.assertTrue(denied["intervention"]["reason"])
        self.assertTrue(denied["intervention"]["repair_guidance"])
        self.assertEqual(self.probe.calls, [])
        parameterized = self.service.ingest_observation(
            principal(),
            "app-a",
            {"operation": "host.health", "parameters": {"force": True}, "result": {"system": "Darwin"}},
            {"idempotency_key": "idem-params", "subject_id": "tsk_1"},
        )
        self.assertEqual(parameterized["http"], 422)
        self.assertFalse(parameterized["executed"])

    def test_d04_platform_and_dispatch_switch_are_explicit(self):
        linux = self.service.ingest_observation(
            principal(),
            "app-a",
            {
                "operation": "service.inventory",
                "parameters": {},
                "system": "Linux",
                "result": [{"service_id": "macci.runner", "state": "running"}],
            },
            {"idempotency_key": "idem-linux", "subject_id": "tsk_1"},
        )
        states = {row["service_id"]: row for row in linux["body"]["services"]}
        self.assertEqual(set(states), {
            "macci.runner", "macci.preview", "macci.publish", "macci.receipts", "panel.merge",
        })
        self.assertTrue(all(row["state"] == "unsupported" for row in states.values()))
        self.assertTrue(all(row["launchctl_invoked"] is False for row in states.values()))
        self.assertTrue(states["macci.runner"]["claimed_state_rejected"])
        self.assertEqual(linux["body"]["dispatch_switch"]["evidence_state"], "missing")
        self.assertIsNone(linux["body"]["dispatch_switch"]["value"])
        unknown_platform = self.service.ingest_observation(
            principal(),
            "app-a",
            {"operation": "service.inventory", "parameters": {}, "result": []},
            {"idempotency_key": "idem-no-platform", "subject_id": "tsk_1"},
        )
        self.assertTrue(all(row["state"] == "unknown" for row in unknown_platform["body"]["services"]))
        self.assertEqual(unknown_platform["body"]["system_evidence"], "missing")

    def test_d05_cpu_memory_and_unobserved_services_are_not_invented(self):
        health = self.service.ingest_observation(
            principal(),
            "app-a",
            {
                "operation": "host.health",
                "parameters": {},
                "result": {"system": "Darwin", "cpu_percent": 99, "memory_percent": 50, "disk": None},
            },
            {"idempotency_key": "idem-health", "subject_id": "tsk_1"},
        )
        self.assertIsNone(health["body"]["cpu_percent"])
        self.assertIsNone(health["body"]["memory_percent"])
        self.assertEqual(health["body"]["cpu_evidence"], "missing")
        self.assertEqual(health["body"]["memory_evidence"], "missing")
        self.assertTrue(health["body"]["supplied_cpu_ignored"])
        self.assertFalse(health["body"]["runtime_evidence"])
        darwin = self.service.ingest_observation(
            principal(),
            "app-a",
            {
                "operation": "service.inventory",
                "parameters": {},
                "system": "Darwin",
                "result": [{"service_id": "macci.runner", "state": "running"}],
            },
            {"idempotency_key": "idem-darwin", "subject_id": "tsk_1"},
        )
        self.assertTrue(all(row["state"] == "unknown" for row in darwin["body"]["services"]))
        self.assertTrue(all(row["evidence_state"] == "missing" for row in darwin["body"]["services"]))
        self.assertIsNone(darwin["body"]["cpu_percent"])
        self.assertFalse(any(row["launchctl_invoked"] for row in darwin["body"]["services"]))

    def test_d08_human_intervention_has_reason_and_does_not_repair(self):
        path = self.write_log("startup.log", "STARTUP web schema_check_failed\n")
        stored = self.service.ingest_failure_log(
            principal(),
            "app-a",
            path,
            {
                "phase": "startup-failed",
                "idempotency_key": "idem-startup",
                "subject_id": "tsk_1",
                "task_id": "tsk_1",
                "run_id": "run_fix",
                "candidate_id": "cand_1",
                "release_id": "rel_1",
                "deployment_id": "dep_1",
                "occurred_at": "2026-11-28T01:00:00+00:00",
            },
        )
        guidance = stored["intervention"]
        self.assertEqual(guidance["code"], "UNKNOWN_EXECUTOR")
        self.assertFalse(guidance["automatic_claim"])
        self.assertFalse(guidance["automatic_repair"])
        self.assertFalse(guidance["writes_issue"])
        self.assertFalse(guidance["writes_comment"])
        self.assertEqual(stored["body"]["summary"]["items"][0]["reason_code"], "schema_check_failed")
        self.assertEqual(stored["body"]["summary"]["items"][0]["category"], "startup_failure")
        self.assertTrue(guidance["reason"])
        self.assertGreaterEqual(len(guidance["repair_guidance"]), 2)
        self.assertTrue(any("did not deploy" in line for line in guidance["repair_guidance"]))
        viewed = self.service.query_run(principal(), "app-a", "run_fix")
        self.assertEqual(viewed["human_intervention"]["code"], "UNKNOWN_EXECUTOR")
        self.assertFalse(viewed["side_effect"])
        self.assertEqual(self.probe.calls, [])

    def test_d09_run_query_correlates_ids_and_pages(self):
        for number in range(1, 6):
            self.service.ingest_event(
                principal(),
                "app-a",
                event(number, run_id="run_page", idempotency_key=f"idem-page-{number}", task_id="tsk_page"),
            )
        first = self.service.query_run(principal(), "app-a", "run_page", page_size=2)
        second = self.service.query_run(
            principal(), "app-a", "run_page", page_size=2, page_token=first["next_page_token"],
        )
        third = self.service.query_run(
            principal(), "app-a", "run_page", page_size=2, page_token=second["next_page_token"],
        )
        sequences = [item["producer_seq"] for item in first["items"] + second["items"] + third["items"]]
        self.assertEqual(sequences, [1, 2, 3, 4, 5])
        self.assertEqual(first["task"]["value"], "tsk_page")
        self.assertEqual(first["candidate"]["value"], "cand_1")
        self.assertEqual(first["release"]["value"], "rel_1")
        self.assertEqual(first["deployment"]["value"], "dep_1")
        self.assertEqual(first["task"]["authority"], "log_only")
        self.assertFalse(first["task"]["runtime_evidence"])
        self.assertFalse(first["side_effect"])
        self.assertEqual(first["deployment_outcome"]["evidence_state"], "missing")
        self.assertIsNone(first["deployment_outcome"]["state"])
        self.assertFalse(first["deployment_outcome"]["success"])

    def test_d09_poll_cursor_is_per_stream_and_push_subscribe_is_refused(self):
        for seq in (1, 2):
            self.service.ingest_event(
                principal(), "app-a", event(seq, stream_id="alpha:diagnostic", idempotency_key=f"idem-a-{seq}"),
            )
        self.service.ingest_event(
            principal(), "app-a", event(1, stream_id="beta:diagnostic", idempotency_key="idem-b-1"),
        )
        self.service.ingest_event(
            principal(), "app-a", event(3, stream_id="beta:diagnostic", idempotency_key="idem-b-3"),
        )
        broad = self.service.poll(principal(), "app-a", subject="tsk_1", after_seq=2)
        beta_seqs = [
            item.get("producer_seq") for item in broad["items"] if item.get("stream_id") == "beta:diagnostic"
        ]
        self.assertIn(1, beta_seqs)
        self.assertEqual(broad["ignored_after_seq"], 2)
        self.assertFalse(broad["after_seq_applied_globally"])
        followed = self.service.poll(
            principal(),
            "app-a",
            subject="tsk_1",
            cursors={"alpha:diagnostic": 2, "beta:diagnostic": 0},
        )
        alpha = [item for item in followed["items"] if item.get("stream_id") == "alpha:diagnostic" and item["classification"] == "applied"]
        beta = [item for item in followed["items"] if item.get("stream_id") == "beta:diagnostic"]
        self.assertEqual(alpha, [])
        self.assertTrue(any(item.get("producer_seq") == 1 for item in beta))
        self.assertTrue(followed["gap"])
        refused = self.service.subscribe(principal(), "app-a", subject="tsk_1")
        self.assertEqual(refused["http"], 405)
        self.assertFalse(refused["subscribe"])
        self.assertEqual(refused["mode"], "poll_cursor")
        self.assertFalse(refused["side_effect"])
        self.assertEqual(refused["items"], [])

    def test_out_of_order_events_leave_a_gap_until_filled_or_declared_absent(self):
        self.service.ingest_event(principal(), "app-a", event(1))
        self.service.ingest_event(principal(), "app-a", event(3))
        partial = self.service.poll(principal(), "app-a", subject="tsk_1")
        classes = {item["producer_seq"]: item["classification"] for item in partial["items"] if "producer_seq" in item}
        self.assertEqual(classes[1], "applied")
        self.assertEqual(classes[2], "gap")
        self.assertEqual(classes[3], "held")
        self.assertTrue(partial["gap"])
        self.assertEqual(partial["watermark_seq"], 1)
        self.service.ingest_event(
            principal(),
            "app-a",
            event(2, classification="never_existed", idempotency_key="idem-never"),
        )
        closed = self.service.poll(principal(), "app-a", subject="tsk_1")
        self.assertEqual(closed["watermark_seq"], 3)
        self.assertFalse(closed["gap"])
        absent = next(item for item in closed["items"] if item["producer_seq"] == 2)
        self.assertEqual(absent["evidence_state"], "missing")
        self.assertIsNone(absent["outcome"])
        self.assertFalse(absent["success"])
        self.service.ingest_event(
            principal(),
            "app-a",
            event(
                4,
                channel="authorization",
                event_class="authorization",
                classification="never_existed",
                idempotency_key="idem-auth-hole",
            ),
        )
        blocked = self.service.poll(principal(), "app-a", subject="tsk_1", after_seq=3)
        self.assertTrue(any(item["classification"] == "quarantined" for item in blocked["items"]))
        self.assertFalse(any(
            item["classification"] == "applied" and item.get("producer_seq") == 4 for item in blocked["items"]
        ))
        self.assertEqual(blocked["watermark_seq"], 3)

    def test_restart_reads_seq_back_and_lost_counter_does_not_fill_the_old_gap(self):
        self.service.ingest_event(principal(), "app-a", event(1, idempotency_key="idem-re-1"))
        self.service.ingest_event(principal(), "app-a", event(3, idempotency_key="idem-re-3"))
        self.service.ingest_observation(
            principal(),
            "app-a",
            {"operation": "host.health", "parameters": {}, "result": {"system": "Linux"}},
            {"idempotency_key": "idem-re-local", "subject_id": "tsk_1", "run_id": "run_1"},
        )
        reopened = self.open_service()
        self.assertEqual(reopened.journal.status()["journal_epoch"], 1)
        self.assertEqual(reopened.journal.status()["next_local_producer_seq"], 2)
        self.assertFalse(reopened.journal.status()["counter_loss"])
        still = reopened.poll(principal(), "app-a", subject="tsk_1", stream_id="fixture:diagnostic")
        self.assertTrue(any(item["classification"] == "gap" and item["producer_seq"] == 2 for item in still["items"]))
        reopened.ingest_event(principal(), "app-a", event(2, idempotency_key="idem-re-2"))
        filled = reopened.poll(principal(), "app-a", subject="tsk_1", stream_id="fixture:diagnostic")
        self.assertEqual(filled["watermark_seq"], 3)
        self.assertFalse(filled["gap"])
        reopened.ingest_event(
            principal(), "app-a", event(1, stream_id="gamma:diagnostic", idempotency_key="idem-g-1"),
        )
        reopened.ingest_event(
            principal(), "app-a", event(3, stream_id="gamma:diagnostic", idempotency_key="idem-g-3"),
        )
        (self.root / "meta.json").unlink()
        readonly = Journal.open(self.root, writable=False)
        self.assertTrue(readonly.counter_loss)
        self.assertFalse((self.root / "meta.json").exists())
        lost = self.open_service()
        self.assertTrue(lost.journal.status()["counter_loss"])
        self.assertEqual(lost.journal.status()["journal_epoch"], 2)
        lost.ingest_observation(
            principal(),
            "app-a",
            {"operation": "agent.snapshot", "parameters": {}, "result": {"capabilities": ["service.restart"]}},
            {"idempotency_key": "idem-re-new-epoch", "subject_id": "tsk_1", "run_id": "run_1"},
        )
        viewed = lost.poll(principal(), "app-a", subject="tsk_1")
        gamma_epoch = viewed["streams"]["gamma:diagnostic"]["epochs"]["1"]
        self.assertIn(2, gamma_epoch["missing_seq"])
        local_epoch = viewed["streams"]["w06:diagnostic"]["epochs"]["2"]
        self.assertEqual(local_epoch["watermark_seq"], 1)
        applied_hole = [
            item for item in viewed["items"]
            if item.get("stream_id") == "gamma:diagnostic" and item.get("producer_seq") == 2 and item["classification"] == "applied"
        ]
        self.assertEqual(applied_hole, [])

    def test_redaction_removes_secrets_from_the_journal_and_the_response(self):
        secret = (
            "github_pat_AAAABBBBCCCCDDDD postgres://app:hunter2@db.internal/prod "
            "APP_API_TOKEN=supersecret -----BEGIN PRIVATE KEY-----abc-----END PRIVATE KEY-----"
        )
        path = self.write_log("secret.log", secret + "\nCOMPILE src/app.c:3: error C1000: " + secret + "\n")
        stored = self.service.ingest_failure_log(
            principal(),
            "app-a",
            path,
            {
                "phase": "compile-failed",
                "idempotency_key": "idem-secret-log",
                "subject_id": "tsk_1",
                "run_id": "run_secret",
                "occurred_at": "2026-11-28T01:00:00+00:00",
            },
        )
        leaked = self.service.ingest_event(
            principal(),
            "app-a",
            event(
                9,
                idempotency_key="idem-secret-body",
                run_id="run_secret",
                body={"message": secret, "token": "github_pat_AAAABBBBCCCCDDDD"},
            ),
        )
        blob = journal_text(self.root) + json.dumps(stored) + json.dumps(leaked)
        for marker in ("github_pat_", "hunter2", "supersecret", "BEGIN PRIVATE KEY", "PRIVATE KEY"):
            self.assertNotIn(marker, blob)
        self.assertIn("compile_failure", json.dumps(stored))
        self.assertIn("omitted_fields", json.dumps(leaked["body"]))

    def test_cross_app_reads_do_not_leak_and_readers_cannot_ingest(self):
        self.service.ingest_event(
            principal("app-b"),
            "app-b",
            event(1, app_id="app-b", body={"message": "APP_B_ONLY_MARKER"}, idempotency_key="idem-b-secret"),
        )
        denied = self.service.poll(principal("app-a"), "app-b", subject="tsk_1")
        self.assertEqual(denied["http"], 403)
        self.assertEqual(denied["code"], "POLICY_DENIED")
        self.assertIsNone(denied["items"])
        self.assertNotIn("APP_B_ONLY_MARKER", json.dumps(denied))
        own = self.service.poll(principal("app-a"), "app-a", subject="tsk_1")
        self.assertEqual(own["items"], [])
        self.assertNotIn("APP_B_ONLY_MARKER", json.dumps(own))
        visible = self.service.poll(principal("app-b"), "app-b", subject="tsk_1")
        self.assertIn("APP_B_ONLY_MARKER", json.dumps(visible))
        before = fingerprint(self.root)
        self.authz.calls.clear()
        rejected = self.service.ingest_event(principal("app-a", "fixture-reader"), "app-a", event(2, idempotency_key="idem-reader"))
        self.assertEqual(rejected["http"], 403)
        self.assertEqual(fingerprint(self.root), before)
        self.assertEqual(self.probe.calls, [])

    def test_expired_and_unknown_evidence_are_not_success(self):
        self.service.ingest_event(
            principal(),
            "app-a",
            event(
                1,
                evidence_state="missing",
                body={"outcome": "success", "message": "claimed"},
                idempotency_key="idem-missing",
            ),
        )
        self.service.ingest_event(
            principal(),
            "app-a",
            event(
                2,
                evidence_state="unknown",
                body={"outcome": "COMMIT"},
                expires_at="2026-11-28T00:00:00+00:00",
                idempotency_key="idem-expired",
            ),
        )
        polled = self.service.poll(principal(), "app-a", subject="tsk_1")
        by_seq = {item["producer_seq"]: item for item in polled["items"]}
        self.assertEqual(by_seq[1]["evidence_state"], "missing")
        self.assertIsNone(by_seq[1]["outcome"])
        self.assertFalse(by_seq[1]["success"])
        self.assertEqual(by_seq[1]["rejected_outcome"], "success")
        self.assertEqual(by_seq[2]["evidence_state"], "expired")
        self.assertIsNone(by_seq[2]["outcome"])
        self.assertFalse(by_seq[2]["success"])
        expired_id = principal(expires_at="2026-11-28T00:00:00+00:00")
        denied = self.service.poll(expired_id, "app-a", subject="tsk_1")
        self.assertEqual(denied["evidence_state"], "expired")
        self.assertNotEqual(denied.get("outcome"), "success")

    def test_reads_have_no_side_effects(self):
        self.service.ingest_event(principal(), "app-a", event(1))
        before = fingerprint(self.root)
        self.service.poll(principal(), "app-a", subject="tsk_1")
        self.service.list_records(principal(), "app-a")
        self.service.query_run(principal(), "app-a", "run_1")
        self.service.subscribe(principal(), "app-a")
        self.service.summarize("FAILED tests/test_alpha.py::test_one - x\n", "pytest-failed")
        self.service.external_report("panel#13")
        self.service.flags()
        self.service.read_production_database()
        self.service.enable_production()
        self.assertEqual(fingerprint(self.root), before)
        self.assertEqual(self.probe.calls, [])

        def boom(*args, **kwargs):
            raise AssertionError("read appended to the diagnostic journal")

        self.service.journal.append = boom
        self.service.poll(principal(), "app-a", subject="tsk_1", after_seq=0)
        self.service.query_run(principal(), "app-a", "run_1")
        self.service.list_records(principal(), "app-a", page_size=1)
        self.service.subscribe(principal(), "app-a")

    def test_missing_interfaces_stay_blocked_and_diagnostics_cannot_override_them(self):
        self.service.ingest_event(
            principal(),
            "app-a",
            event(1, body={"deployment": {"state": "COMMIT"}, "candidate": {"state": "INTEGRATED"}, "message": "advisory"}),
        )
        viewed = self.service.query_run(principal(), "app-a", "run_1")
        self.assertEqual(viewed["interfaces"]["w01"]["status"], "BLOCKED")
        self.assertIsNone(viewed["interfaces"]["w01"]["events"])
        self.assertEqual(viewed["interfaces"]["w04"]["status"], "BLOCKED")
        self.assertEqual(viewed["interfaces"]["w07"]["status"], "BLOCKED")
        self.assertIsNone(viewed["interfaces"]["w07"]["events"])
        self.assertEqual(viewed["interfaces"]["w07"]["interface"], "pending")
        self.assertIsNone(viewed["deployment_outcome"]["state"])
        self.assertEqual(viewed["deployment_outcome"]["evidence_state"], "missing")
        self.assertFalse(viewed["deployment_outcome"]["success"])
        self.assertFalse(viewed["advisory_claims"]["deployment"]["can_override"])
        self.assertIsNone(viewed["schedule"]["state"])
        self.assertEqual(viewed["human_intervention"]["category"], "blocked_w07")
        before = len(self.service.journal.records()[0])
        self.service.query_run(principal(), "app-a", "run_1")
        self.assertEqual(len(self.service.journal.records()[0]), before)

    def test_synthetic_port_double_does_not_become_runtime_evidence(self):
        events = FixtureEventPort([
            {"event_class": "task", "app_id": "app-a", "run_id": "run_1", "task_id": "tsk_1", "state": "FAILED", "authoritative": True},
        ])
        deployment = FixtureDeploymentPort(state="RECONCILE", evidence_state="present")
        schedule = FixtureSchedulePort(state="UNKNOWN", evidence_state="unknown")
        service = self.open_service(events=events, deployment=deployment, schedule=schedule)
        service.ingest_event(
            principal(),
            "app-a",
            event(1, body={"deployment": {"state": "COMMIT"}, "message": "advisory"}),
        )
        before = fingerprint(self.root)
        viewed = service.query_run(principal(), "app-a", "run_1")
        again = service.query_run(principal(), "app-a", "run_1")
        self.assertEqual(viewed["deployment_outcome"]["state"], "RECONCILE")
        self.assertFalse(viewed["deployment_outcome"]["runtime_evidence"])
        self.assertTrue(viewed["deployment_outcome"]["synthetic"])
        self.assertEqual(viewed["schedule"]["state"], "UNKNOWN")
        self.assertFalse(viewed["schedule"]["success"])
        self.assertFalse(viewed["interfaces"]["w01"]["runtime_evidence"])
        self.assertEqual(events.calls, 2)
        self.assertEqual(fingerprint(self.root), before)
        self.assertFalse(again["side_effect"])

    def test_auth_failure_does_not_touch_idempotency_and_replay_does_not_append(self):
        self.authz.calls.clear()
        denied = self.service.ingest_event(
            {"kind": "agent", "id": "someone", "proof_ref": "test-proof://w06/app-a"},
            "app-a",
            event(1),
        )
        self.assertEqual(denied["http"], 403)
        self.assertEqual(denied["code"], "NOT_AUTHORIZED")
        self.assertEqual(self.authz.calls, ["authenticate"])
        self.assertEqual(self.service.journal.meta["idempotency"], {})
        first = self.service.ingest_event(principal(), "app-a", event(1))
        self.assertEqual(first["http"], 200)
        before = fingerprint(self.root)
        replay = self.service.ingest_event(principal(), "app-a", event(1))
        self.assertEqual(replay["code"], "IDEMPOTENCY_REPLAY")
        self.assertFalse(replay["side_effect"])
        self.assertEqual(fingerprint(self.root), before)
        mismatch = self.service.ingest_event(principal(), "app-a", event(1, body={"message": "other"}))
        self.assertEqual(mismatch["code"], "IDEMPOTENCY_PAYLOAD_MISMATCH")
        self.assertEqual(fingerprint(self.root), before)
        self.service.journal.append = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("append"))
        self.service.poll(principal(), "app-a", subject="tsk_1", route="real")

    def test_poll_empty_journal_matches_the_contract_shape(self):
        polled = self.service.poll(principal(), "app-a", subject="tsk_absent")
        for key in ("watermark_seq", "gap", "items", "side_effect"):
            self.assertIn(key, polled)
        self.assertEqual(polled["http"], 200)
        self.assertEqual(polled["watermark_seq"], 0)
        self.assertFalse(polled["gap"])
        self.assertEqual(polled["items"], [])
        self.assertFalse(polled["side_effect"])
        self.assertFalse(polled["subscribe"])
        self.assertEqual(polled["mode"], "poll_cursor")
        self.assertEqual(polled["method"], "GET")
        self.assertEqual(polled["path"], "/api/v1/control/diagnostics")

    def test_production_flags_stay_false_and_the_package_is_isolated(self):
        self.assertFalse(PRODUCTION_ENABLEMENT)
        self.assertFalse(PRODUCTION_VERIFICATION_ENABLED)
        self.assertFalse(PRODUCTION_DB_ACCESS)
        self.assertFalse(DISPATCH_EXPANSION)
        self.assertFalse(SUBSCRIBE_PUSH)
        self.assertFalse(MOUNTED_IN_APP_MAIN)
        flags = self.service.flags()
        self.assertFalse(flags["production_enablement"])
        self.assertFalse(flags["mounted_in_app_main"])
        refused = self.service.enable_production()
        self.assertEqual(refused["http"], 403)
        self.assertFalse(refused["enabled"])
        self.assertFalse(PRODUCTION_ENABLEMENT)
        database = self.service.read_production_database()
        self.assertEqual(database["code"], "PRODUCTION_DB_FORBIDDEN")
        self.assertIsNone(database["rows"])
        source = "\n".join(path.read_text(encoding="utf-8") for path in PACKAGE.glob("*.py"))
        for banned in (
            "import subprocess",
            "import sqlite3",
            "import app.main",
            "import app.operations",
            "deploy.macci",
            "deploy.hostops",
            "app.integration_queue",
            "app.control_events",
            "app.control_identity",
            "app.control_deploy",
            "app.control_scheduler",
            "app.control_authz",
        ):
            self.assertNotIn(banned, source)
        self.assertNotIn("control_diagnostics", (ROOT / "app" / "main.py").read_text(encoding="utf-8"))
        self.assertEqual(self.probe.calls, [])
