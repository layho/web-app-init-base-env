"""Isolated W10 unified task board. Synthetic ports and in-memory projection only.

The first run on the frozen ancestor fails because ``app.control_board`` does
not exist yet. Later runs must pass. Command:

PYTHONPATH=. python3 -m unittest tests.test_v060_w10_task_board -v

Test doubles are not GitHub, MacCI, merge, or deploy proof.
"""

import json
import threading
import unittest
from pathlib import Path

from app.control_board import (
    ACCEPTANCE_ENABLED,
    AUTHORITATIVE_MUTATION,
    GITHUB_EXECUTION,
    JOINT_INTEGRATION_READY,
    MACCI_PROOF,
    MOUNTED_IN_APP_MAIN,
    PHASES,
    PRODUCTION_DB_ACCESS,
    PRODUCTION_ENABLEMENT,
    PRODUCTION_VERIFICATION_ENABLED,
    REAL_DEPLOY,
    REAL_MERGE,
    SUBSCRIBE_PUSH,
    BlockedAuthorizationPort,
    BlockedDeliveryPort,
    BlockedDiagnosticPort,
    BlockedEventPort,
    BlockedSchedulePort,
    Board,
    FixtureAuthorizationPort,
    FixtureDeliveryPort,
    FixtureDiagnosticPort,
    FixtureEventPort,
    FixtureSchedulePort,
    Grant,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "tests" / "fixtures" / "v060" / "w00" / "contract.json"
REPO = "layho/web-app-init-base-env"


def principal(app_id="app-a", role="agent", pid=None, kind="test", proof=None):
    return {
        "kind": kind,
        "id": pid or f"fixture-{role}-{app_id}",
        "proof_ref": proof if proof is not None else f"test-proof://w10/{app_id}",
        "app_id": app_id,
        "role": role,
    }


def grants_for(*roles):
    items = []
    mapping = {
        "agent": [
            "board.read",
            "task.request_action",
            "schedule.intervene",
            "repair.authorize",
        ],
        "session": ["board.read", "task.request_action", "preview.merge"],
        "host": ["board.read", "deploy.plan"],
        "hostops": ["board.read"],
        "reader": ["board.read"],
    }
    for app_id, role in roles:
        for action in mapping[role]:
            items.append(Grant(f"fixture-{role}-{app_id}", app_id, action))
    return items


def event(seq, **overrides):
    item = {
        "event_id": f"evt-{seq}",
        "schema_version": 2,
        "event_type": "task.stage",
        "event_class": "task",
        "repo_id": REPO,
        "producer_id": "fixture",
        "source": "macci",
        "stream_id": "fixture:task",
        "stream_epoch": 1,
        "producer_seq": seq,
        "occurred_at": "2026-11-28T01:00:00Z",
        "idempotency_key": f"idem-w10-{seq}",
        "app_id": "app-a",
        "host_id": None,
        "task_id": "tsk_1",
        "run_id": "run_1",
        "attempt": 1,
        "revision": 1,
        "stage": "develop",
        "state": "SUCCEEDED",
        "owner": "owner-a",
        "executor": "agent-a",
        "schedule_mode": "scheduled",
    }
    item.update(overrides)
    return item


def candidate(seq, candidate_id, **overrides):
    item = event(
        seq,
        event_id=f"evt-c-{seq}",
        event_type="candidate.registered",
        event_class="candidate",
        source="policy_service",
        stream_id="fixture:candidate",
        stage=None,
        state="OPEN",
        candidate_id=candidate_id,
        head="a" * 40,
        base="b" * 40,
        merge="c" * 40,
        tree="d" * 40,
        pr_number=seq,
    )
    item.update(overrides)
    return item


def board_with(events=None, schedule=None, diagnostics=None, delivery=None, responses=None, extra_grants=()):
    auth = FixtureAuthorizationPort(grants_for(("app-a", "agent"), ("app-a", "session"), ("app-a", "host"), ("app-a", "hostops"), ("app-b", "agent")), responses=responses)
    for grant in extra_grants:
        auth.grants.append(grant)
    return Board(
        auth=auth,
        events=FixtureEventPort(events or []),
        schedule=FixtureSchedulePort(schedule or {"tasks": [], "chains": [], "rounds": []}),
        delivery=FixtureDeliveryPort(delivery or {}),
        diagnostics=FixtureDiagnosticPort(diagnostics or []),
    )


class TaskBoardTests(unittest.TestCase):
    def test_flags_stay_false_and_shared_entries_are_untouched(self):
        self.assertFalse(MOUNTED_IN_APP_MAIN)
        self.assertFalse(PRODUCTION_ENABLEMENT)
        self.assertFalse(PRODUCTION_VERIFICATION_ENABLED)
        self.assertFalse(PRODUCTION_DB_ACCESS)
        self.assertFalse(REAL_MERGE)
        self.assertFalse(REAL_DEPLOY)
        self.assertFalse(GITHUB_EXECUTION)
        self.assertFalse(MACCI_PROOF)
        self.assertFalse(JOINT_INTEGRATION_READY)
        self.assertFalse(ACCEPTANCE_ENABLED)
        self.assertFalse(AUTHORITATIVE_MUTATION)
        self.assertFalse(SUBSCRIBE_PUSH)
        board = board_with()
        flags = board.flags()
        for key in (
            "mounted_in_app_main",
            "production_enablement",
            "production_verification_enabled",
            "production_db_access",
            "real_merge",
            "real_deploy",
            "github_proof",
            "macci_proof",
            "joint_integration_ready",
            "acceptance_enabled",
            "authoritative_mutation",
            "subscribe_push",
        ):
            self.assertFalse(flags[key], key)
        denied = board.enable_production()
        self.assertEqual(denied["code"], "POLICY_DENIED")
        self.assertFalse(board.flags()["production_enablement"])
        database = board.read_production_database()
        self.assertEqual(database["code"], "PRODUCTION_DB_FORBIDDEN")
        self.assertIsNone(database["rows"])
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("control_board", main)
        self.assertNotIn("control_routes_w10", main)
        for name in ("DevelopmentTasksPage.tsx", "OperationsPage.tsx", "main.tsx"):
            text = (ROOT / "frontend" / "src" / name).read_text(encoding="utf-8")
            self.assertNotIn("control_board", text)
        diagnostics = (ROOT / "app" / "integration_diagnostics.py").read_text(encoding="utf-8")
        self.assertNotIn("control_board", diagnostics)

    def test_phases_follow_frozen_w00_contract(self):
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        self.assertEqual(list(PHASES), contract["entities"]["task"]["phases"])
        self.assertEqual(
            contract["object_projection"]["diagnostic_cannot_override"],
            ["authorization", "deployment", "candidate"],
        )
        self.assertFalse(contract["identity"]["production_verification_enabled"])
        self.assertFalse(contract["normative"] if "normative" in contract else False)

    def test_unified_lists_owner_executor_mode_stages_and_many_prs(self):
        events = [
            event(1, stage="develop", state="SUCCEEDED", schedule_mode="realtime"),
            event(2, event_id="evt-2", stage="test", state="FAILED", failure_reason="pytest assertion", schedule_mode="realtime"),
            candidate(1, "cand_a", pr_number=41, state="TESTING"),
            candidate(2, "cand_b", pr_number=42, state="OPEN"),
            event(
                3,
                event_id="evt-test",
                event_type="ci.test.finished",
                event_class="evidence",
                stream_id="macci:task",
                source="macci",
                stage="test",
                state="FAILED",
                candidate_id="cand_a",
                failure_reason="pytest assertion",
                evidence_ref="test-proof://w10/evidence/1",
                producer_seq=1,
            ),
        ]
        schedule = {
            "tasks": [{"task_id": "tsk_1", "app_id": "app-a", "closed": False}],
            "chains": [
                {"chain_id": "ch_a", "task_id": "tsk_1", "candidate_id": "cand_a", "priority": 10, "dependencies": ["ch_z"], "pr_line": "41"},
                {"chain_id": "ch_b", "task_id": "tsk_1", "candidate_id": "cand_b", "priority": 5, "dependencies": [], "pr_line": "42"},
            ],
            "rounds": [],
        }
        board = board_with(events, schedule)
        refreshed = board.refresh(principal(), "app-a", version="v060")
        self.assertFalse(refreshed["side_effect"])
        self.assertFalse(refreshed["authoritative_state_changed"])
        page = board.list_tasks(principal(), "app-a")
        self.assertEqual(page["http"], 200)
        self.assertEqual(len(page["items"]), 1)
        task = page["items"][0]
        self.assertEqual(task["owner"]["value"], "owner-a")
        self.assertEqual(task["owner"]["evidence"], "present")
        self.assertFalse(task["executor"]["verified"])
        self.assertEqual(task["executor"]["code"], "UNKNOWN_EXECUTOR")
        self.assertEqual(task["executor"]["claimed"], "agent-a")
        self.assertEqual(task["schedule_mode"]["value"], "realtime")
        self.assertEqual(task["stages"]["develop"]["at"], "2026-11-28T01:00:00Z")
        self.assertEqual(task["stages"]["develop"]["evidence"], "present")
        self.assertEqual(task["stages"]["review"]["evidence"], "missing")
        self.assertIsNone(task["stages"]["review"]["at"])
        self.assertEqual(task["lanes"]["development"]["state"], "recorded")
        self.assertEqual(task["lanes"]["test"]["state"], "failed")
        self.assertEqual(task["lanes"]["test"]["failure_reason"], "pytest assertion")
        self.assertEqual(task["lanes"]["merge"]["evidence"], "missing")
        self.assertEqual(task["lanes"]["deploy"]["evidence"], "missing")
        self.assertEqual(task["lanes"]["deploy"]["state"], "unrecorded")
        self.assertEqual([item["pr_number"] for item in task["prs"]], [41, 42])
        self.assertEqual(task["prs"][0]["priority"], 10)
        self.assertEqual(task["prs"][0]["dependencies"], ["ch_z"])
        self.assertEqual(task["prs"][1]["priority"], 5)
        self.assertEqual(task["schedule_evidence"], "synthetic")
        self.assertFalse(task["closure"]["closed"])
        self.assertIn("发布", task["closure"]["reason"])
        tests = board.list_tests(principal(), "app-a")
        self.assertEqual(len(tests["items"]), 1)
        self.assertEqual(tests["items"][0]["candidate_id"], "cand_a")
        self.assertEqual(tests["items"][0]["state"], "failed")
        prs = board.list_prs(principal(), "app-a", task_id="tsk_1")
        self.assertEqual(len(prs["items"]), 2)
        self.assertTrue(all(item["association"] == "explicit_task_id" for item in prs["items"]))
        text = board.render_text(principal(), "app-a")
        self.assertIn("统一任务看板", text)
        self.assertIn("owner-a", text)
        self.assertIn("未接通执行器", text)
        self.assertIn("人工干预", text)

    def test_missing_evidence_is_explicit_and_not_success(self):
        bare = event(
            1,
            owner=None,
            executor=None,
            schedule_mode=None,
            occurred_at=None,
            state="SUCCEEDED",
            source=None,
            stage="deploy",
        )
        failed = event(
            2,
            event_id="evt-fail",
            stage="test",
            state="FAILED",
            failure_reason=None,
            source="macci",
            occurred_at="2026-11-28T02:00:00Z",
            owner=None,
            executor=None,
            schedule_mode=None,
        )
        deployment = event(
            1,
            event_id="evt-dep",
            event_type="deployment.commit",
            event_class="deployment",
            source=None,
            stream_id="host:deployment",
            stage="deploy",
            state="COMMIT",
            producer_seq=1,
            owner=None,
            executor=None,
            schedule_mode=None,
        )
        board = board_with([bare, failed, deployment])
        board.refresh(principal(), "app-a", version="v060")
        task = board.task(principal(), "app-a", "tsk_1")
        self.assertEqual(task["owner"]["evidence"], "missing")
        self.assertEqual(task["owner"]["display"], "未记录")
        self.assertEqual(task["schedule_mode"]["evidence"], "missing")
        self.assertEqual(task["schedule_mode"]["display"], "未记录")
        self.assertEqual(task["executor"]["code"], "UNKNOWN_EXECUTOR")
        self.assertIsNone(task["executor"]["claimed"])
        self.assertEqual(task["stages"]["develop"]["evidence"], "missing")
        self.assertEqual(task["lanes"]["deploy"]["state"], "unknown")
        self.assertNotEqual(task["lanes"]["deploy"]["state"], "recorded")
        self.assertFalse(task["lanes"]["deploy"]["authoritative"])
        reasons = [item["display"] for item in task["failure_reasons"]]
        self.assertIn("未记录失败原因", reasons)
        self.assertTrue(any(item["evidence"] == "missing" for item in task["failure_reasons"]))
        self.assertFalse(task["closure"]["closed"])
        self.assertIn("未记录", board.render_text(principal(), "app-a"))

    def test_caller_verified_flag_and_received_at_do_not_become_authority(self):
        claimed = event(1, executor_verified=True, received_at="2026-11-28T00:00:00Z", state="SUCCEEDED")
        board = board_with([claimed])
        refreshed = board.refresh(principal(), "app-a", version="v060")
        task = board.task(principal(), "app-a", "tsk_1")
        self.assertFalse(task["executor"]["verified"])
        self.assertEqual(task["executor"]["code"], "UNKNOWN_EXECUTOR")
        self.assertEqual(task["stages"]["develop"]["evidence"], "missing")
        self.assertTrue(any("received_at" in str(item.get("reason")) for item in refreshed["rejected"]))
        history = board.history(principal(), "app-a", "tsk_1")
        self.assertEqual(history["items"], [])

    def test_diagnostic_cannot_override_protected_facts(self):
        deployment = event(
            1,
            event_id="evt-dep",
            event_type="deployment.commit",
            event_class="deployment",
            source="host_deployer",
            stream_id="host:deployment",
            stage="deploy",
            state="RECONCILE",
            revision=1,
            producer_seq=1,
        )
        diagnostic = event(
            2,
            event_id="evt-diag",
            event_type="diagnostic.observation",
            event_class="diagnostic",
            source="hostops_readonly",
            stream_id="hostops:diagnostic",
            stage="deploy",
            state="COMMIT",
            revision=99,
            producer_seq=1,
            failure_reason="deploy COMMIT success",
            candidate_id="cand_a",
        )
        same = event(
            2,
            event_id="evt-adv",
            event_type="task.stage",
            event_class="task",
            source=None,
            stream_id="advisory:task",
            stage="develop",
            state="FAILED",
            revision=1,
            producer_seq=1,
            failure_reason="advisory",
        )
        authoritative = event(1, stage="develop", state="SUCCEEDED", revision=1)
        board = board_with([deployment, diagnostic, authoritative, same])
        board.refresh(principal(), "app-a", version="v060")
        task = board.task(principal(), "app-a", "tsk_1")
        self.assertEqual(task["lanes"]["deploy"]["raw_state"], "RECONCILE")
        self.assertNotEqual(task["lanes"]["deploy"]["raw_state"], "COMMIT")
        self.assertEqual(task["lanes"]["development"]["raw_state"], "SUCCEEDED")
        self.assertTrue(task["advisory"]["success_claim_ignored"])
        self.assertTrue(any(item["authoritative"] is False for item in task["advisory_failures"]))
        self.assertNotEqual(task["lanes"]["deploy"]["state"], "recorded")

    def test_out_of_order_gaps_hold_one_stream_only(self):
        first = event(1, stage="develop", state="SUCCEEDED")
        third = event(3, event_id="evt-3", stage="review", state="SUCCEEDED")
        other = event(
            1,
            event_id="evt-other",
            stream_id="other:task",
            stage="test",
            state="FAILED",
            failure_reason="isolated",
            producer_seq=1,
        )
        auth_gap = event(
            2,
            event_id="evt-auth-2",
            event_type="authorization.granted",
            event_class="authorization",
            source="policy_service",
            stream_id="policy:authorization",
            producer_seq=2,
            stage="accept",
            state="GRANTED",
            operation="run.execute",
        )
        auth_epoch = event(
            1,
            event_id="evt-auth-e2",
            event_type="authorization.granted",
            event_class="authorization",
            source="policy_service",
            stream_id="policy:authorization",
            stream_epoch=2,
            producer_seq=1,
            stage="accept",
            state="GRANTED",
            operation="run.execute",
            epoch_closed=False,
        )
        diagnostic_epoch = event(
            1,
            event_id="evt-diag-e2",
            event_type="diagnostic.observation",
            event_class="diagnostic",
            source="hostops_readonly",
            stream_id="hostops:diagnostic",
            stream_epoch=2,
            producer_seq=1,
            state="OBSERVED",
            failure_reason="late diagnostic",
        )
        board = board_with([third, first, other, auth_gap, auth_epoch, diagnostic_epoch])
        refreshed = board.refresh(principal(), "app-a", version="v060")
        self.assertTrue(refreshed["gap"])
        task = board.task(principal(), "app-a", "tsk_1")
        self.assertEqual(task["stages"]["develop"]["evidence"], "present")
        self.assertEqual(task["stages"]["review"]["evidence"], "missing")
        self.assertEqual(task["lanes"]["test"]["failure_reason"], "isolated")
        self.assertFalse(task["executor"]["verified"])
        self.assertTrue(any(item["stream_id"] == "hostops:diagnostic" for item in task["advisory"]["items"]))
        held = {item["event_id"] for item in refreshed["held"]}
        self.assertIn("evt-3", held)
        self.assertIn("evt-auth-2", held)
        self.assertIn("evt-auth-e2", held)
        filler = event(2, event_id="evt-2", stage="review", state="SUCCEEDED")
        closer = event(
            1,
            event_id="evt-auth-1",
            event_type="authorization.granted",
            event_class="authorization",
            source="policy_service",
            stream_id="policy:authorization",
            producer_seq=1,
            stage="accept",
            state="GRANTED",
            operation="run.execute",
            executor="executor-verified",
            epoch_closed=True,
        )
        board.events.events.extend([filler, closer])
        again = board.refresh(principal(), "app-a", version="v060")
        task = board.task(principal(), "app-a", "tsk_1")
        self.assertEqual(task["stages"]["review"]["evidence"], "present")
        self.assertNotIn("evt-3", {item["event_id"] for item in again["held"]})
        self.assertTrue(task["executor"]["verified"])
        self.assertEqual(task["executor"]["claimed"], "executor-verified")
        self.assertIsNone(task["executor"]["code"])

    def test_authorization_never_existed_does_not_fill_a_gap(self):
        skip = event(
            1,
            event_id="evt-skip",
            event_type="authorization.granted",
            event_class="authorization",
            source="policy_service",
            stream_id="policy:authorization",
            producer_seq=1,
            classification="never_existed",
            operation="run.execute",
            state="GRANTED",
        )
        nxt = event(
            2,
            event_id="evt-next",
            event_type="authorization.granted",
            event_class="authorization",
            source="policy_service",
            stream_id="policy:authorization",
            producer_seq=2,
            operation="run.execute",
            state="GRANTED",
            executor="should-not-apply",
        )
        diagnostic = event(
            1,
            event_id="evt-dskip",
            event_type="diagnostic.observation",
            event_class="diagnostic",
            source="hostops_readonly",
            stream_id="hostops:diagnostic",
            producer_seq=1,
            classification="never_existed",
            state="OBSERVED",
        )
        following = event(
            2,
            event_id="evt-dfollow",
            event_type="diagnostic.observation",
            event_class="diagnostic",
            source="hostops_readonly",
            stream_id="hostops:diagnostic",
            producer_seq=2,
            state="OBSERVED",
            failure_reason="after hole",
        )
        board = board_with([skip, nxt, diagnostic, following])
        board.refresh(principal(), "app-a", version="v060")
        task = board.task(principal(), "app-a", "tsk_1")
        self.assertFalse(task["executor"]["verified"])
        self.assertIsNone(task["executor"]["claimed"])
        self.assertNotEqual(task["executor"]["claimed"], "should-not-apply")
        self.assertTrue(any(item["event_id"] == "evt-dfollow" for item in task["advisory"]["items"]))

    def test_history_keeps_superseded_rows_and_default_list_archives_them(self):
        live = candidate(1, "cand_live", pr_number=7, state="OPEN", task_id="tsk_live")
        old = candidate(2, "cand_old", pr_number=8, state="SUPERSEDED", task_id="tsk_old")
        old_task = event(1, event_id="evt-old", task_id="tsk_old", stream_id="old:task", stage="develop", state="SUCCEEDED")
        live_task = event(1, event_id="evt-live", task_id="tsk_live", stream_id="live:task", stage="develop", state="SUCCEEDED")
        board = board_with([live, old, old_task, live_task])
        board.refresh(principal(), "app-a", version="v060")
        current = board.list_tasks(principal(), "app-a")
        self.assertEqual([item["task_id"] for item in current["items"]], ["tsk_live"])
        self.assertGreaterEqual(current["archived_hidden"], 1)
        archived = board.list_tasks(principal(), "app-a", include_archived=True)
        ids = [item["task_id"] for item in archived["items"]]
        self.assertEqual(ids, ["tsk_live", "tsk_old"])
        hidden = next(item for item in archived["items"] if item["task_id"] == "tsk_old")
        self.assertTrue(hidden["archived"])
        self.assertEqual(hidden["archive_reason"], "superseded")
        history = board.history(principal(), "app-a", "tsk_old")
        self.assertIn("evt-c-2", [item["event_id"] for item in history["items"]])
        self.assertIn("evt-old", [item["event_id"] for item in history["items"]])
        again = board.refresh(principal(), "app-a", version="v060")
        self.assertFalse(again["deleted_event_ids"])
        self.assertIn("evt-c-2", board.retained_event_ids("app-a"))

    def test_pagination_is_stable_and_tokens_do_not_cross_apps(self):
        events = []
        for index in range(1, 26):
            events.append(event(
                1,
                event_id=f"evt-page-{index}",
                task_id=f"tsk_{index:02d}",
                stream_id=f"s{index}:task",
                producer_seq=1,
                idempotency_key=f"idem-page-{index}",
            ))
        events.append(event(
            1,
            event_id="evt-b",
            app_id="app-b",
            task_id="tsk_b",
            stream_id="b:task",
            owner="owner-b",
            producer_seq=1,
        ))
        board = board_with(events)
        who = principal()
        board.refresh(who, "app-a", version="v060")
        seen = []
        token = None
        for _ in range(3):
            page = board.list_tasks(who, "app-a", page_size=10, page_token=token)
            self.assertLessEqual(len(page["items"]), 10)
            seen.extend(item["task_id"] for item in page["items"])
            token = page["next_page_token"]
        self.assertEqual(seen, [f"tsk_{index:02d}" for index in range(1, 26)])
        self.assertIsNone(token)
        self.assertNotIn("tsk_b", seen)
        foreign = board.list_tasks(who, "app-a", page_token="v1:app-b:tsk_b")
        self.assertEqual(foreign["code"], "APP_MISMATCH")
        self.assertEqual(foreign["items"], [])
        self.assertNotIn("owner-b", json.dumps(foreign))
        denied = board.list_tasks(who, "app-b")
        self.assertEqual(denied["http"], 403)
        self.assertEqual(denied["code"], "POLICY_DENIED")
        self.assertEqual(denied["items"], [])
        other = principal("app-b")
        board.refresh(other, "app-b", version="v060")
        foreign_page = board.list_tasks(other, "app-b")
        self.assertEqual([item["task_id"] for item in foreign_page["items"]], ["tsk_b"])
        self.assertNotIn("tsk_01", [item["task_id"] for item in foreign_page["items"]])

    def test_history_pagination_retains_every_applied_event(self):
        events = [
            event(index, event_id=f"evt-h-{index}", stage="develop", state="SUCCEEDED", revision=index)
            for index in range(1, 8)
        ]
        board = board_with(events)
        board.refresh(principal(), "app-a", version="v060")
        token = None
        collected = []
        while True:
            page = board.history(principal(), "app-a", "tsk_1", page_size=3, page_token=token)
            collected.extend(item["event_id"] for item in page["items"])
            token = page["next_page_token"]
            if token is None:
                break
        self.assertEqual(collected, [f"evt-h-{index}" for index in range(1, 8)])

    def test_blocked_ports_are_not_an_empty_healthy_board(self):
        board = Board(
            auth=BlockedAuthorizationPort(),
            events=BlockedEventPort(),
            schedule=BlockedSchedulePort(),
            delivery=BlockedDeliveryPort(),
            diagnostics=BlockedDiagnosticPort(),
        )
        page = board.list_tasks(principal(), "app-a")
        self.assertEqual(page["code"], "INTERFACE_PENDING")
        self.assertEqual(page["evidence_state"], "missing")
        self.assertEqual(page["items"], [])
        self.assertFalse(page["production_healthy"])
        self.assertFalse(page["side_effect"])
        self.assertEqual(board.events.calls, [])

    def test_read_side_effect_is_rejected_and_not_applied(self):
        events = FixtureEventPort([event(1)])
        events.side_effect = True
        board = Board(
            auth=FixtureAuthorizationPort(grants_for(("app-a", "agent"))),
            events=events,
            schedule=FixtureSchedulePort({"tasks": [], "chains": [], "rounds": []}),
            delivery=FixtureDeliveryPort({}),
            diagnostics=FixtureDiagnosticPort([]),
        )
        refreshed = board.refresh(principal(), "app-a", version="v060")
        self.assertEqual(refreshed["code"], "SIDE_EFFECT_UNKNOWN")
        self.assertEqual(board.retained_event_ids("app-a"), [])
        task = board.task(principal(), "app-a", "tsk_1")
        self.assertEqual(task["code"], "EVIDENCE_MISSING")

    def test_button_permissions_and_manual_actions_do_not_change_state(self):
        events = [event(1, stage="develop", state="SUCCEEDED")]
        board = board_with(events)
        who = principal(role="agent")
        board.refresh(who, "app-a", version="v060")
        before = board.task(who, "app-a", "tsk_1")
        buttons = board.buttons(who, "app-a", "tsk_1")
        by_id = {item["id"]: item for item in buttons["buttons"]}
        self.assertFalse(by_id["execute_now"]["connected_executor"])
        self.assertIn("未接通执行器", by_id["execute_now"]["label"])
        self.assertTrue(by_id["nudge"]["enabled"])
        self.assertTrue(by_id["execute_now"]["enabled"])
        self.assertFalse(by_id["request_publish"]["enabled"])
        self.assertEqual(by_id["request_publish"]["code"], "POLICY_DENIED")
        self.assertFalse(by_id["deploy"]["enabled"])
        self.assertFalse(by_id["merge"]["enabled"])
        deceptive = board.submit(
            who, "app-a", action="execute_now", task_id="tsk_1", idempotency_key="idem-w10-exec",
        )
        self.assertEqual(deceptive["code"], "UNKNOWN_EXECUTOR")
        self.assertFalse(deceptive["accepted_as_success"])
        self.assertFalse(deceptive["authoritative_state_changed"])
        self.assertFalse(deceptive["connected_executor"])
        self.assertFalse(deceptive["port_response"]["running"])
        after = board.task(who, "app-a", "tsk_1")
        self.assertEqual(before["stages"], after["stages"])
        self.assertEqual(before["lanes"], after["lanes"])
        self.assertEqual(deceptive["projection_revision_before"], deceptive["projection_revision_after"])
        publish = board.submit(
            who, "app-a", action="request_publish", task_id="tsk_1", chain_id="ch_1", idempotency_key="idem-w10-pub",
        )
        self.assertEqual(publish["code"], "POLICY_DENIED")
        self.assertNotIn("request_publish", board.delivery.calls)
        hostops = principal(role="hostops")
        calls_before = len(board.auth.action_calls)
        forced = board.submit(
            hostops, "app-a", action="execute_now", task_id="tsk_1", idempotency_key="idem-w10-ops",
        )
        self.assertEqual(forced["code"], "NOT_AN_EXECUTOR")
        self.assertEqual(len(board.auth.action_calls), calls_before)
        missing = board.submit(
            who, "app-a", action="retry_phase", task_id="tsk_1", idempotency_key="idem-w10-retry",
        )
        self.assertEqual(missing["code"], "EVIDENCE_MISSING")
        self.assertEqual(board.schedule.intervene_calls, [])
        repair = board.submit(
            who, "app-a", action="authorize_repair", task_id="tsk_1", chain_id="cand_a",
            idempotency_key="idem-w10-repair", evidence_ref="test-proof://w10/repair", successor_id="cand_new",
        )
        self.assertEqual(board.schedule.intervene_calls, ["authorize_repair"])
        self.assertFalse(repair["accepted_as_success"])
        self.assertEqual(before["stages"], board.task(who, "app-a", "tsk_1")["stages"])
        session = principal(role="session")
        session_buttons = {item["id"]: item for item in board.buttons(session, "app-a", "tsk_1")["buttons"]}
        self.assertTrue(session_buttons["merge"]["enabled"])
        self.assertFalse(session_buttons["execute_now"]["enabled"])
        merged = board.submit(
            session, "app-a", action="merge", task_id="tsk_1", chain_id="cand_a", idempotency_key="idem-w10-merge",
        )
        self.assertEqual(board.delivery.calls, ["coordinate:merge"])
        self.assertFalse(merged["port_response"]["performed"])
        self.assertFalse(merged["port_response"]["real_merge"])
        self.assertFalse(merged["accepted_as_success"])
        view = board.render(who, "app-a")
        self.assertFalse(view["flags"]["mounted_in_app_main"])
        self.assertTrue(view["intervention"]["entry"] == "manual")
        self.assertFalse(view["intervention"]["connected_executor"])

    def test_deceptive_port_success_is_not_adopted(self):
        board = board_with(
            [event(1)],
            responses={"execute_now": {"http": 200, "state": "SUCCEEDED", "running": True, "ok": True, "performed": True}},
        )
        who = principal()
        board.refresh(who, "app-a", version="v060")
        result = board.submit(who, "app-a", action="execute_now", task_id="tsk_1", idempotency_key="idem-w10-lie")
        self.assertEqual(result["port_response"]["state"], "SUCCEEDED")
        self.assertFalse(result["accepted_as_success"])
        self.assertFalse(result["authoritative_state_changed"])
        task = board.task(who, "app-a", "tsk_1")
        self.assertNotEqual(task["lanes"]["development"]["raw_state"], "RUNNING")
        self.assertFalse(task["executor"]["verified"])
        second = board.submit(who, "app-a", action="execute_now", task_id="tsk_1", idempotency_key="idem-w10-lie")
        self.assertTrue(second["replayed"])
        self.assertEqual(len(board.auth.action_calls), 1)

    def test_cross_app_and_bad_identity_fail_closed(self):
        board = board_with([event(1, app_id="app-b", owner="secret-b")])
        outsider = principal("app-a")
        board.refresh(principal("app-b"), "app-b", version="v060")
        denied = board.list_tasks(outsider, "app-b")
        self.assertEqual(denied["code"], "POLICY_DENIED")
        self.assertNotIn("secret-b", json.dumps(denied))
        bad_kind = principal(kind="agent", proof="test-proof://w10/app-a")
        self.assertEqual(board.list_tasks(bad_kind, "app-a")["code"], "NOT_AUTHORIZED")
        bad_proof = principal(proof="github://token")
        self.assertEqual(board.list_tasks(bad_proof, "app-a")["code"], "NOT_AUTHORIZED")
        generous = FixtureAuthorizationPort(grants_for(("app-a", "agent")))
        generous.allow_any = True
        leaky = Board(
            auth=generous,
            events=FixtureEventPort([]),
            schedule=FixtureSchedulePort({"tasks": [], "chains": [], "rounds": []}),
            delivery=FixtureDeliveryPort({}),
            diagnostics=FixtureDiagnosticPort([]),
        )
        self.assertEqual(leaky.list_tasks(bad_kind, "app-a")["code"], "NOT_AUTHORIZED")

    def test_unscoped_events_are_counted_and_not_attached(self):
        loose = event(1, app_id=None, task_id="tsk_loose", owner="hidden", stream_id="loose:task", event_id="evt-loose")
        board = board_with([event(1), loose])
        refreshed = board.refresh(principal(), "app-a", version="v060")
        self.assertGreaterEqual(refreshed["unscoped_event_count"], 1)
        self.assertTrue(refreshed["unscoped_payloads_hidden"])
        page = board.list_tasks(principal(), "app-a")
        self.assertEqual([item["task_id"] for item in page["items"]], ["tsk_1"])
        blob = json.dumps(page)
        self.assertNotIn("tsk_loose", blob)
        self.assertNotIn("evt-loose", blob)
        self.assertTrue(all(item["owner"]["value"] != "hidden" for item in page["items"]))

    def test_snapshot_publish_claim_is_not_closure(self):
        schedule = {
            "tasks": [{"task_id": "tsk_1", "app_id": "app-a", "closed": True}],
            "chains": [{
                "chain_id": "ch_1",
                "task_id": "tsk_1",
                "candidate_id": "cand_1",
                "state": "PUBLISHED",
                "priority": 1,
                "dependencies": ["missing-dep"],
            }],
            "rounds": [],
        }
        board = board_with([], schedule)
        board.refresh(principal(), "app-a", version="v060")
        task = board.task(principal(), "app-a", "tsk_1")
        self.assertFalse(task["closure"]["closed"])
        self.assertEqual(task["schedule_evidence"], "synthetic")
        self.assertEqual(task["lanes"]["deploy"]["state"], "unrecorded")
        self.assertEqual(task["prs"][0]["claimed_state"], "PUBLISHED")
        self.assertEqual(task["prs"][0]["authoritative_state"], "unrecorded")
        self.assertFalse(task["next_step"]["actionable"])
        self.assertEqual(task["next_step"]["evidence"], "missing")

    def test_concurrent_refresh_and_submit_do_not_adopt_success(self):
        events = [event(index, event_id=f"evt-con-{index}", revision=index, stage="develop", state="SUCCEEDED") for index in range(1, 21)]
        board = board_with(
            events,
            responses={"nudge": {"http": 200, "state": "SUCCEEDED", "running": True, "ok": True}},
        )
        who = principal()
        errors = []
        barrier = threading.Barrier(6)

        def refresh():
            try:
                barrier.wait()
                board.refresh(who, "app-a", version="v060")
            except Exception as exc:  # noqa: BLE001 - the test records every worker failure
                errors.append(exc)

        def submit(index):
            try:
                barrier.wait()
                result = board.submit(
                    who, "app-a", action="nudge", task_id="tsk_1", idempotency_key=f"idem-w10-con-{index}",
                )
                if result["accepted_as_success"] or result["authoritative_state_changed"]:
                    errors.append(AssertionError("submit adopted success"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        workers = [threading.Thread(target=refresh) for _ in range(3)]
        workers.extend(threading.Thread(target=submit, args=(index,)) for index in range(3))
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(errors, [])
        task = board.task(who, "app-a", "tsk_1")
        self.assertEqual(task["lanes"]["development"]["raw_state"], "SUCCEEDED")
        self.assertFalse(task["executor"]["verified"])
        self.assertEqual(len(board.auth.action_calls), 3)
        ids = board.retained_event_ids("app-a")
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 20)

    def test_invalid_page_and_unknown_action_are_explicit(self):
        board = board_with([event(1)])
        who = principal()
        board.refresh(who, "app-a", version="v060")
        self.assertEqual(board.list_tasks(who, "app-a", page_size=0)["code"], "SCHEMA_UNSUPPORTED")
        self.assertEqual(board.list_tasks(who, "app-a", page_token="nope")["code"], "SCHEMA_UNSUPPORTED")
        unknown = board.submit(who, "app-a", action="hostops_execute", task_id="tsk_1", idempotency_key="idem-w10-no")
        self.assertEqual(unknown["code"], "NOT_AN_EXECUTOR")
        self.assertEqual(board.schedule.intervene_calls, [])
        self.assertEqual(board.delivery.calls, [])
        self.assertEqual(board.auth.action_calls, [])


if __name__ == "__main__":
    unittest.main()
