"""Isolated V0.6.0 W05 delivery loop.

Temporary directory and test doubles only. Doubles are not GitHub or MacCI
proof, and they do not authorize production publish or runtime user acceptance.
"""

import ast
import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.control_delivery import (
    AUTOMATIC_PUBLISH_AUTHORIZATION,
    FORGED_USER_ACCEPTANCE,
    GITHUB_EXECUTION,
    MACCI_PROOF,
    MODULE_NAME,
    MOUNTED_IN_APP_MAIN,
    PRODUCTION_DB_ACCESS,
    PRODUCTION_ENABLEMENT,
    PRODUCTION_VERIFICATION_ENABLED,
    REAL_DEPLOY,
    REAL_MERGE,
    ROUTE_MODULE,
)
from app.control_delivery.candidate import candidate_id
from app.control_delivery.ports import (
    BlockedAuth,
    BlockedDeploy,
    BlockedEvents,
    BlockedRemote,
    BlockedSchedule,
    FixtureAuth,
    FixtureDeploy,
    FixtureEvents,
    FixtureRemote,
    FixtureSchedule,
    ServerClock,
)
from app.control_delivery.projection import project_chain
from app.control_delivery.service import DeliveryService, operation_id_for

ROOT = Path(__file__).resolve().parents[1]


def sha(mark):
    return mark * 40


class IsolatedDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auth = FixtureAuth()
        self.events = FixtureEvents()
        self.schedule = FixtureSchedule()
        self.deploy = FixtureDeploy()
        self.remote = FixtureRemote()
        self.clock = ServerClock()
        self.svc = self._service()
        self.actor = {"kind": "test", "id": "fixture", "proof_ref": "test-proof://w05/fixture"}
        self.scope = {"repo": "layho/web-app-init-base-env", "task_id": "tsk_w05"}

    def _scope(self, task_id=None):
        scope = dict(self.scope)
        if task_id is not None:
            scope["task_id"] = task_id
        return scope

    def _feedback(self, feedback_id):
        return self.svc.feedback(feedback_id, actor=self.actor, scope=self.scope)

    def _arm_acceptance(self, task_id, chain_id, decision):
        chain = self._read_chain(task_id, chain_id)
        self.schedule.evidence_state[chain["candidate_id"]] = {
            "valid": True,
            "known": True,
            "candidate_id": chain["candidate_id"],
            "head": chain["head"],
            "base": chain["base"],
            "merge": chain["merge"],
            "tree": chain["tree"],
            "passed": decision == "accepted",
        }
        self.auth.acceptances[chain["candidate_id"]] = {
            "decision": decision,
            "candidate_id": chain["candidate_id"],
            "task_id": task_id,
            "chain_id": chain_id,
            "repo": chain["repo_id"],
            "head": chain["head"],
            "base": chain["base"],
            "merge": chain["merge"],
            "tree": chain["tree"],
            "principal_id": self.actor["id"],
            "test_double": True,
        }
        return chain

    def _service(self, **ports):
        values = {
            "auth": self.auth,
            "events": self.events,
            "schedule": self.schedule,
            "deploy": self.deploy,
            "remote": self.remote,
        }
        values.update(ports)
        return DeliveryService(self.tmp.name, clock=self.clock, **values)

    def _submit(self, feedback_id, author=None, key=None, *, expect_success=True):
        author = self.actor if author is None else author
        result = self.svc.submit_feedback(
            actor=author,
            scope=self.scope,
            author_id=author["id"],
            feedback_id=feedback_id,
            category="bug",
            body="body-" + feedback_id,
            idempotency_key=key or ("submit-" + feedback_id),
            expected_revision=0,
        )
        if expect_success:
            self.assertTrue(result.ok, result.as_dict())
        return result

    def _sync(self, *, credential=True, batch_limit=None, key="sync-1", expected_revision=0):
        return self.svc.sync_outbound(
            actor=self.actor,
            scope=self.scope,
            idempotency_key=key,
            credential_present=credential,
            batch_limit=batch_limit,
            expected_revision=expected_revision,
        )

    def _chain(self, chain_id, mark, **extra):
        spec = {
            "chain_id": chain_id,
            "pr_number": extra.pop("pr_number", 10),
            "required": extra.pop("required", True),
            "head": sha(mark),
            "base": sha("b"),
            "merge": sha("c"),
            "tree": sha("d"),
        }
        spec.update(extra)
        return spec

    def _claim_task(self, task_id="tsk_w05", chains=None, feedback_ids=None, key="claim-task"):
        result = self.svc.claim_task(
            actor=self.actor,
            scope=self._scope(task_id),
            task_id=task_id,
            feedback_ids=feedback_ids or [],
            chains=chains if chains is not None else [self._chain("ch_a", "a", pr_number=11)],
            idempotency_key=key,
            expected_revision=0,
        )
        self.assertTrue(result.ok, result.as_dict())
        return result

    def _read_chain(self, task_id, chain_id):
        return self.svc.store.read("chain", task_id + "__" + chain_id)

    def _bind(self, task_id, chain_id, *, passed=True, key=None):
        chain = self._read_chain(task_id, chain_id)
        result = self.svc.bind_test_evidence(
            actor=self.actor,
            scope=self._scope(task_id),
            task_id=task_id,
            chain_id=chain_id,
            head=chain["head"],
            base=chain["base"],
            merge=chain["merge"],
            tree=chain["tree"],
            passed=passed,
            run_id="run_test_1",
            idempotency_key=key or ("bind-" + chain_id),
            expected_revision=chain["revision"],
        )
        self.assertTrue(result.ok, result.as_dict())
        self.assertFalse(result.body["macci_proof"])
        self.assertEqual(result.body["proof_class"], "test_double")
        return result

    def _observe(self, task_id, chain_id, guard, key):
        chain = self._read_chain(task_id, chain_id)
        self.deploy.observations[chain["candidate_id"]] = {
            "guard": guard,
            "exact": True,
            "status": "observed",
            "candidate_id": chain["candidate_id"],
            "evidence_ref": "test-double://publish/" + chain["candidate_id"],
        }
        result = self.svc.apply_observation(
            actor=self.actor,
            scope=self._scope(task_id),
            task_id=task_id,
            chain_id=chain_id,
            idempotency_key=key,
            expected_revision=chain["revision"],
        )
        self.assertTrue(result.ok, result.as_dict())
        self.assertFalse(result.body["real_merge"])
        self.assertFalse(result.body["real_deploy"])
        return result

    def _to_accepting(self, task_id, chain_id):
        self._bind(task_id, chain_id)
        self._observe(task_id, chain_id, "exact_remote_observation", "obs-int-" + chain_id)
        self._observe(task_id, chain_id, "deployment_committed", "obs-dep-" + chain_id)
        chain = self._read_chain(task_id, chain_id)
        self.assertEqual(chain["state"], "ACCEPTING")
        return chain

    def _to_published(self, task_id, chain_id, decision="accepted"):
        chain = self._to_accepting(task_id, chain_id)
        self._arm_acceptance(task_id, chain_id, decision)
        recorded = self.svc.record_acceptance(
            actor=self.actor,
            scope=self._scope(task_id),
            task_id=task_id,
            chain_id=chain_id,
            signer_kind="user_acceptance",
            decision=decision,
            idempotency_key="accept-" + chain_id,
            expected_revision=chain["revision"],
        )
        self.assertTrue(recorded.ok, recorded.as_dict())
        self.assertFalse(recorded.body["counts_as_runtime_user_acceptance"])
        self.assertFalse(recorded.body["production_publish_authorized"])
        if decision != "accepted":
            return recorded
        chain = self._read_chain(task_id, chain_id)
        published = self._observe(task_id, chain_id, "formal_publish_evidence", "obs-pub-" + chain_id)
        self.assertEqual(published.body["state"], "PUBLISHED")
        self.assertFalse(published.body["publish_performed"])
        return published

    def test_f02_row_failure_continues_and_missing_credential_blocks(self):
        self._submit("fb_fail")
        self._submit("fb_ok")
        self.remote.fail_create.add("fb_fail")
        missing = self._sync(credential=False, key="sync-missing")
        self.assertFalse(missing.ok)
        self.assertEqual(missing.code, "CREDENTIAL_ABSENT")
        self.assertEqual(self.remote.calls, [])
        self.assertIsNone(self._feedback("fb_ok")["issue_number"])

        synced = self._sync(key="sync-rows", expected_revision=1)
        self.assertTrue(synced.ok, synced.as_dict())
        by_id = {row["feedback_id"]: row for row in synced.body["results"]}
        self.assertEqual(by_id["fb_fail"]["state"], "FAILED")
        self.assertEqual(by_id["fb_ok"]["state"], "SYNCED")
        self.assertTrue(by_id["fb_ok"]["created"])
        self.assertIsNotNone(self._feedback("fb_ok")["issue_number"])
        self.assertIsNone(self._feedback("fb_fail")["issue_number"])
        creates = [call for call in self.remote.calls if call[0] == "create"]
        self.assertEqual(creates, [("create", "fb_fail"), ("create", "fb_ok")])

    def test_f02_duplicate_issue_and_cross_author_hijack(self):
        self._submit("fb_dup")
        first = self._sync(key="sync-dup")
        self.assertTrue(first.ok, first.as_dict())
        number = self._feedback("fb_dup")["issue_number"]
        row = self._feedback("fb_dup")
        row["state"] = "PENDING"
        self.svc.store.write("feedback", "fb_dup", row)
        cursor = self.svc.store.read("cursor", "outbound")
        second = self._sync(key="sync-dup-2", expected_revision=cursor["revision"])
        self.assertTrue(second.ok, second.as_dict())
        self.assertFalse(second.body["results"][0]["created"])
        self.assertEqual(self._feedback("fb_dup")["issue_number"], number)
        self.assertEqual([call for call in self.remote.calls if call[0] == "create"], [("create", "fb_dup")])

        other = {"kind": "test", "id": "other", "proof_ref": "test-proof://w05/other"}
        hijack = self.svc.submit_feedback(
            actor=other,
            scope=self.scope,
            author_id="other",
            feedback_id="fb_dup",
            category="bug",
            body="taken",
            idempotency_key="submit-hijack",
            expected_revision=0,
        )
        self.assertFalse(hijack.ok)
        self.assertEqual(hijack.code, "NOT_AUTHORIZED")
        self.assertEqual(hijack.body["reason"], "cross_author")
        self.assertEqual(self._feedback("fb_dup")["body"], "body-fb_dup")

        self._submit("fb_owned")
        self.remote.issues["fb_owned"] = {
            "number": 44,
            "author_id": "intruder",
            "body": "foreign",
            "feedback_id": "fb_owned",
        }
        cursor = self.svc.store.read("cursor", "outbound")
        owned = self._sync(key="sync-owned", expected_revision=cursor["revision"])
        owned_row = {row["feedback_id"]: row for row in owned.body["results"]}["fb_owned"]
        self.assertEqual(owned_row["reason"], "cross_author")
        self.assertIsNone(self._feedback("fb_owned")["issue_number"])
        self.assertNotIn(("create", "fb_owned"), self.remote.calls)

    def test_claim_recovers_original_lease_after_lost_acknowledgement(self):
        for elapsed in (3, 61):
            with self.subTest(elapsed=elapsed):
                feedback = f"fb_crash_{elapsed}"
                self._submit(feedback)
                args = dict(actor=self.actor, scope=self.scope, feedback_id=feedback,
                            idempotency_key=feedback, expected_revision=1)
                append = self.events.append

                def lost_ack(event):
                    append(event)
                    raise RuntimeError("lost acknowledgement")

                self.events.append = lost_ack
                with self.assertRaisesRegex(RuntimeError, "lost acknowledgement"):
                    self.svc.claim_feedback_row(**args)
                self.events.append = append
                pending, = [o for o in self.svc.store.list("outbox") if o.get("pending_lease")]
                lease = pending["pending_lease"]
                self.clock.advance(elapsed)
                self.svc = self._service()
                recovered = self.svc.claim_feedback_row(**args)
                self.assertTrue(recovered.ok, recovered.as_dict())
                self.assertEqual(self.svc.store.read("feedback", feedback)["lease"], lease)
                self.assertNotIn("token", self._feedback(feedback)["lease"])
                self.assertEqual(recovered.body["lease_token"], lease["token"] if elapsed < 60 else None)
                self.assertNotIn("pending_lease", self.svc.store.read("outbox", pending["event_id"]))
                self.assertTrue(self.svc.claim_feedback_row(**args).replayed)
                events = [e for e in self.events.events if e["event_id"] == pending["event_id"]]
                self.assertEqual(len(events), 1)
                self.assertNotIn(lease["token"], str(events))

    def test_f02_row_claim_fence(self):
        self._submit("fb_lease")
        first = self.svc.claim_feedback_row(
            actor=self.actor, scope=self.scope, feedback_id="fb_lease",
            idempotency_key="lease-1", expected_revision=1,
        )
        self.assertTrue(first.ok, first.as_dict())
        stale = first.body["lease_token"]
        second = self.svc.claim_feedback_row(
            actor=self.actor, scope=self.scope, feedback_id="fb_lease",
            idempotency_key="lease-2", expected_revision=first.body["revision"],
        )
        self.assertTrue(second.ok, second.as_dict())
        self.assertNotEqual(second.body["lease_token"], stale)
        pushed = self.svc.push_claimed(
            actor=self.actor, scope=self.scope, feedback_id="fb_lease", token=stale,
            idempotency_key="push-stale", expected_revision=second.body["revision"],
            credential_present=True,
        )
        self.assertFalse(pushed.ok)
        self.assertEqual(pushed.code, "LEASE_FENCE_REJECTED")
        self.assertNotIn(("create", "fb_lease"), self.remote.calls)
        current = self.svc.push_claimed(
            actor=self.actor, scope=self.scope, feedback_id="fb_lease",
            token=second.body["lease_token"], idempotency_key="push-current",
            expected_revision=second.body["revision"], credential_present=True,
        )
        self.assertTrue(current.ok, current.as_dict())
        self.assertEqual(current.body["state"], "SYNCED")
        replay = self.svc.claim_feedback_row(
            actor=self.actor, scope=self.scope, feedback_id="fb_lease",
            idempotency_key="lease-2", expected_revision=second.body["revision"],
        )
        self.assertTrue(replay.replayed)
        self.assertIsNone(replay.body["lease_token"])
        self.assertEqual(replay.code, "IDEMPOTENCY_REPLAY")

    def test_f02_duplicate_and_out_of_order_comments(self):
        self._submit("fb_notes")
        synced = self._sync(key="sync-notes")
        self.assertTrue(synced.ok, synced.as_dict())
        number = self._feedback("fb_notes")["issue_number"]
        self.remote.comments[number] = [
            {"comment_id": "c2", "seq": 2, "author_id": "fixture", "body": "new", "status": "coding"},
            {"comment_id": "c1", "seq": 1, "author_id": "fixture", "body": "old", "status": "stale"},
            {"comment_id": "c2", "seq": 2, "author_id": "fixture", "body": "new", "status": "coding"},
            {"comment_id": "c2", "seq": 9, "author_id": "fixture", "body": "rewritten", "status": "hijack"},
            {"comment_id": "c9", "seq": 4, "author_id": "intruder", "body": "mine", "status": "accepted"},
        ]
        inbound = self.svc.sync_inbound(
            actor=self.actor, scope=self.scope, feedback_id="fb_notes",
            idempotency_key="in-notes", expected_revision=2, per_page=10,
        )
        self.assertTrue(inbound.ok, inbound.as_dict())
        self.assertEqual(inbound.body["progress_seq"], 2)
        self.assertEqual(inbound.body["progress_status"], "coding")
        self.assertEqual(inbound.body["out_of_order"], ["c1"])
        reasons = {item["comment_id"]: item["reason"] for item in inbound.body["rejected_comments"]}
        self.assertEqual(reasons["c2"], "payload_mismatch")
        self.assertEqual(reasons["c9"], "cross_author")
        self.assertNotEqual(self._feedback("fb_notes")["progress_status"], "accepted")

    def test_f03_batch_and_page_budget_do_not_drop_or_pretend_complete(self):
        for feedback_id in ("fb_a", "fb_b", "fb_c"):
            self._submit(feedback_id)
        limited = self._sync(batch_limit=1, key="sync-batch")
        self.assertTrue(limited.ok, limited.as_dict())
        self.assertFalse(limited.body["truncated_as_complete"])
        self.assertEqual(limited.body["continuation"], "fb_b")
        self.assertCountEqual(limited.body["remaining"], ["fb_b", "fb_c"])
        self.assertEqual(
            sorted(row["feedback_id"] for row in self.svc.store.list("feedback")),
            ["fb_a", "fb_b", "fb_c"],
        )
        cursor = self.svc.store.read("cursor", "outbound")
        rest = self._sync(key="sync-rest", expected_revision=cursor["revision"])
        self.assertTrue(rest.ok, rest.as_dict())
        self.assertIsNone(rest.body["continuation"])
        number = self._feedback("fb_a")["issue_number"]
        self.remote.comments[number] = [
            {"comment_id": "p1", "seq": 1, "author_id": "fixture", "body": "one", "status": "noted"},
            {"comment_id": "p2", "seq": 2, "author_id": "fixture", "body": "two", "status": "later"},
        ]
        partial = self.svc.sync_inbound(
            actor=self.actor, scope=self.scope, feedback_id="fb_a",
            idempotency_key="in-partial", expected_revision=self._feedback("fb_a")["revision"],
            page_budget=1, per_page=1,
        )
        self.assertFalse(partial.ok)
        self.assertEqual(partial.code, "INCOMPLETE_PAGE")
        self.assertFalse(partial.body["complete"])
        self.assertFalse(partial.body["inbound_complete"])
        self.assertEqual(partial.body["retained_ids"], ["fb_a", "fb_b", "fb_c"])
        self.assertEqual(
            sorted(row["feedback_id"] for row in self.svc.store.list("feedback")),
            ["fb_a", "fb_b", "fb_c"],
        )

    def test_f06_multiple_prs_and_completion_condition(self):
        self._submit("fb_multi")
        claimed = self._claim_task(
            chains=[
                self._chain("ch_a", "a", pr_number=21),
                self._chain("ch_b", "e", pr_number=22),
            ],
            feedback_ids=["fb_multi"],
            key="claim-multi",
        )
        self.assertEqual(claimed.body["chain_ids"], ["ch_a", "ch_b"])
        self.assertIn("PUBLISHED", claimed.body["completion_condition"])
        self.assertIn("task_acceptance", claimed.body["completion_condition"])
        self._to_published("tsk_w05", "ch_a")
        early = self.svc.close_task(
            actor=self.actor, scope=self.scope, task_id="tsk_w05",
            idempotency_key="close-early", expected_revision=1,
        )
        self.assertFalse(early.ok)
        self.assertEqual(early.code, "DEPENDENCY_UNSATISFIED")
        self.assertFalse(early.body["feedback_closed"])
        self.assertFalse(early.body["completion"]["met"])
        self._to_published("tsk_w05", "ch_b")
        still = self.svc.close_task(
            actor=self.actor, scope=self.scope, task_id="tsk_w05",
            idempotency_key="close-no-task-acceptance", expected_revision=1,
        )
        self.assertFalse(still.ok)
        self.assertIn("task_acceptance_missing", still.body["completion"]["reasons"])
        accepted = self.svc.record_task_acceptance(
            actor=self.actor, scope=self.scope, task_id="tsk_w05",
            signer_kind="user_acceptance", idempotency_key="task-accept",
            expected_revision=1,
        )
        self.assertTrue(accepted.ok, accepted.as_dict())
        self.assertFalse(accepted.body["counts_as_runtime_user_acceptance"])
        closed = self.svc.close_task(
            actor=self.actor, scope=self.scope, task_id="tsk_w05",
            idempotency_key="close-ok", expected_revision=accepted.body["revision"],
        )
        self.assertTrue(closed.ok, closed.as_dict())
        self.assertEqual(closed.body["state"], "close")
        self.assertFalse(closed.body["runtime_closed"])
        self.assertFalse(closed.body["production_publish_authorized"])
        self.assertTrue(self._feedback("fb_multi")["closed"])
        view = self.svc.project(actor=self.actor, scope=self.scope, task_id="tsk_w05")
        self.assertEqual(view.body["milestone_source"], "required_chains")
        self.assertFalse(view.body["single_developer_pr_decides_task"])
        self.assertEqual(len(view.body["chains"]), 2)

    def test_f06_superseded_pr_excluded_from_failures(self):
        self._claim_task(chains=[
            self._chain("ch_live", "a", pr_number=31),
            self._chain("ch_old", "e", pr_number=32, closed_superseded=True),
        ], key="claim-fail")
        self._bind("tsk_w05", "ch_live", passed=False, key="bind-live-fail")
        self._bind("tsk_w05", "ch_old", passed=False, key="bind-old-fail")
        view = self.svc.project(actor=self.actor, scope=self.scope, task_id="tsk_w05")
        self.assertEqual([row["chain_id"] for row in view.body["failures"]], ["ch_live"])

    def test_f07_tuple_change_invalidates_evidence(self):
        chains = []
        for index, field in enumerate(("head", "base", "merge", "tree")):
            chains.append(self._chain("ch_" + field, "a", pr_number=40 + index))
        self._claim_task(chains=chains, key="claim-tuple")
        for field in ("head", "base", "merge", "tree"):
            chain_id = "ch_" + field
            self._bind("tsk_w05", chain_id, key="bind-" + field)
            chain = self._read_chain("tsk_w05", chain_id)
            mutated = {
                "head": chain["head"],
                "base": chain["base"],
                "merge": chain["merge"],
                "tree": chain["tree"],
            }
            mutated[field] = sha("f")
            result = self.svc.observe_candidate(
                actor=self.actor, scope=self.scope, task_id="tsk_w05", chain_id=chain_id,
                idempotency_key="stale-" + field, expected_revision=chain["revision"], **mutated,
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "CANDIDATE_STALE")
            self.assertEqual(result.body["changed_field"], field)
            self.assertFalse(result.body["evidence_valid"])
            self.assertFalse(result.body["prior_evidence_reused"])
            stored = self._read_chain("tsk_w05", chain_id)
            self.assertEqual(stored["state"], "SUPERSEDED")
            self.assertFalse(stored["test_evidence"]["valid"])

    def test_f07_test_evidence_can_become_invalid(self):
        self._claim_task(key="claim-evidence")
        bound = self._bind("tsk_w05", "ch_a")
        chain = self._read_chain("tsk_w05", "ch_a")
        self.schedule.evidence_state[chain["candidate_id"]] = {
            "valid": False,
            "candidate_id": chain["candidate_id"],
            "head": chain["head"],
            "base": chain["base"],
            "merge": chain["merge"],
            "tree": chain["tree"],
        }
        refreshed = self.svc.refresh_test_evidence(
            actor=self.actor, scope=self.scope, task_id="tsk_w05", chain_id="ch_a",
            idempotency_key="refresh-evidence", expected_revision=bound.body["revision"],
        )
        self.assertFalse(refreshed.ok)
        self.assertEqual(refreshed.code, "EVIDENCE_MISSING")
        self.assertFalse(refreshed.body["evidence_valid"])
        self.assertFalse(refreshed.body["macci_proof"])
        blocked = self.svc.coordinate(
            actor=self.actor, scope=self.scope, task_id="tsk_w05", chain_id="ch_a",
            action="merge", idempotency_key="merge-stale-evidence",
            expected_revision=refreshed.body["revision"],
        )
        self.assertEqual(blocked.code, "EVIDENCE_MISSING")
        self.assertFalse(blocked.body["real_merge"])

    def test_f07_no_legacy_accepted_fallback_gap_or_blocked_override(self):
        self._claim_task(chains=[self._chain(
            "ch_a", "a", legacy_overall_status="accepted", historical_blocked=True,
        )], key="claim-legacy")
        view = self.svc.project(actor=self.actor, scope=self.scope, task_id="tsk_w05")
        chain = view.body["chains"][0]
        self.assertIsNone(chain["acceptance_decision"])
        self.assertFalse(chain["fell_back_to_legacy_accepted"])
        self.assertEqual(chain["legacy_overall_status_ignored"], "accepted")
        self.assertFalse(chain["blocked_overrode_phase"])
        self.assertNotEqual(chain["phase"], "blocked")
        projected = project_chain({
            "chain_id": "gap",
            "pr_number": 1,
            "state": "INTEGRATING",
            "acceptance_decision": None,
            "legacy_overall_status": "accepted",
            "historical_blocked": True,
            "recorded_steps": {"tests": True},
            "evidence_valid": True,
            "candidate_id": "cand_gap",
            "draft": False,
            "closed_superseded": False,
        })
        claim = next(step for step in projected["steps"] if step["key"] == "claim")
        self.assertEqual(claim["state"], "unrecorded")
        self.assertEqual(projected["phase"], "integrate")
        self.assertFalse(projected["fell_back_to_legacy_accepted"])

    def test_f07_rejection_stays_rejected(self):
        self._claim_task(key="claim-reject")
        self._to_accepting("tsk_w05", "ch_a")
        chain = self._arm_acceptance("tsk_w05", "ch_a", "rejected")
        rejected = self.svc.record_acceptance(
            actor=self.actor, scope=self.scope, task_id="tsk_w05", chain_id="ch_a",
            signer_kind="user_acceptance", decision="rejected",
            idempotency_key="reject-1", expected_revision=chain["revision"],
        )
        self.assertTrue(rejected.ok, rejected.as_dict())
        self.assertEqual(rejected.body["state"], "REJECTED")
        chain = self._read_chain("tsk_w05", "ch_a")
        again = self.svc.record_acceptance(
            actor=self.actor, scope=self.scope, task_id="tsk_w05", chain_id="ch_a",
            signer_kind="user_acceptance", decision="accepted",
            idempotency_key="reject-overwrite", expected_revision=chain["revision"],
        )
        self.assertFalse(again.ok)
        self.assertEqual(again.body["acceptance_decision"], "rejected")
        self.assertFalse(again.body["forged_user_acceptance"])
        self.assertEqual(self._read_chain("tsk_w05", "ch_a")["state"], "REJECTED")
        forged = self.svc.record_acceptance(
            actor=self.actor, scope=self.scope, task_id="tsk_w05", chain_id="ch_a",
            signer_kind="agent", decision="accepted",
            idempotency_key="reject-agent", expected_revision=chain["revision"],
        )
        self.assertEqual(forged.code, "POLICY_DENIED")
        self.assertEqual(self._read_chain("tsk_w05", "ch_a")["acceptance_decision"], "rejected")

    def test_f08_draft_freeze_is_not_acceptance_and_equal_tree_stays_frozen(self):
        self._claim_task(chains=[self._chain("ch_draft", "a", draft=True, pr_number=51)], key="claim-draft")
        frozen = self.svc.coordinate(
            actor=self.actor, scope=self.scope, task_id="tsk_w05", chain_id="ch_draft",
            action="merge", idempotency_key="merge-draft", expected_revision=1,
        )
        self.assertEqual(frozen.code, "FREEZE_ACTIVE")
        self.assertEqual(frozen.body["reason"], "draft")
        self.assertFalse(frozen.body["freeze_is_acceptance"])
        self.assertIsNone(frozen.body["acceptance_decision"])
        self.assertFalse(frozen.body["performed"])
        self.assertEqual(self.schedule.reservations, [])

        self._claim_task(
            task_id="tsk_tree",
            chains=[self._chain("ch_tree", "a", pr_number=52)],
            key="claim-tree",
        )
        scope = dict(self.scope)
        scope["task_id"] = "tsk_tree"
        bound = self._bind("tsk_tree", "ch_tree", key="bind-tree")
        self.schedule.freeze_state = {
            "draft": False,
            "equal_tree": True,
            "official_merge_observed": False,
            "blocked": False,
        }
        held = self.svc.coordinate(
            actor=self.actor, scope=scope, task_id="tsk_tree", chain_id="ch_tree",
            action="merge", idempotency_key="merge-tree",
            expected_revision=bound.body["revision"],
        )
        self.assertEqual(held.code, "FREEZE_ACTIVE")
        self.assertEqual(held.body["reason"], "equal_tree_without_official_merge")
        self.assertFalse(held.body["equal_tree_unfreezes"])
        self.assertIsNone(self._read_chain("tsk_tree", "ch_tree")["acceptance_decision"])

    def test_f08_merge_deploy_protocol_only_and_publish_denied(self):
        self.auth.allow_publish = True
        self._claim_task(key="claim-protocol")
        bound = self._bind("tsk_w05", "ch_a")
        merge = self.svc.coordinate(
            actor=self.actor, scope=self.scope, task_id="tsk_w05", chain_id="ch_a",
            action="merge", idempotency_key="proto-merge", expected_revision=bound.body["revision"],
        )
        self.assertTrue(merge.ok, merge.as_dict())
        self.assertFalse(merge.body["performed"])
        self.assertFalse(merge.body["real_merge"])
        self.assertEqual(len(self.schedule.reservations), 1)
        self.assertEqual(self._read_chain("tsk_w05", "ch_a")["state"], "INTEGRATING")
        deploy = self.svc.coordinate(
            actor=self.actor, scope=self.scope, task_id="tsk_w05", chain_id="ch_a",
            action="deploy", idempotency_key="proto-deploy", expected_revision=bound.body["revision"],
        )
        self.assertTrue(deploy.ok, deploy.as_dict())
        self.assertFalse(deploy.body["real_deploy"])
        self.assertEqual(self.deploy.plans[0]["real_host"], False)
        self.assertEqual(self.deploy.real_host, False)
        publish = self.svc.request_publish(
            actor=self.actor, scope=self.scope, task_id="tsk_w05", chain_id="ch_a",
            idempotency_key="publish-denied", expected_revision=bound.body["revision"],
        )
        self.assertFalse(publish.ok)
        self.assertEqual(publish.code, "POLICY_DENIED")
        self.assertFalse(publish.body["allow"])
        self.assertFalse(publish.body["production_publish_authorized"])
        self.assertIn(("authorize", "release.publish"), self.auth.calls)

    def test_f09_unknown_remote_requires_reconcile_before_retry(self):
        self._submit("fb_unknown")
        self.remote.unknown_create.add("fb_unknown")
        synced = self._sync(key="sync-unknown")
        self.assertTrue(synced.ok, synced.as_dict())
        row = synced.body["results"][0]
        self.assertEqual(row["state"], "UNKNOWN")
        self.assertFalse(row["retry_allowed"])
        operation_id = row["operation_id"]
        denied = self.svc.retry_operation(
            actor=self.actor, scope=self.scope, operation_id=operation_id,
            idempotency_key="retry-unknown", expected_revision=1,
        )
        self.assertEqual(denied.code, "SIDE_EFFECT_UNKNOWN")
        self.assertFalse(denied.body["guessed_success"])
        self.assertEqual([call for call in self.remote.calls if call[0] == "create"], [("create", "fb_unknown")])
        guessed = self.svc.reconcile_operation(
            actor=self.actor, scope=self.scope, operation_id=operation_id,
            idempotency_key="reconcile-missing", expected_revision=1,
        )
        self.assertEqual(guessed.code, "SIDE_EFFECT_UNKNOWN")
        self.assertEqual(self._feedback("fb_unknown")["state"], "UNKNOWN")
        stored_operation = self.svc.store.read("operation", operation_id)
        self.remote.reconcile_result[operation_id] = {
            "classification": "absent",
            "operation_id": operation_id,
            "author_id": "fixture",
            "repo": stored_operation["scope"]["repo"],
            "scope": stored_operation["scope"],
            "request_digest": stored_operation["request_digest"],
        }
        reconciled = self.svc.reconcile_operation(
            actor=self.actor, scope=self.scope, operation_id=operation_id,
            idempotency_key="reconcile-absent", expected_revision=1,
        )
        self.assertTrue(reconciled.ok, reconciled.as_dict())
        self.assertEqual(reconciled.body["state"], "RETRY_WAIT")
        self.assertFalse(reconciled.body["same_run_resumed"])
        self.assertEqual(self._feedback("fb_unknown")["state"], "RETRY_WAIT")
        same_run = self.svc.retry_operation(
            actor=self.actor, scope=self.scope, operation_id=operation_id,
            idempotency_key="retry-same-run", expected_revision=reconciled.body["revision"],
        )
        self.assertFalse(same_run.ok)
        self.assertFalse(same_run.body["same_run_resumed"])
        self.remote.unknown_create.discard("fb_unknown")
        continued = self.svc.continue_after_absent(
            actor=self.actor, scope=self.scope, feedback_id="fb_unknown",
            idempotency_key="continue-new-run",
            expected_revision=self._feedback("fb_unknown")["revision"],
            credential_present=True,
        )
        self.assertTrue(continued.ok, continued.as_dict())
        self.assertEqual(continued.body["state"], "SYNCED")
        self.assertFalse(continued.body["same_run_resumed"])
        self.assertTrue(continued.body["run_id"])
        self.assertNotEqual(continued.body["run_id"], continued.body["previous_run_id"])
        self.assertEqual(
            [call for call in self.remote.calls if call[0] == "create"],
            [("create", "fb_unknown"), ("create", "fb_unknown")],
        )
        actions = [item["action"] for item in self.svc.traces(actor=self.actor, scope=self.scope)]
        self.assertIn("operation.reconcile", actions)
        self.assertIn("feedback.continue", actions)

    def test_f09_snapshot_does_not_regress_terminal(self):
        merged = self.svc.merge_pr_snapshot(
            actor=self.actor, scope=self.scope, task_id="tsk_w05",
            history=[{"number": 202, "state": "merged", "revision": 3}],
            active=[{"number": 202, "state": "open", "revision": 4}],
            idempotency_key="snap-1",
        )
        self.assertTrue(merged.ok, merged.as_dict())
        self.assertFalse(merged.body["mtime_wins"])
        self.assertEqual(merged.body["prs"][0]["state"], "merged")
        self.assertEqual(merged.body["kept_terminal"], [202])

    def test_f10_acceptance_issue_does_not_close_feedback(self):
        self._submit("fb_ack")
        self._claim_task(feedback_ids=["fb_ack"], key="claim-ack")
        ack = self.svc.acknowledge_acceptance_issue(
            actor=self.actor, scope=self.scope, task_id="tsk_w05", issue_number=70,
            idempotency_key="ack-1", expected_revision=1,
        )
        self.assertTrue(ack.ok, ack.as_dict())
        self.assertTrue(ack.body["acceptance_issue_closed"])
        self.assertFalse(ack.body["feedback_closed"])
        self.assertFalse(ack.body["business_feedback_closed_by_acknowledge"])
        self.assertFalse(self._feedback("fb_ack")["closed"])
        self.assertEqual(self.svc.store.read("task", "tsk_w05")["phase"], "claim")

    def test_r08_repair_does_not_forge_acceptance(self):
        self._claim_task(chains=[self._chain("ch_a", "a", draft=True)], key="claim-repair")
        self._to_accepting("tsk_w05", "ch_a")
        chain = self._arm_acceptance("tsk_w05", "ch_a", "rejected")
        rejected = self.svc.record_acceptance(
            actor=self.actor, scope=self.scope, task_id="tsk_w05", chain_id="ch_a",
            signer_kind="user_acceptance", decision="rejected",
            idempotency_key="repair-reject", expected_revision=chain["revision"],
        )
        self.assertEqual(rejected.body["state"], "REJECTED")
        draft = self.svc.authorize_repair(
            actor=self.actor, scope=self.scope, task_id="tsk_w05", chain_id="ch_a",
            new_chain=self._chain("ch_new", "f", pr_number=81), decision="draft",
            idempotency_key="repair-draft", expected_revision=rejected.body["revision"],
        )
        self.assertEqual(draft.code, "FREEZE_ACTIVE")
        self.assertFalse(draft.body["freeze_is_acceptance"])
        self.assertEqual(self._read_chain("tsk_w05", "ch_a")["state"], "REJECTED")
        chain = self._read_chain("tsk_w05", "ch_a")
        chain["draft"] = False
        self.svc.store.write("chain", "tsk_w05__ch_a", chain)
        marker = self.svc.authorize_repair(
            actor=self.actor, scope=self.scope, task_id="tsk_w05", chain_id="ch_a",
            new_chain=self._chain("ch_new", "f", pr_number=81), decision=None,
            idempotency_key="repair-marker", expected_revision=chain["revision"],
        )
        self.assertEqual(marker.code, "POLICY_DENIED")
        self.assertFalse(marker.body["owner_marker_is_sufficient"])
        self.assertEqual(marker.body["acceptance_decision"], "rejected")
        forged = self.svc.authorize_repair(
            actor=self.actor, scope=self.scope, task_id="tsk_w05", chain_id="ch_a",
            new_chain=self._chain("ch_new", "f", pr_number=81), decision="accepted",
            idempotency_key="repair-forge", expected_revision=chain["revision"],
        )
        self.assertEqual(forged.code, "POLICY_DENIED")
        self.assertFalse(forged.body["forged_user_acceptance"])
        self.assertEqual(self._read_chain("tsk_w05", "ch_a")["acceptance_decision"], "rejected")
        self.auth.preauth[("repair.replace", chain["candidate_id"])] = {"policy": "ordinary"}
        opened = self.svc.authorize_repair(
            actor=self.actor, scope=self.scope, task_id="tsk_w05", chain_id="ch_a",
            new_chain=self._chain("ch_new", "f", pr_number=81), decision=None,
            idempotency_key="repair-open", expected_revision=chain["revision"],
        )
        self.assertTrue(opened.ok, opened.as_dict())
        self.assertEqual(opened.body["old_acceptance_decision"], "rejected")
        self.assertIsNone(opened.body["new_acceptance_decision"])
        self.assertEqual(opened.body["new_state"], "OPEN")
        self.assertNotEqual(opened.body["new_candidate_id"], chain["candidate_id"])
        self.assertEqual(self._read_chain("tsk_w05", "ch_a")["state"], "SUPERSEDED")
        self.assertEqual(self._read_chain("tsk_w05", "ch_a")["acceptance_decision"], "rejected")
        self.assertIsNone(self._read_chain("tsk_w05", "ch_new")["acceptance_decision"])

    def test_blocked_ports_do_not_advance_or_count_as_proof(self):
        blocked = self._service(
            auth=BlockedAuth(),
            events=BlockedEvents(),
            schedule=BlockedSchedule(),
            deploy=BlockedDeploy(),
            remote=BlockedRemote(),
        )
        result = blocked.submit_feedback(
            actor=self.actor, scope=self.scope, author_id="fixture", feedback_id="fb_blocked",
            category="bug", body="x", idempotency_key="blocked-1", expected_revision=0,
        )
        self.assertTrue(result.blocked)
        self.assertEqual(result.code, "INTERFACE_PENDING")
        self.assertIsNone(blocked.store.read("feedback", "fb_blocked"))
        self._submit("fb_event")
        quiet = self._service(events=BlockedEvents())
        denied = quiet.submit_feedback(
            actor=self.actor, scope=self.scope, author_id="fixture", feedback_id="fb_quiet",
            category="bug", body="y", idempotency_key="blocked-event", expected_revision=0,
        )
        self.assertTrue(denied.pending)
        self.assertFalse(denied.body["github_proof"])
        self.assertFalse(denied.body["macci_proof"])
        self.assertIsNone(quiet.store.read("feedback", "fb_quiet"))

    def test_auth_failure_skips_authorize_and_idempotency(self):
        self.auth.authenticated = False
        result = self.svc.submit_feedback(
            actor=self.actor, scope=self.scope, author_id="fixture", feedback_id="fb_auth",
            category="bug", body="z", idempotency_key="auth-1", expected_revision=0,
        )
        self.assertEqual(result.code, "NOT_AUTHORIZED")
        self.assertEqual(self.auth.calls, [("authenticate", "fixture")])
        self.assertEqual(self.svc.store.list("idem"), [])
        self.assertEqual(self.svc.store.list("feedback"), [])

    def test_candidate_id_matches_w00_vector(self):
        vector = json.loads((ROOT / "tests/fixtures/v060/w00/contract.json").read_text(encoding="utf-8"))
        spec = vector["entities"]["candidate"]["id"]["vector"]
        self.assertEqual(
            candidate_id(spec["repo_id"], spec["task_id"], spec["head"], spec["base"], spec["merge"], spec["tree"]),
            spec["candidate_id"],
        )

    def test_package_flags_and_imports_stay_isolated(self):
        self.assertEqual(MODULE_NAME, "app.control_delivery")
        self.assertEqual(ROUTE_MODULE, "app.control_routes_w05")
        self.assertFalse(MOUNTED_IN_APP_MAIN)
        self.assertFalse(PRODUCTION_ENABLEMENT)
        self.assertFalse(PRODUCTION_VERIFICATION_ENABLED)
        self.assertFalse(PRODUCTION_DB_ACCESS)
        self.assertFalse(REAL_MERGE)
        self.assertFalse(REAL_DEPLOY)
        self.assertFalse(GITHUB_EXECUTION)
        self.assertFalse(MACCI_PROOF)
        self.assertFalse(FORGED_USER_ACCEPTANCE)
        self.assertFalse(AUTOMATIC_PUBLISH_AUTHORIZATION)
        self.assertFalse((ROOT / "app/control_routes_w05.py").exists())
        banned = (
            "app.db", "app.main", "app.feedback_progress", "app.operations",
            "app.integration_queue", "app.control_events", "app.control_identity",
            "app.control_authz", "app.control_scheduler", "app.control_deploy",
            "app.control_diagnostics", "deploy.macci", "urllib", "subprocess", "sqlalchemy",
        )
        for path in (ROOT / "app/control_delivery").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    for item in banned:
                        self.assertNotIn(item, name)

    def test_p0_intent_before_create_and_missing_local_is_not_absence(self):
        self._submit("fb_intent")
        seen = {}

        def create_issue(payload):
            seen["record"] = self.svc.store.read("operation", payload["operation_id"])
            seen["payload"] = dict(payload)
            return FixtureRemote.create_issue(self.remote, payload)

        self.remote.create_issue = create_issue
        synced = self._sync(key="sync-intent")
        self.assertTrue(synced.ok, synced.as_dict())
        record = seen["record"]
        self.assertIsNotNone(record)
        self.assertEqual(record["state"], "INTENT")
        self.assertEqual(record["scope"], {"repo": self.scope["repo"], "task_id": self.scope["task_id"]})
        self.assertTrue(record.get("request_digest"))
        self.assertEqual(seen["payload"].get("request_digest"), record["request_digest"])
        self.assertFalse(record.get("github_proof", False))

        self._submit("fb_gap")
        adopted = {}

        def reconcile(operation_id):
            adopted["operation_id"] = operation_id
            _op, digest, scope_exact = operation_id_for(
                self.scope,
                "feedback.create_issue",
                {"feedback_id": "fb_gap", "author_id": "fixture", "body": "body-fb_gap", "attempt": 1},
            )
            return {
                "ok": True,
                "blocked": False,
                "classification": "succeeded",
                "operation_id": operation_id,
                "issue_number": 77,
                "author_id": "fixture",
                "feedback_id": "fb_gap",
                "repo": self.scope["repo"],
                "scope": scope_exact,
                "request_digest": digest,
                "test_double": True,
                "github_proof": False,
            }

        self.remote.reconcile = reconcile
        self.remote.calls.clear()
        cursor = self.svc.store.read("cursor", "outbound")
        gap = self._sync(key="sync-gap", expected_revision=cursor["revision"])
        self.assertTrue(gap.ok, gap.as_dict())
        self.assertEqual([call for call in self.remote.calls if call[0] == "create"], [])
        self.assertTrue(adopted.get("operation_id"))
        stored = self.svc.store.read("feedback", "fb_gap")
        self.assertEqual(stored["state"], "SYNCED")
        self.assertEqual(stored["issue_number"], 77)
        self.assertFalse(stored.get("github_proof", False))

    def test_p0_crash_after_remote_success_does_not_create_again(self):
        self._submit("fb_crash")
        numbers = {}

        def create_issue(payload):
            result = FixtureRemote.create_issue(self.remote, payload)
            self.remote.issues.pop(payload["feedback_id"], None)
            numbers["issue"] = result["number"]
            self.remote.reconcile_result[payload["operation_id"]] = {
                "classification": "succeeded",
                "operation_id": payload["operation_id"],
                "issue_number": result["number"],
                "author_id": payload["author_id"],
                "feedback_id": payload["feedback_id"],
                "repo": payload["scope"]["repo"],
                "scope": payload["scope"],
                "request_digest": payload["request_digest"],
            }
            raise RuntimeError("crash before local result")

        self.remote.create_issue = create_issue
        with self.assertRaises(RuntimeError):
            self._sync(key="sync-crash")
        self.assertEqual(self.svc.store.read("feedback", "fb_crash")["state"], "PENDING")
        self.assertIsNotNone(self.svc.store.read("operation", list(self.remote.reconcile_result)[0]))
        self.remote.create_issue = lambda payload: FixtureRemote.create_issue(self.remote, payload)
        self.remote.calls.clear()
        cursor = self.svc.store.read("cursor", "outbound")
        expected = 0 if cursor is None else cursor["revision"]
        recovered = self._sync(key="sync-crash-recover", expected_revision=expected)
        self.assertTrue(recovered.ok, recovered.as_dict())
        self.assertEqual([call for call in self.remote.calls if call[0] == "create"], [])
        stored = self.svc.store.read("feedback", "fb_crash")
        self.assertEqual(stored["state"], "SYNCED")
        self.assertEqual(stored["issue_number"], numbers["issue"])

    def test_p0_concurrent_push_creates_once(self):
        self._submit("fb_once")
        row = self.svc.store.read("feedback", "fb_once")
        row["state"] = "PENDING"
        row["lease"] = None
        self.svc.store.write("feedback", "fb_once", row)
        entered = []
        release = threading.Event()

        def create_issue(payload):
            entered.append(payload["feedback_id"])
            if len(entered) == 1:
                release.wait(2)
            return FixtureRemote.create_issue(self.remote, payload)

        self.remote.create_issue = create_issue
        barrier = threading.Barrier(2)
        errors = []

        def worker(key):
            try:
                barrier.wait(2)
                self.svc.sync_outbound(
                    actor=self.actor, scope=self.scope, idempotency_key=key,
                    credential_present=True, expected_revision=0,
                )
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=("sync-race-%s" % index,)) for index in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        release.set()
        for thread in threads:
            thread.join(5)
        self.assertEqual(errors, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(entered, ["fb_once"])
        self.assertEqual(self.svc.store.read("feedback", "fb_once")["state"], "SYNCED")

    def test_reconcile_requires_unambiguous_exact_event(self):
        self._submit("fb_exact")
        event = self.events.events[-1]
        original = self.events.query
        for changes in ({"items": [{**event, "note": "other-intent"}]},
                        {"items": [event, event]}, {"authority": "other"},
                        {"classification": "absent"}, {"ok": False}):
            def query(**kwargs):
                return {**original(**kwargs), **changes}
            self.events.query = query
            with self.subTest(changes=list(changes)):
                self.assertIsNone(self.svc._dispatch(event))
        self.assertEqual(len(self.events.events), 1)

    def test_p0_outbox_crash_recovers_exact_receipt_once(self):
        class CrashEvents(FixtureEvents):
            def __init__(self):
                super().__init__()
                self.crash = True
                self.calls = 0

            def append(self, event):
                self.calls += 1
                result = super().append(event)
                if self.crash:
                    raise RuntimeError("crash before local receipt")
                return result

        self.events = CrashEvents()
        self.svc = self._service()
        with self.assertRaises(RuntimeError):
            self._submit("fb_evt")
        self.assertIsNone(self.svc.store.read("feedback", "fb_evt"))
        self.assertEqual(self.events.calls, 1)
        self.events.crash = False
        again = self._submit("fb_evt")
        self.assertTrue(again.ok, again.as_dict())
        self.assertEqual(self.events.calls, 1)
        self.assertEqual(self.svc.store.read("feedback", "fb_evt")["state"], "PENDING")
        receipts = [
            item for item in self.svc.store.list("outbox")
            if item.get("receipt") and item["receipt"].get("authority") == "W01"
        ]
        self.assertEqual(len(receipts), 1)
        self.assertFalse(receipts[0].get("local_is_authority", False))
        self.assertFalse(receipts[0]["receipt"].get("w01_proof", False))

    def test_pending_outbox_for_different_intent_cannot_commit(self):
        append = self.events.append

        def crash(event):
            append(event)
            raise RuntimeError("crash before local commit")

        self.events.append = crash
        with self.assertRaisesRegex(RuntimeError, "crash before"):
            self._submit("fb_evt")
        outbox, = self.svc.store.list("outbox")
        outbox["event"]["note"] = "w05-intent-v1:" + "0" * 64
        self.svc.store.write("outbox", outbox["event_id"], outbox)
        self.events.append = append

        result = self._submit("fb_evt", expect_success=False)
        self.assertEqual(result.http, 409)
        self.assertEqual(result.body["reason"], "outbox_intent_mismatch")
        self.assertIsNone(self.svc.store.read("feedback", "fb_evt"))

    def test_p0_concurrent_claim_is_cas_and_does_not_duplicate_events(self):
        self._submit("fb_cas")
        original = self.svc.store.read
        reads = {"n": 0}

        def slow_read(kind, key):
            value = original(kind, key)
            if kind == "feedback" and key == "fb_cas":
                reads["n"] += 1
                if reads["n"] <= 2:
                    time.sleep(0.2)
            return value

        self.svc.store.read = slow_read
        barrier = threading.Barrier(2)
        results = []

        def worker(key):
            barrier.wait(2)
            results.append(self.svc.claim_feedback_row(
                actor=self.actor, scope=self.scope, feedback_id="fb_cas",
                idempotency_key=key, expected_revision=1,
            ))

        threads = [threading.Thread(target=worker, args=("cas-%s" % index,)) for index in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(1 for result in results if result.ok and not result.replayed), 1)
        self.assertEqual(sum(1 for result in results if result.code == "CONFLICT_REVISION"), 1)
        self.assertEqual(self.svc.store.read("feedback", "fb_cas")["revision"], 2)
        claimed = [
            event for event in self.events.events
            if event.get("event_type") == "feedback.claimed"
        ]
        self.assertEqual(len(claimed), 1)

    def test_p0_local_commit_is_atomic(self):
        self.svc.store.crash_before_commit = True
        with self.assertRaises(RuntimeError):
            self._submit("fb_atomic")
        self.assertIsNone(self.svc.store.read("feedback", "fb_atomic"))
        self.assertEqual(self.svc.store.list("idem"), [])
        self.assertEqual(self.svc.store.list("outbox"), [])
        self.svc.store.crash_before_commit = False
        created = self._submit("fb_atomic")
        self.assertTrue(created.ok, created.as_dict())
        self.assertEqual(self.svc.store.read("feedback", "fb_atomic")["revision"], 1)

    def test_p0_cross_repo_forged_scope_batch_and_unauthenticated_reads(self):
        self._claim_task(key="claim-bind-scope")
        self._bind("tsk_w05", "ch_a", key="bind-scope")
        forged_task = dict(self.scope)
        forged_task["task_id"] = "tsk_other"
        forged = self.svc.coordinate(
            actor=self.actor, scope=forged_task, task_id="tsk_w05", chain_id="ch_a",
            action="merge", idempotency_key="forged-scope",
            expected_revision=self._read_chain("tsk_w05", "ch_a")["revision"],
        )
        self.assertEqual(forged.code, "NOT_AUTHORIZED")
        self.assertEqual(self.schedule.reservations, [])
        self.assertEqual(self._read_chain("tsk_w05", "ch_a")["state"], "INTEGRATING")
        forged_repo = dict(self.scope)
        forged_repo["repo"] = "other/repo"
        cross = self.svc.coordinate(
            actor=self.actor, scope=forged_repo, task_id="tsk_w05", chain_id="ch_a",
            action="deploy", idempotency_key="cross-repo",
            expected_revision=self._read_chain("tsk_w05", "ch_a")["revision"],
        )
        self.assertEqual(cross.code, "NOT_AUTHORIZED")
        self.assertEqual(self.deploy.plans, [])

        self._submit("fb_local")
        local = self.svc.store.read("feedback", "fb_local")
        foreign = dict(local)
        foreign["feedback_id"] = "fb_foreign"
        foreign["repo_id"] = "other/repo"
        foreign["body"] = "foreign-body"
        foreign["scope_task_id"] = "tsk_other"
        self.svc.store.write("feedback", "fb_foreign", foreign)
        cursor = self.svc.store.read("cursor", "outbound")
        expected = 0 if cursor is None else cursor["revision"]
        synced = self._sync(key="sync-batch-iso", expected_revision=expected)
        self.assertTrue(synced.ok, synced.as_dict())
        created = [call for call in self.remote.calls if call[0] == "create"]
        self.assertIn(("create", "fb_local"), created)
        self.assertNotIn(("create", "fb_foreign"), created)
        self.assertEqual(self.svc.store.read("feedback", "fb_foreign")["state"], "PENDING")
        self.assertIsNone(getattr(self.svc, "feedback")("fb_local"))
        self.assertEqual(getattr(self.svc, "traces")(), [])
        hidden = getattr(self.svc, "project")(actor=self.actor, task_id="tsk_w05")
        self.assertEqual(hidden.code, "NOT_AUTHORIZED")

    def test_p0_author_binds_server_principal_not_actor_id(self):
        class BoundAuth(FixtureAuth):
            def authenticate(self, actor):
                result = super().authenticate(actor)
                result["principal_id"] = "server-principal"
                return result

        self.auth = BoundAuth()
        self.svc = self._service()
        denied = self.svc.submit_feedback(
            actor=self.actor, scope=self.scope, author_id="fixture", feedback_id="fb_actor",
            category="bug", body="actor-id", idempotency_key="principal-deny", expected_revision=0,
        )
        self.assertEqual(denied.code, "NOT_AUTHORIZED")
        self.assertIsNone(self.svc.store.read("feedback", "fb_actor"))
        allowed = self.svc.submit_feedback(
            actor=self.actor, scope=self.scope, author_id="server-principal", feedback_id="fb_principal",
            category="bug", body="server", idempotency_key="principal-allow", expected_revision=0,
        )
        self.assertTrue(allowed.ok, allowed.as_dict())
        self.assertEqual(self.svc.store.read("feedback", "fb_principal")["author_id"], "server-principal")

    def test_p0_acceptance_ignores_caller_decision_and_rejects_promotion(self):
        self._claim_task(key="claim-accept-p0")
        self._to_accepting("tsk_w05", "ch_a")
        chain = self._read_chain("tsk_w05", "ch_a")
        caller = self.svc.record_acceptance(
            actor=self.actor, scope=self.scope, task_id="tsk_w05", chain_id="ch_a",
            signer_kind="user_acceptance", decision="accepted",
            idempotency_key="caller-only", expected_revision=chain["revision"],
        )
        self.assertFalse(caller.ok)
        self.assertEqual(self._read_chain("tsk_w05", "ch_a")["state"], "ACCEPTING")
        self.assertIsNone(self._read_chain("tsk_w05", "ch_a")["acceptance_decision"])

        self.tmp.cleanup()
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events = FixtureEvents()
        self.schedule = FixtureSchedule()
        self.deploy = FixtureDeploy()
        self.remote = FixtureRemote()

        class PromotingAuth(FixtureAuth):
            def acceptance(self, principal_id, binding):
                return {
                    "ok": True,
                    "allow": True,
                    "blocked": False,
                    "decision": "accepted",
                    "signer_kind": "user_acceptance",
                    "test_double": False,
                    "runtime_acceptance": True,
                    "github_proof": True,
                    "macci_proof": True,
                    "counts_as_runtime_user_acceptance": True,
                    "candidate_id": binding.get("candidate_id"),
                }

        self.auth = PromotingAuth()
        self.svc = self._service()
        self._claim_task(key="claim-promote")
        self._to_accepting("tsk_w05", "ch_a")
        chain = self._read_chain("tsk_w05", "ch_a")
        self.schedule.evidence_state[chain["candidate_id"]] = {
            "valid": True,
            "known": True,
            "candidate_id": chain["candidate_id"],
            "head": chain["head"],
            "base": chain["base"],
            "merge": chain["merge"],
            "tree": chain["tree"],
        }
        promoted = self.svc.record_acceptance(
            actor=self.actor, scope=self.scope, task_id="tsk_w05", chain_id="ch_a",
            signer_kind="user_acceptance", decision="accepted",
            idempotency_key="promote", expected_revision=chain["revision"],
        )
        self.assertFalse(promoted.ok)
        self.assertFalse(promoted.body.get("counts_as_runtime_user_acceptance", False))
        self.assertFalse(promoted.body.get("github_proof", False))
        self.assertEqual(self._read_chain("tsk_w05", "ch_a")["state"], "ACCEPTING")
        self.assertIsNone(self._read_chain("tsk_w05", "ch_a")["acceptance_decision"])


if __name__ == "__main__":
    unittest.main()
