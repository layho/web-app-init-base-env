"""Isolated automatic delivery loop for V0.6.0 W05.

Scope is F02, F03, F06, F07, F08, F09, F10, and R08. Merge and deploy are
protocol records only. User acceptance and publish authorization are not
forged: protocol stamps stay ``test_double`` and production flags stay false.
"""

from __future__ import annotations

import hashlib
import json
import secrets

from app.control_delivery.candidate import candidate_id, valid_sha
from app.control_delivery.errors import Result
from app.control_delivery.machine import allows, apply_guard, completion
from app.control_delivery.projection import project_task
from app.control_delivery.flags import (
    AUTOMATIC_PUBLISH_AUTHORIZATION,
    FORGED_USER_ACCEPTANCE,
    PRODUCTION_ENABLEMENT,
    PRODUCTION_VERIFICATION_ENABLED,
)
from app.control_delivery.store import Journal

_TERMINAL_PR = {"merged", "closed", "superseded"}
_SUPERSEDE_FROM = {"OPEN", "TESTING", "INTEGRATING"}


def _hash(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _denied(http, code, state, body=None):
    stamp = _limits()
    data = stamp if body is None else {**stamp, **body}
    return Result(ok=False, http=http, code=code, state=state, changed=False, body=data)


def _ok(state, body, *, changed=True, code=None, http=200, replayed=False):
    data = {**_limits(), **body}
    return Result(
        ok=True, http=http, code=code, state=state, changed=changed, body=data, replayed=replayed,
    )


def _pending(code="INTERFACE_PENDING", state="BLOCKED", body=None):
    data = {**_limits(), "pending": True}
    if body:
        data.update(body)
    return Result(
        ok=False, http=409, code=code, state=state, changed=False, body=data, blocked=True, pending=True,
    )


def _limits():
    return {
        "production_enablement": PRODUCTION_ENABLEMENT,
        "production_verification_enabled": PRODUCTION_VERIFICATION_ENABLED,
        "production_publish_authorized": AUTOMATIC_PUBLISH_AUTHORIZATION,
        "forged_user_acceptance": FORGED_USER_ACCEPTANCE,
        "runtime_acceptance": False,
        "runtime_evidence": False,
        "github_proof": False,
        "macci_proof": False,
        "real_merge": False,
        "real_deploy": False,
    }


class DeliveryService:
    def __init__(self, root, *, auth, events, schedule, deploy, remote, clock):
        self.store = Journal(root)
        self.auth = auth
        self.events = events
        self.schedule = schedule
        self.deploy = deploy
        self.remote = remote
        self.clock = clock

    def submit_feedback(self, *, actor, scope, author_id, feedback_id, category, body, idempotency_key, expected_revision=0):
        payload = {
            "author_id": author_id,
            "feedback_id": feedback_id,
            "category": category,
            "body": body,
        }
        begun = self._begin(
            actor=actor, scope=scope, operation="feedback.submit", candidate_id=None,
            idempotency_key=idempotency_key, payload=payload, expected_revision=expected_revision,
            kind="feedback", object_id=feedback_id, create=True,
        )
        if isinstance(begun, Result):
            return begun
        existing = self.store.read("feedback", feedback_id)
        if existing is not None:
            if existing["author_id"] != author_id:
                self._trace("feedback.hijack", feedback_id=feedback_id, actor=actor.get("id"))
                return _denied(403, "NOT_AUTHORIZED", existing["state"], {"reason": "cross_author"})
            return _denied(409, "CONFLICT_REVISION", existing["state"], {"reason": "feedback_exists"})
        if author_id != actor.get("id"):
            return _denied(403, "NOT_AUTHORIZED", "REJECTED", {"reason": "cross_author"})
        doc = {
            "feedback_id": feedback_id,
            "author_id": author_id,
            "category": category,
            "body": body,
            "revision": 1,
            "state": "PENDING",
            "issue_number": None,
            "task_id": None,
            "lease": None,
            "operation_id": None,
            "progress_seq": 0,
            "progress_status": None,
            "comments": {},
            "rejected_comments": [],
            "out_of_order": [],
            "inbound_complete": False,
            "closed": False,
            "close_evidence": None,
            "repo_id": scope["repo"],
        }
        if not self._commit_event(begun, "feedback.submitted", doc, feedback_id=feedback_id):
            return _pending(body={"feedback_id": feedback_id})
        self.store.write("feedback", feedback_id, doc)
        public = {"feedback_id": feedback_id, "state": "PENDING", "revision": 1}
        self._remember(begun, public, "PENDING")
        self._trace("feedback.submit", feedback_id=feedback_id, state="PENDING")
        return _ok("PENDING", public)

    def claim_feedback_row(self, *, actor, scope, feedback_id, idempotency_key, expected_revision):
        payload = {"feedback_id": feedback_id}
        begun = self._begin(
            actor=actor, scope=scope, operation="feedback.claim", candidate_id=None,
            idempotency_key=idempotency_key, payload=payload, expected_revision=expected_revision,
            kind="feedback", object_id=feedback_id,
        )
        if isinstance(begun, Result):
            return begun
        row = begun["doc"]
        if row is None:
            return _denied(409, "EVIDENCE_MISSING", "ABSENT", {"feedback_id": feedback_id})
        if row["author_id"] != actor.get("id"):
            self._trace("feedback.hijack", feedback_id=feedback_id, actor=actor.get("id"))
            return _denied(403, "NOT_AUTHORIZED", row["state"], {"reason": "cross_author"})
        if row["state"] == "UNKNOWN":
            return _denied(409, "SIDE_EFFECT_UNKNOWN", "UNKNOWN", {"feedback_id": feedback_id, "retry_allowed": False})
        lease = row.get("lease")
        if lease and lease["expires_at"] > self.clock() and lease["principal_id"] != actor.get("id"):
            return _denied(409, "LEASE_FENCE_REJECTED", row["state"], {"changed": False})
        generation = 1 if not lease else lease["generation"] + 1
        token = "opaque-" + secrets.token_hex(16)
        row["lease"] = {
            "principal_id": actor.get("id"),
            "generation": generation,
            "token": token,
            "expires_at": self.clock() + 60,
        }
        row["state"] = "CLAIMED"
        row["revision"] = row["revision"] + 1
        if not self._commit_event(begun, "feedback.claimed", row, feedback_id=feedback_id):
            return _pending(body={"feedback_id": feedback_id})
        self.store.write("feedback", feedback_id, row)
        public = {
            "feedback_id": feedback_id,
            "state": "CLAIMED",
            "revision": row["revision"],
            "lease_generation": generation,
        }
        self._remember(begun, public, "CLAIMED")
        self._trace("feedback.claim", feedback_id=feedback_id, state="CLAIMED", token_issued=True)
        public = dict(public)
        public["lease_token"] = token
        return _ok("CLAIMED", public)

    def push_claimed(self, *, actor, scope, feedback_id, token, idempotency_key, expected_revision, credential_present):
        payload = {"feedback_id": feedback_id, "credential_present": bool(credential_present)}
        begun = self._begin(
            actor=actor, scope=scope, operation="feedback.push", candidate_id=None,
            idempotency_key=idempotency_key, payload=payload, expected_revision=expected_revision,
            kind="feedback", object_id=feedback_id,
        )
        if isinstance(begun, Result):
            return begun
        row = begun["doc"]
        if row["author_id"] != actor.get("id"):
            self._trace("feedback.hijack", feedback_id=feedback_id, actor=actor.get("id"))
            return _denied(403, "NOT_AUTHORIZED", row["state"], {"reason": "cross_author"})
        lease = row.get("lease") or {}
        if (
            row["state"] != "CLAIMED"
            or lease.get("token") != token
            or lease.get("principal_id") != actor.get("id")
            or lease.get("expires_at", 0) <= self.clock()
        ):
            self._trace("feedback.fence", feedback_id=feedback_id, state=row["state"])
            return _denied(409, "LEASE_FENCE_REJECTED", row["state"], {"changed": False, "retry_allowed": False})
        if not credential_present:
            return _denied(403, "CREDENTIAL_ABSENT", row["state"], {"remote_called": False})
        before = len(self.remote.calls)
        outcome = self._push_row(actor, row)
        if outcome.get("code") == "LEASE_FENCE_REJECTED":
            return _denied(409, "LEASE_FENCE_REJECTED", row["state"])
        public = {**_limits(), **outcome, "remote_calls_added": len(self.remote.calls) - before}
        self._remember(begun, public, outcome.get("state", row["state"]))
        self._trace("feedback.push", feedback_id=feedback_id, state=outcome.get("state"))
        return _ok(outcome.get("state", row["state"]), public, changed=outcome.get("state") != "CLAIMED")

    def sync_outbound(self, *, actor, scope, idempotency_key, credential_present, batch_limit=None, expected_revision=0):
        payload = {"credential_present": bool(credential_present), "batch_limit": batch_limit}
        begun = self._begin(
            actor=actor, scope=scope, operation="feedback.sync_out", candidate_id=None,
            idempotency_key=idempotency_key, payload=payload, expected_revision=expected_revision,
            kind="cursor", object_id="outbound", create=True,
        )
        if isinstance(begun, Result):
            return begun
        if not credential_present:
            self._trace("feedback.sync_out", state="BLOCKED", reason="credential_absent")
            public = {
                **_limits(),
                "state": "BLOCKED",
                "reason": "credential_absent",
                "synced": [],
                "remote_called": False,
            }
            self._remember(begun, public, "BLOCKED")
            cursor = begun["doc"] or {"revision": 0, "cursor": None}
            cursor["revision"] = cursor.get("revision", 0) + 1
            cursor["state"] = "BLOCKED"
            self.store.write("cursor", "outbound", cursor)
            return Result(
                ok=False, http=403, code="CREDENTIAL_ABSENT", state="BLOCKED",
                changed=False, body=public, blocked=True,
            )

        rows = [
            row for row in self.store.list("feedback")
            if row["state"] in {"PENDING", "FAILED"} and row["author_id"] == actor.get("id")
        ]
        rows.sort(key=lambda row: row["feedback_id"])
        results = []
        processed = 0
        remainder = None
        for row in rows:
            if batch_limit is not None and processed >= batch_limit:
                remainder = row["feedback_id"]
                break
            outcome = self._push_row(actor, row)
            results.append(outcome)
            processed += 1
        left = [
            row["feedback_id"] for row in self.store.list("feedback")
            if row["state"] in {"PENDING", "FAILED", "UNKNOWN"}
        ]
        public = {
            "state": "SYNCED",
            "results": results,
            "continuation": remainder,
            "remaining": left,
            "truncated_as_complete": False,
            "remote_called": True,
        }
        cursor = begun["doc"] or {"revision": 0}
        cursor["revision"] = cursor.get("revision", 0) + 1
        cursor["cursor"] = remainder
        cursor["state"] = "SYNCED"
        cursor["retained_ids"] = sorted(row["feedback_id"] for row in self.store.list("feedback"))
        self.store.write("cursor", "outbound", cursor)
        self._remember(begun, public, "SYNCED")
        self._trace("feedback.sync_out", state="SYNCED", continuation=remainder, count=processed)
        return _ok("SYNCED", public)

    def sync_inbound(self, *, actor, scope, feedback_id, idempotency_key, expected_revision, page_budget=None, per_page=1):
        payload = {"feedback_id": feedback_id, "page_budget": page_budget, "per_page": per_page}
        begun = self._begin(
            actor=actor, scope=scope, operation="feedback.sync_in", candidate_id=None,
            idempotency_key=idempotency_key, payload=payload, expected_revision=expected_revision,
            kind="feedback", object_id=feedback_id,
        )
        if isinstance(begun, Result):
            return begun
        row = begun["doc"]
        if row is None:
            return _denied(409, "EVIDENCE_MISSING", "ABSENT")
        if row["author_id"] != actor.get("id"):
            self._trace("feedback.hijack", feedback_id=feedback_id, actor=actor.get("id"))
            return _denied(403, "NOT_AUTHORIZED", row["state"], {"reason": "cross_author"})
        if not row.get("issue_number"):
            return _denied(409, "DEPENDENCY_UNSATISFIED", row["state"], {"reason": "issue_unlinked"})
        before = sorted(item["feedback_id"] for item in self.store.list("feedback"))
        page = 1
        applied = 0
        while True:
            if page_budget is not None and page > page_budget:
                row["inbound_complete"] = False
                row["revision"] = row["revision"] + 1
                self.store.write("feedback", feedback_id, row)
                public = {
                    "feedback_id": feedback_id,
                    "state": row["state"],
                    "revision": row["revision"],
                    "inbound_complete": False,
                    "complete": False,
                    "retained_ids": before,
                }
                self._remember(begun, public, row["state"])
                self._trace("feedback.sync_in", feedback_id=feedback_id, complete=False)
                return Result(
                    ok=False, http=409, code="INCOMPLETE_PAGE", state=row["state"],
                    changed=True, body={**_limits(), **public},
                )
            batch = self.remote.list_comments(row["issue_number"], page=page, per_page=per_page)
            if batch.get("blocked"):
                return _pending(body={"feedback_id": feedback_id})
            for comment in batch.get("items") or []:
                self._apply_comment(row, comment)
                applied += 1
            if not batch.get("has_more"):
                row["inbound_complete"] = True
                break
            page += 1
        row["revision"] = row["revision"] + 1
        self.store.write("feedback", feedback_id, row)
        after = sorted(item["feedback_id"] for item in self.store.list("feedback"))
        public = {
            "feedback_id": feedback_id,
            "state": row["state"],
            "revision": row["revision"],
            "inbound_complete": True,
            "complete": True,
            "progress_seq": row["progress_seq"],
            "progress_status": row["progress_status"],
            "out_of_order": list(row["out_of_order"]),
            "rejected_comments": list(row["rejected_comments"]),
            "applied": applied,
            "retained_ids": after,
        }
        self._remember(begun, public, row["state"])
        self._trace("feedback.sync_in", feedback_id=feedback_id, complete=True, progress_seq=row["progress_seq"])
        return _ok(row["state"], public)

    def claim_task(self, *, actor, scope, task_id, feedback_ids, chains, idempotency_key, expected_revision=0):
        payload = {"task_id": task_id, "feedback_ids": list(feedback_ids), "chains": chains}
        begun = self._begin(
            actor=actor, scope=scope, operation="task.claim", candidate_id=None,
            idempotency_key=idempotency_key, payload=payload, expected_revision=expected_revision,
            kind="task", object_id=task_id, create=True,
        )
        if isinstance(begun, Result):
            return begun
        if begun["doc"] is not None:
            return _denied(409, "CONFLICT_REVISION", begun["doc"]["phase"], {"reason": "task_exists"})
        built = []
        for spec in chains:
            if not all(valid_sha(spec.get(field)) for field in ("head", "base", "merge", "tree")):
                return _denied(422, "SCHEMA_UNSUPPORTED", "REJECTED", {"reason": "candidate_tuple"})
            cid = candidate_id(scope["repo"], task_id, spec["head"], spec["base"], spec["merge"], spec["tree"])
            built.append({
                "chain_id": spec["chain_id"],
                "task_id": task_id,
                "pr_number": spec["pr_number"],
                "required": bool(spec.get("required", True)),
                "state": "OPEN",
                "head": spec["head"],
                "base": spec["base"],
                "merge": spec["merge"],
                "tree": spec["tree"],
                "candidate_id": cid,
                "revision": 1,
                "draft": bool(spec.get("draft", False)),
                "closed_superseded": bool(spec.get("closed_superseded", False)),
                "evidence_valid": False,
                "test_evidence": None,
                "acceptance_decision": None,
                "acceptance_proof_class": None,
                "publish_evidence": None,
                "run_id": None,
                "legacy_overall_status": spec.get("legacy_overall_status"),
                "historical_blocked": bool(spec.get("historical_blocked", False)),
                "recorded_steps": {},
                "repo_id": scope["repo"],
            })
        run_id = "run_" + task_id + "_1"
        task = {
            "task_id": task_id,
            "phase": "claim",
            "revision": 1,
            "feedback_ids": list(feedback_ids),
            "run_id": run_id,
            "attempt": 1,
            "task_acceptance": None,
            "acceptance_issue_closed": False,
            "repo_id": scope["repo"],
            "actor_id": actor.get("id"),
            "completion_condition": (
                "every required chain is PUBLISHED and each chain has publish_evidence "
                "and task_acceptance is recorded"
            ),
        }
        if not self._commit_event(begun, "task.claimed", task, task_id=task_id, run_id=run_id):
            return _pending(body={"task_id": task_id})
        for chain in built:
            chain["run_id"] = run_id
            self.store.write("chain", task_id + "__" + chain["chain_id"], chain)
        for feedback_id in feedback_ids:
            row = self.store.read("feedback", feedback_id)
            if row is not None:
                row["task_id"] = task_id
                row["revision"] = row["revision"] + 1
                self.store.write("feedback", feedback_id, row)
        self.store.write("task", task_id, task)
        public = {
            "task_id": task_id,
            "state": "claim",
            "revision": 1,
            "run_id": run_id,
            "chain_ids": [chain["chain_id"] for chain in built],
            "candidate_ids": [chain["candidate_id"] for chain in built],
            "completion_condition": task["completion_condition"],
        }
        self._remember(begun, public, "claim")
        self._trace("task.claim", task_id=task_id, run_id=run_id, chains=public["chain_ids"])
        return _ok("claim", public)

    def bind_test_evidence(self, *, actor, scope, task_id, chain_id, head, base, merge, tree, passed, run_id, idempotency_key, expected_revision):
        payload = {
            "task_id": task_id, "chain_id": chain_id, "head": head, "base": base,
            "merge": merge, "tree": tree, "passed": bool(passed), "run_id": run_id,
        }
        key = task_id + "__" + chain_id
        begun = self._begin(
            actor=actor, scope=scope, operation="evidence.bind", candidate_id=None,
            idempotency_key=idempotency_key, payload=payload, expected_revision=expected_revision,
            kind="chain", object_id=key,
        )
        if isinstance(begun, Result):
            return begun
        chain = begun["doc"]
        if chain is None:
            return _denied(409, "EVIDENCE_MISSING", "ABSENT")
        if not all(valid_sha(value) for value in (head, base, merge, tree)):
            return _denied(422, "SCHEMA_UNSUPPORTED", chain["state"])
        cid = candidate_id(scope["repo"], task_id, head, base, merge, tree)
        if (head, base, merge, tree, cid) != (
            chain["head"], chain["base"], chain["merge"], chain["tree"], chain["candidate_id"],
        ):
            return _denied(409, "CANDIDATE_STALE", chain["state"], {"candidate_id": chain["candidate_id"]})
        if chain["state"] == "OPEN":
            if not allows("OPEN", "TESTING", None):
                return _denied(409, "DEPENDENCY_UNSATISFIED", chain["state"])
            chain["state"] = "TESTING"
            chain["recorded_steps"]["claim"] = True
        if chain["state"] != "TESTING":
            return _denied(409, "DEPENDENCY_UNSATISFIED", chain["state"], {"reason": "evidence_requires_testing"})
        evidence = {
            "candidate_id": cid,
            "head": head,
            "base": base,
            "merge": merge,
            "tree": tree,
            "run_id": run_id,
            "passed": bool(passed),
            "valid": True,
            "proof_class": "test_double",
            "macci_proof": False,
            "runtime_evidence": False,
        }
        guard = "test_succeeded" if passed else "test_failed"
        target = "INTEGRATING" if passed else "FAILED"
        if not allows("TESTING", target, guard):
            return _denied(409, "DEPENDENCY_UNSATISFIED", chain["state"])
        chain["state"] = target
        chain["evidence_valid"] = bool(passed)
        chain["test_evidence"] = evidence
        chain["recorded_steps"]["tests"] = True
        chain["run_id"] = run_id
        chain["revision"] = chain["revision"] + 1
        if not self._commit_event(
            begun, "evidence.bound", chain, task_id=task_id, run_id=run_id, candidate_id=cid,
        ):
            return _pending(body={"candidate_id": cid})
        self.store.write("evidence", cid, evidence)
        self.store.write("chain", key, chain)
        public = {
            "task_id": task_id,
            "chain_id": chain_id,
            "state": chain["state"],
            "revision": chain["revision"],
            "candidate_id": cid,
            "evidence_valid": chain["evidence_valid"],
            "proof_class": "test_double",
        }
        self._remember(begun, public, chain["state"])
        self._trace("evidence.bind", task_id=task_id, run_id=run_id, candidate_id=cid, state=chain["state"])
        return _ok(chain["state"], public)

    def observe_candidate(self, *, actor, scope, task_id, chain_id, head, base, merge, tree, idempotency_key, expected_revision):
        payload = {"task_id": task_id, "chain_id": chain_id, "head": head, "base": base, "merge": merge, "tree": tree}
        key = task_id + "__" + chain_id
        begun = self._begin(
            actor=actor, scope=scope, operation="candidate.observe", candidate_id=None,
            idempotency_key=idempotency_key, payload=payload, expected_revision=expected_revision,
            kind="chain", object_id=key,
        )
        if isinstance(begun, Result):
            return begun
        chain = begun["doc"]
        if chain is None:
            return _denied(409, "EVIDENCE_MISSING", "ABSENT")
        changed_field = _tuple_diff(chain, head, base, merge, tree)
        if changed_field is None:
            public = {"state": chain["state"], "evidence_valid": chain["evidence_valid"], "changed_field": None}
            self._remember(begun, public, chain["state"])
            return _ok(chain["state"], public, changed=False)
        evidence = chain.get("test_evidence")
        if evidence is not None:
            evidence = dict(evidence)
            evidence["valid"] = False
            evidence["invalidated_by"] = changed_field
            chain["test_evidence"] = evidence
        chain["evidence_valid"] = False
        if chain["state"] in _SUPERSEDE_FROM and allows(chain["state"], "SUPERSEDED", "candidate_updated"):
            chain["state"] = "SUPERSEDED"
            chain["closed_superseded"] = True
        chain["revision"] = chain["revision"] + 1
        if not self._commit_event(
            begun, "candidate.stale", chain, task_id=task_id, candidate_id=chain["candidate_id"],
        ):
            return _pending(body={"candidate_id": chain["candidate_id"]})
        if evidence is not None:
            self.store.write("evidence", chain["candidate_id"], evidence)
        self.store.write("chain", key, chain)
        public = {
            **_limits(),
            "state": chain["state"],
            "revision": chain["revision"],
            "evidence_valid": False,
            "changed_field": changed_field,
            "candidate_id": chain["candidate_id"],
            "prior_evidence_reused": False,
        }
        self._remember(begun, public, chain["state"])
        self._trace(
            "candidate.observe", task_id=task_id, candidate_id=chain["candidate_id"],
            state=chain["state"], changed_field=changed_field,
        )
        return Result(
            ok=False, http=409, code="CANDIDATE_STALE", state=chain["state"], changed=True, body=public,
        )

    def refresh_test_evidence(self, *, actor, scope, task_id, chain_id, idempotency_key, expected_revision):
        key = task_id + "__" + chain_id
        begun = self._begin(
            actor=actor, scope=scope, operation="evidence.refresh", candidate_id=None,
            idempotency_key=idempotency_key, payload={"task_id": task_id, "chain_id": chain_id},
            expected_revision=expected_revision, kind="chain", object_id=key,
        )
        if isinstance(begun, Result):
            return begun
        chain = begun["doc"]
        if chain is None or not chain.get("test_evidence"):
            return _denied(409, "EVIDENCE_MISSING", "ABSENT" if chain is None else chain["state"])
        status = self.schedule.evidence(chain["candidate_id"])
        if status.get("blocked"):
            return _pending(body={"candidate_id": chain["candidate_id"]})
        same_tuple = (
            status.get("head") == chain["head"]
            and status.get("base") == chain["base"]
            and status.get("merge") == chain["merge"]
            and status.get("tree") == chain["tree"]
            and status.get("candidate_id") == chain["candidate_id"]
        )
        if not status.get("known") or not status.get("valid") or not same_tuple:
            evidence = dict(chain["test_evidence"])
            evidence["valid"] = False
            evidence["proof_class"] = "test_double"
            evidence["macci_proof"] = False
            chain["test_evidence"] = evidence
            chain["evidence_valid"] = False
            if chain["state"] == "TESTING" and allows("TESTING", "FAILED", "test_failed"):
                chain["state"] = "FAILED"
            chain["revision"] = chain["revision"] + 1
            self.store.write("evidence", chain["candidate_id"], evidence)
            self.store.write("chain", key, chain)
            public = {
                "state": chain["state"],
                "revision": chain["revision"],
                "evidence_valid": False,
                "candidate_id": chain["candidate_id"],
                "macci_proof": False,
            }
            self._remember(begun, public, chain["state"])
            self._trace("evidence.invalid", task_id=task_id, candidate_id=chain["candidate_id"], state=chain["state"])
            return Result(
                ok=False, http=409, code="EVIDENCE_MISSING", state=chain["state"],
                changed=True, body={**_limits(), **public},
            )
        public = {"state": chain["state"], "evidence_valid": True, "candidate_id": chain["candidate_id"]}
        self._remember(begun, public, chain["state"])
        return _ok(chain["state"], public, changed=False)

    def record_acceptance(self, *, actor, scope, task_id, chain_id, signer_kind, decision, idempotency_key, expected_revision):
        payload = {"task_id": task_id, "chain_id": chain_id, "signer_kind": signer_kind, "decision": decision}
        key = task_id + "__" + chain_id
        if signer_kind != "user_acceptance" or decision not in {"accepted", "rejected"}:
            gated = self._begin(
                actor=actor, scope=scope, operation="release.reject" if decision == "rejected" else "release.accept",
                candidate_id=None, idempotency_key=idempotency_key, payload=payload,
                expected_revision=expected_revision, kind="chain", object_id=key,
                stop_before_idempotency=True,
            )
            if isinstance(gated, Result):
                return gated
            return _denied(403, "POLICY_DENIED", "REJECTED", {"reason": "signer_kind", "forged_user_acceptance": False})
        operation = "release.accept" if decision == "accepted" else "release.reject"
        begun = self._begin(
            actor=actor, scope=scope, operation=operation, candidate_id=None,
            idempotency_key=idempotency_key, payload=payload, expected_revision=expected_revision,
            kind="chain", object_id=key,
        )
        if isinstance(begun, Result):
            return begun
        chain = begun["doc"]
        if chain is None:
            return _denied(409, "EVIDENCE_MISSING", "ABSENT")
        if chain.get("acceptance_decision") == "rejected":
            self._trace("acceptance.sticky_rejection", task_id=task_id, chain_id=chain_id)
            return _denied(409, "FREEZE_ACTIVE", "REJECTED", {
                "reason": "acceptance_rejection",
                "acceptance_decision": "rejected",
                "forged_user_acceptance": False,
            })
        if chain["state"] != "ACCEPTING":
            return _denied(409, "DEPENDENCY_UNSATISFIED", chain["state"], {"reason": "not_accepting"})
        guard = "user_acceptance" if decision == "accepted" else "user_rejection"
        target = "SATISFIED" if decision == "accepted" else "REJECTED"
        if not allows("ACCEPTING", target, guard):
            return _denied(409, "DEPENDENCY_UNSATISFIED", chain["state"])
        chain["state"] = target
        chain["acceptance_decision"] = decision
        chain["acceptance_proof_class"] = "test_double"
        chain["recorded_steps"]["accept"] = True
        chain["revision"] = chain["revision"] + 1
        if not self._commit_event(
            begun, "acceptance.recorded", chain, task_id=task_id, candidate_id=chain["candidate_id"],
        ):
            return _pending(body={"candidate_id": chain["candidate_id"]})
        self.store.write("chain", key, chain)
        public = {
            "state": chain["state"],
            "revision": chain["revision"],
            "acceptance_decision": decision,
            "acceptance_proof_class": "test_double",
            "counts_as_runtime_user_acceptance": False,
            "forged_user_acceptance": False,
            "production_publish_authorized": False,
        }
        self._remember(begun, public, chain["state"])
        self._trace("acceptance.record", task_id=task_id, chain_id=chain_id, decision=decision, state=chain["state"])
        return _ok(chain["state"], public)

    def record_task_acceptance(self, *, actor, scope, task_id, signer_kind, idempotency_key, expected_revision):
        payload = {"task_id": task_id, "signer_kind": signer_kind}
        if signer_kind != "user_acceptance":
            gated = self._begin(
                actor=actor, scope=scope, operation="release.accept", candidate_id=None,
                idempotency_key=idempotency_key, payload=payload, expected_revision=expected_revision,
                kind="task", object_id=task_id, stop_before_idempotency=True,
            )
            if isinstance(gated, Result):
                return gated
            return _denied(403, "POLICY_DENIED", "REJECTED", {"reason": "signer_kind"})
        begun = self._begin(
            actor=actor, scope=scope, operation="task.accept", candidate_id=None,
            idempotency_key=idempotency_key, payload=payload, expected_revision=expected_revision,
            kind="task", object_id=task_id,
        )
        if isinstance(begun, Result):
            return begun
        task = begun["doc"]
        if task is None:
            return _denied(409, "EVIDENCE_MISSING", "ABSENT")
        chains = self._chains(task_id)
        required = [chain for chain in chains if chain.get("required") and chain.get("state") != "SUPERSEDED"]
        if not required or any(chain.get("acceptance_decision") != "accepted" for chain in required):
            return _denied(409, "DEPENDENCY_UNSATISFIED", task["phase"], {"reason": "chains_not_accepted"})
        if any(chain.get("acceptance_decision") == "rejected" for chain in chains):
            return _denied(409, "FREEZE_ACTIVE", task["phase"], {"reason": "acceptance_rejection"})
        task["task_acceptance"] = {
            "proof_class": "test_double",
            "counts_as_runtime_user_acceptance": False,
            "recorded_at": self.clock(),
            "signer_kind": "user_acceptance",
        }
        task["revision"] = task["revision"] + 1
        self.store.write("task", task_id, task)
        public = {
            "state": task["phase"],
            "revision": task["revision"],
            "task_acceptance": task["task_acceptance"],
            "counts_as_runtime_user_acceptance": False,
        }
        self._remember(begun, public, task["phase"])
        self._trace("task.accept", task_id=task_id, run_id=task.get("run_id"))
        return _ok(task["phase"], public)

    def apply_observation(self, *, actor, scope, task_id, chain_id, idempotency_key, expected_revision):
        key = task_id + "__" + chain_id
        chain = self.store.read("chain", key)
        candidate = None if chain is None else chain.get("candidate_id")
        begun = self._begin(
            actor=actor, scope=scope, operation="chain.observe", candidate_id=candidate,
            idempotency_key=idempotency_key, payload={"task_id": task_id, "chain_id": chain_id},
            expected_revision=expected_revision, kind="chain", object_id=key,
        )
        if isinstance(begun, Result):
            return begun
        chain = begun["doc"]
        if chain is None:
            return _denied(409, "EVIDENCE_MISSING", "ABSENT")
        observed = self.deploy.observe(chain["candidate_id"])
        if observed.get("blocked"):
            return _pending(body={"candidate_id": chain["candidate_id"]})
        if observed.get("status") == "unknown" or observed.get("classification") == "unknown":
            self._mark_unknown("obs-" + chain["candidate_id"] + "-" + idempotency_key, "chain.observe", task_id, chain)
            return _denied(409, "SIDE_EFFECT_UNKNOWN", chain["state"], {
                "retry_allowed": False,
                "candidate_id": chain["candidate_id"],
            })
        if observed.get("candidate_id") != chain["candidate_id"] or not observed.get("exact"):
            return _denied(409, "CANDIDATE_STALE", chain["state"], {"candidate_id": chain["candidate_id"]})
        guard = observed.get("guard")
        if guard == "formal_publish_evidence":
            return self._publish_observation(begun, task_id, chain, key, observed)
        target = apply_guard(chain["state"], guard)
        if target is None:
            return _denied(409, "DEPENDENCY_UNSATISFIED", chain["state"], {"guard": guard})
        if target == "DEPLOYING" and not chain.get("evidence_valid"):
            return _denied(409, "EVIDENCE_MISSING", chain["state"], {"candidate_id": chain["candidate_id"]})
        chain["state"] = target
        if target == "DEPLOYING":
            chain["recorded_steps"]["integrate"] = True
        if target == "ACCEPTING":
            chain["recorded_steps"]["deploy"] = True
        chain["revision"] = chain["revision"] + 1
        self.store.write("chain", key, chain)
        public = {
            "state": chain["state"],
            "revision": chain["revision"],
            "candidate_id": chain["candidate_id"],
            "guard": guard,
            "performed": False,
            "real_merge": False,
            "real_deploy": False,
            "test_double": True,
        }
        self._remember(begun, public, chain["state"])
        self._trace("chain.observe", task_id=task_id, candidate_id=chain["candidate_id"], state=chain["state"], guard=guard)
        return _ok(chain["state"], public)

    def coordinate(self, *, actor, scope, task_id, chain_id, action, idempotency_key, expected_revision):
        if action not in {"merge", "deploy"}:
            return _denied(422, "SCHEMA_UNSUPPORTED", "REJECTED")
        key = task_id + "__" + chain_id
        chain = self.store.read("chain", key)
        candidate = None if chain is None else chain.get("candidate_id")
        operation = "preview.merge" if action == "merge" else "deploy.plan"
        begun = self._begin(
            actor=actor, scope=scope, operation=operation, candidate_id=candidate,
            idempotency_key=idempotency_key, payload={"task_id": task_id, "chain_id": chain_id, "action": action},
            expected_revision=expected_revision, kind="chain", object_id=key,
        )
        if isinstance(begun, Result):
            return begun
        chain = begun["doc"]
        if chain is None:
            return _denied(409, "EVIDENCE_MISSING", "ABSENT")
        freeze = self.schedule.freeze(chain["candidate_id"])
        if freeze.get("blocked"):
            return _pending(body={"candidate_id": chain["candidate_id"], "action": action})
        draft = bool(chain.get("draft") or freeze.get("draft"))
        equal_tree = bool(freeze.get("equal_tree"))
        official = bool(freeze.get("official_merge_observed"))
        if draft or (equal_tree and not official):
            self._trace("freeze.active", task_id=task_id, candidate_id=chain["candidate_id"], draft=draft, equal_tree=equal_tree)
            return _denied(409, "FREEZE_ACTIVE", chain["state"], {
                "reason": "draft" if draft else "equal_tree_without_official_merge",
                "acceptance_decision": chain.get("acceptance_decision"),
                "freeze_is_acceptance": False,
                "equal_tree_unfreezes": False,
                "action": action,
                "performed": False,
            })
        if not chain.get("evidence_valid"):
            return _denied(409, "EVIDENCE_MISSING", chain["state"], {"candidate_id": chain["candidate_id"]})
        record = {
            "action": action,
            "candidate_id": chain["candidate_id"],
            "performed": False,
            "real_merge": False,
            "real_deploy": False,
            "production_publish_authorized": False,
            "test_double": True,
        }
        if action == "merge":
            reserved = self.schedule.reserve({
                "task_id": task_id,
                "candidate_id": chain["candidate_id"],
                "pr_number": chain["pr_number"],
                "performed": False,
            })
            if reserved.get("blocked") or not reserved.get("reserved"):
                return _pending(body=record)
        else:
            planned = self.deploy.plan({
                "task_id": task_id,
                "candidate_id": chain["candidate_id"],
                "performed": False,
                "real_host": False,
            })
            if planned.get("blocked") or planned.get("real_host") or planned.get("real_deploy"):
                return _pending(body=record)
            record["deploy_state"] = planned.get("state")
        self._remember(begun, record, chain["state"])
        self._trace(
            "coordinate", task_id=task_id, candidate_id=chain["candidate_id"],
            protocol_action=action, performed=False,
        )
        return _ok(chain["state"], record, changed=False)

    def request_publish(self, *, actor, scope, task_id, chain_id, idempotency_key, expected_revision):
        key = task_id + "__" + chain_id
        chain = self.store.read("chain", key)
        candidate = None if chain is None else chain.get("candidate_id")
        begun = self._begin(
            actor=actor, scope=scope, operation="release.publish", candidate_id=candidate,
            idempotency_key=idempotency_key, payload={"task_id": task_id, "chain_id": chain_id},
            expected_revision=expected_revision, kind="chain", object_id=key,
        )
        if isinstance(begun, Result):
            return begun
        return _denied(403, "POLICY_DENIED", "REJECTED", {
            "allow": False,
            "reason": "publish_not_authorized",
            "production_publish_authorized": False,
        })

    def retry_operation(self, *, actor, scope, operation_id, idempotency_key, expected_revision):
        begun = self._begin(
            actor=actor, scope=scope, operation="operation.retry", candidate_id=None,
            idempotency_key=idempotency_key, payload={"operation_id": operation_id},
            expected_revision=expected_revision, kind="operation", object_id=operation_id,
        )
        if isinstance(begun, Result):
            return begun
        op = begun["doc"]
        if op is None:
            return _denied(409, "EVIDENCE_MISSING", "ABSENT")
        if op["state"] == "UNKNOWN" or not op.get("reconciled"):
            self._trace("operation.retry_blocked", operation_id=operation_id, state=op["state"])
            return _denied(409, "SIDE_EFFECT_UNKNOWN", "UNKNOWN", {
                "operation_id": operation_id,
                "retry_allowed": False,
                "guessed_success": False,
            })
        if op["state"] == "RETRY_WAIT":
            return _denied(409, "DEPENDENCY_UNSATISFIED", "RETRY_WAIT", {
                "reason": "new_run_required",
                "same_run_resumed": False,
            })
        return _denied(409, "DEPENDENCY_UNSATISFIED", op["state"], {"retry_allowed": False})

    def reconcile_operation(self, *, actor, scope, operation_id, idempotency_key, expected_revision):
        begun = self._begin(
            actor=actor, scope=scope, operation="operation.reconcile", candidate_id=None,
            idempotency_key=idempotency_key, payload={"operation_id": operation_id},
            expected_revision=expected_revision, kind="operation", object_id=operation_id,
        )
        if isinstance(begun, Result):
            return begun
        op = begun["doc"]
        if op is None:
            return _denied(409, "EVIDENCE_MISSING", "ABSENT")
        if op["state"] != "UNKNOWN":
            return _denied(409, "DEPENDENCY_UNSATISFIED", op["state"], {"reason": "not_unknown"})
        observed = self.remote.reconcile(operation_id)
        if observed.get("blocked"):
            return _pending(body={"operation_id": operation_id})
        if observed.get("operation_id") != operation_id:
            return _denied(409, "SIDE_EFFECT_UNKNOWN", "UNKNOWN", {"guessed_success": False})
        classification = observed.get("classification")
        if classification not in {"succeeded", "failed", "absent"}:
            self._trace("operation.reconcile_unknown", operation_id=operation_id)
            return _denied(409, "SIDE_EFFECT_UNKNOWN", "UNKNOWN", {
                "operation_id": operation_id,
                "retry_allowed": False,
                "guessed_success": False,
            })
        if classification == "succeeded":
            op["state"] = "SUCCEEDED"
            op["reconciled"] = True
            self._apply_reconciled_success(op, observed)
        elif classification == "failed":
            op["state"] = "FAILED"
            op["reconciled"] = True
            self._set_feedback_state(op, "FAILED")
        else:
            op["state"] = "RETRY_WAIT"
            op["reconciled"] = True
            self._set_feedback_state(op, "RETRY_WAIT")
        op["classification"] = classification
        op["revision"] = op["revision"] + 1
        self.store.write("operation", operation_id, op)
        public = {
            "operation_id": operation_id,
            "state": op["state"],
            "revision": op["revision"],
            "classification": classification,
            "guessed_success": False,
            "same_run_resumed": False,
            "test_double": True,
            "github_proof": False,
        }
        self._remember(begun, public, op["state"])
        self._trace("operation.reconcile", operation_id=operation_id, state=op["state"], run_id=op.get("run_id"))
        return _ok(op["state"], public)

    def continue_after_absent(self, *, actor, scope, feedback_id, idempotency_key, expected_revision, credential_present):
        begun = self._begin(
            actor=actor, scope=scope, operation="feedback.continue", candidate_id=None,
            idempotency_key=idempotency_key, payload={"feedback_id": feedback_id},
            expected_revision=expected_revision, kind="feedback", object_id=feedback_id,
        )
        if isinstance(begun, Result):
            return begun
        row = begun["doc"]
        if row.get("state") != "RETRY_WAIT":
            return _denied(409, "DEPENDENCY_UNSATISFIED", row.get("state", "ABSENT"), {
                "reason": "reconcile_required",
                "same_run_resumed": False,
            })
        operation = self.store.read("operation", row.get("operation_id") or "")
        if operation is None or operation.get("state") != "RETRY_WAIT" or not operation.get("reconciled"):
            return _denied(409, "SIDE_EFFECT_UNKNOWN", row["state"], {
                "retry_allowed": False,
                "same_run_resumed": False,
                "guessed_success": False,
            })
        if not credential_present:
            return _denied(403, "CREDENTIAL_ABSENT", row["state"], {"remote_called": False})
        attempt = int(row.get("attempt") or 1) + 1
        previous_run = row.get("delivery_run_id")
        row["state"] = "PENDING"
        row["operation_id"] = None
        row["attempt"] = attempt
        row["delivery_run_id"] = "run_" + feedback_id + "_" + str(attempt)
        row["revision"] = row["revision"] + 1
        self.store.write("feedback", feedback_id, row)
        outcome = self._push_row(actor, self.store.read("feedback", feedback_id))
        public = {
            **_limits(),
            **outcome,
            "same_run_resumed": False,
            "previous_run_id": previous_run,
            "run_id": row["delivery_run_id"],
            "attempt": attempt,
        }
        self._remember(begun, public, outcome.get("state", "PENDING"))
        self._trace(
            "feedback.continue", feedback_id=feedback_id, run_id=row["delivery_run_id"],
            previous_run_id=previous_run, state=outcome.get("state"),
        )
        return _ok(outcome.get("state", "PENDING"), public)

    def merge_pr_snapshot(self, *, actor, scope, task_id, history, active, idempotency_key, expected_revision=0):
        payload = {"task_id": task_id, "history": history, "active": active}
        begun = self._begin(
            actor=actor, scope=scope, operation="snapshot.merge", candidate_id=None,
            idempotency_key=idempotency_key, payload=payload, expected_revision=expected_revision,
            kind="snapshot", object_id=task_id, create=True,
        )
        if isinstance(begun, Result):
            return begun
        merged = []
        kept = []
        by_number = {row["number"]: dict(row) for row in history}
        for incoming in active:
            number = incoming["number"]
            stored = by_number.get(number)
            if stored is None:
                by_number[number] = dict(incoming)
                merged.append(number)
                continue
            incoming_rev = incoming.get("revision", 0)
            stored_rev = stored.get("revision", 0)
            stored_terminal = stored.get("state") in _TERMINAL_PR
            incoming_terminal = incoming.get("state") in _TERMINAL_PR
            if stored_terminal and not incoming_terminal and incoming_rev <= stored_rev:
                kept.append(number)
                continue
            if incoming_rev < stored_rev:
                kept.append(number)
                continue
            if stored_terminal and not incoming_terminal:
                kept.append(number)
                continue
            by_number[number] = dict(incoming)
            merged.append(number)
        rows = [by_number[number] for number in sorted(by_number)]
        doc = {"task_id": task_id, "revision": 1 if begun["doc"] is None else begun["doc"]["revision"] + 1, "prs": rows}
        self.store.write("snapshot", task_id, doc)
        public = {
            "state": "MERGED_VIEW",
            "revision": doc["revision"],
            "prs": rows,
            "kept_terminal": kept,
            "replaced": merged,
            "mtime_wins": False,
        }
        self._remember(begun, public, "MERGED_VIEW")
        self._trace("snapshot.merge", task_id=task_id, kept_terminal=kept)
        return _ok("MERGED_VIEW", public)

    def acknowledge_acceptance_issue(self, *, actor, scope, task_id, issue_number, idempotency_key, expected_revision):
        begun = self._begin(
            actor=actor, scope=scope, operation="acceptance.acknowledge", candidate_id=None,
            idempotency_key=idempotency_key, payload={"task_id": task_id, "issue_number": issue_number},
            expected_revision=expected_revision, kind="task", object_id=task_id,
        )
        if isinstance(begun, Result):
            return begun
        task = begun["doc"]
        if task is None:
            return _denied(409, "EVIDENCE_MISSING", "ABSENT")
        task["acceptance_issue_closed"] = True
        task["acceptance_issue_number"] = issue_number
        task["revision"] = task["revision"] + 1
        self.store.write("task", task_id, task)
        feedback_closed = []
        for feedback_id in task.get("feedback_ids") or []:
            row = self.store.read("feedback", feedback_id)
            if row is not None:
                feedback_closed.append({"feedback_id": feedback_id, "closed": bool(row.get("closed"))})
        public = {
            "state": task["phase"],
            "revision": task["revision"],
            "acceptance_issue_closed": True,
            "feedback_closed": False,
            "feedback": feedback_closed,
            "business_feedback_closed_by_acknowledge": False,
        }
        self._remember(begun, public, task["phase"])
        self._trace("acceptance.acknowledge", task_id=task_id, feedback_closed=False)
        return _ok(task["phase"], public)

    def close_task(self, *, actor, scope, task_id, idempotency_key, expected_revision):
        begun = self._begin(
            actor=actor, scope=scope, operation="task.close", candidate_id=None,
            idempotency_key=idempotency_key, payload={"task_id": task_id},
            expected_revision=expected_revision, kind="task", object_id=task_id,
        )
        if isinstance(begun, Result):
            return begun
        task = begun["doc"]
        if task is None:
            return _denied(409, "EVIDENCE_MISSING", "ABSENT")
        if task.get("phase") == "close":
            return _denied(409, "DEPENDENCY_UNSATISFIED", "close", {"reason": "already_closed"})
        report = completion(task, self._chains(task_id))
        if not report["met"]:
            return _denied(409, "DEPENDENCY_UNSATISFIED", task["phase"], {
                "completion": report,
                "feedback_closed": False,
            })
        task["phase"] = "close"
        task["revision"] = task["revision"] + 1
        closed_ids = []
        for feedback_id in task.get("feedback_ids") or []:
            row = self.store.read("feedback", feedback_id)
            if row is None:
                continue
            row["closed"] = True
            row["close_evidence"] = {
                "task_id": task_id,
                "run_id": task.get("run_id"),
                "task_acceptance": task.get("task_acceptance"),
                "proof_class": "test_double",
                "runtime_acceptance": False,
            }
            row["revision"] = row["revision"] + 1
            self.store.write("feedback", feedback_id, row)
            closed_ids.append(feedback_id)
        self.store.write("task", task_id, task)
        public = {
            "state": "close",
            "revision": task["revision"],
            "feedback_closed": True,
            "closed_feedback_ids": closed_ids,
            "completion": report,
            "runtime_closed": False,
            "production_publish_authorized": False,
        }
        self._remember(begun, public, "close")
        self._trace("task.close", task_id=task_id, run_id=task.get("run_id"), feedback=closed_ids)
        return _ok("close", public)

    def authorize_repair(self, *, actor, scope, task_id, chain_id, new_chain, decision, idempotency_key, expected_revision):
        payload = {
            "task_id": task_id,
            "chain_id": chain_id,
            "new_chain": new_chain,
            "decision": decision,
        }
        key = task_id + "__" + chain_id
        chain = self.store.read("chain", key)
        candidate = None if chain is None else chain.get("candidate_id")
        if decision == "accepted":
            gated = self._begin(
                actor=actor, scope=scope, operation="repair.replace", candidate_id=candidate,
                idempotency_key=idempotency_key, payload=payload, expected_revision=expected_revision,
                kind="chain", object_id=key, stop_before_idempotency=True,
            )
            if isinstance(gated, Result):
                return gated
            return _denied(403, "POLICY_DENIED", "REJECTED", {
                "reason": "repair_cannot_accept",
                "forged_user_acceptance": False,
            })
        begun = self._begin(
            actor=actor, scope=scope, operation="repair.replace", candidate_id=candidate,
            idempotency_key=idempotency_key, payload=payload, expected_revision=expected_revision,
            kind="chain", object_id=key,
        )
        if isinstance(begun, Result):
            return begun
        chain = begun["doc"]
        if chain is None:
            return _denied(409, "EVIDENCE_MISSING", "ABSENT")
        if chain.get("state") != "REJECTED" or chain.get("acceptance_decision") != "rejected":
            return _denied(409, "DEPENDENCY_UNSATISFIED", chain["state"], {"reason": "not_rejected"})
        if chain.get("draft") and (decision == "draft"):
            return _denied(409, "FREEZE_ACTIVE", "REJECTED", {"freeze_is_acceptance": False, "reason": "draft"})
        grant = self.auth.preauthorization(scope, "repair.replace", chain["candidate_id"])
        if grant.get("blocked"):
            return _pending(body={"candidate_id": chain["candidate_id"]})
        if not grant.get("allow"):
            self._trace("repair.denied", task_id=task_id, candidate_id=chain["candidate_id"])
            return _denied(403, "POLICY_DENIED", "REJECTED", {
                "reason": "preauthorization_missing",
                "acceptance_decision": "rejected",
                "forged_user_acceptance": False,
                "owner_marker_is_sufficient": False,
            })
        if not allows("REJECTED", "SUPERSEDED", "repair_authorized_new_candidate"):
            return _denied(409, "DEPENDENCY_UNSATISFIED", "REJECTED")
        spec = new_chain
        if not all(valid_sha(spec.get(field)) for field in ("head", "base", "merge", "tree")):
            return _denied(422, "SCHEMA_UNSUPPORTED", "REJECTED")
        cid = candidate_id(scope["repo"], task_id, spec["head"], spec["base"], spec["merge"], spec["tree"])
        if cid == chain["candidate_id"]:
            return _denied(409, "CANDIDATE_STALE", "REJECTED", {"reason": "same_candidate"})
        chain["state"] = "SUPERSEDED"
        chain["closed_superseded"] = True
        chain["revision"] = chain["revision"] + 1
        opened = {
            "chain_id": spec["chain_id"],
            "task_id": task_id,
            "pr_number": spec["pr_number"],
            "required": True,
            "state": "OPEN",
            "head": spec["head"],
            "base": spec["base"],
            "merge": spec["merge"],
            "tree": spec["tree"],
            "candidate_id": cid,
            "revision": 1,
            "draft": False,
            "closed_superseded": False,
            "evidence_valid": False,
            "test_evidence": None,
            "acceptance_decision": None,
            "acceptance_proof_class": None,
            "publish_evidence": None,
            "run_id": None,
            "legacy_overall_status": None,
            "historical_blocked": False,
            "recorded_steps": {},
            "repo_id": scope["repo"],
            "follows_chain_id": chain_id,
        }
        self.store.write("chain", key, chain)
        self.store.write("chain", task_id + "__" + spec["chain_id"], opened)
        public = {
            "state": "SUPERSEDED",
            "revision": chain["revision"],
            "old_acceptance_decision": "rejected",
            "new_chain_id": spec["chain_id"],
            "new_state": "OPEN",
            "new_acceptance_decision": None,
            "new_candidate_id": cid,
            "forged_user_acceptance": False,
        }
        self._remember(begun, public, "SUPERSEDED")
        self._trace("repair.authorize", task_id=task_id, candidate_id=cid, old=chain["candidate_id"])
        return _ok("SUPERSEDED", public)

    def project(self, *, actor, task_id):
        authn = self.auth.authenticate(actor)
        if authn.get("blocked"):
            return _pending()
        if not authn.get("ok") or actor.get("kind") != "test":
            return _denied(403, "NOT_AUTHORIZED", "REJECTED")
        task = self.store.read("task", task_id)
        if task is None:
            return _denied(409, "EVIDENCE_MISSING", "ABSENT")
        view = project_task(task, self._chains(task_id))
        view["completion"] = completion(task, self._chains(task_id))
        return _ok(task["phase"], view, changed=False)

    def feedback(self, feedback_id):
        return self.store.read("feedback", feedback_id)

    def traces(self):
        return self.store.traces()

    def _chains(self, task_id):
        rows = [
            row for row in self.store.list("chain")
            if row.get("task_id") == task_id
        ]
        rows.sort(key=lambda row: row["chain_id"])
        return rows

    def _push_row(self, actor, row):
        feedback_id = row["feedback_id"]
        if row["author_id"] != actor.get("id"):
            self._trace("feedback.hijack", feedback_id=feedback_id, actor=actor.get("id"))
            return {"feedback_id": feedback_id, "state": row["state"], "code": "NOT_AUTHORIZED", "reason": "cross_author"}
        if row["state"] == "UNKNOWN":
            return {"feedback_id": feedback_id, "state": "UNKNOWN", "code": "SIDE_EFFECT_UNKNOWN", "retry_allowed": False}
        looked = self.remote.lookup_issue(feedback_id)
        if looked.get("blocked"):
            return {"feedback_id": feedback_id, "state": row["state"], "code": "INTERFACE_PENDING", "blocked": True}
        if looked.get("found"):
            if looked.get("author_id") != row["author_id"]:
                self._trace("feedback.hijack", feedback_id=feedback_id, issue_author=looked.get("author_id"))
                return {
                    "feedback_id": feedback_id,
                    "state": row["state"],
                    "code": "NOT_AUTHORIZED",
                    "reason": "cross_author",
                    "issue_number": None,
                }
            row["issue_number"] = looked["number"]
            row["state"] = "SYNCED"
            row["lease"] = None
            row["revision"] = row["revision"] + 1
            self.store.write("feedback", feedback_id, row)
            return {"feedback_id": feedback_id, "state": "SYNCED", "issue_number": looked["number"], "created": False}
        operation_id = "op-" + feedback_id
        created = self.remote.create_issue({
            "feedback_id": feedback_id,
            "author_id": row["author_id"],
            "body": row["body"],
            "operation_id": operation_id,
        })
        if created.get("status") == "unknown":
            row["state"] = "UNKNOWN"
            row["operation_id"] = created.get("operation_id") or operation_id
            row["revision"] = row["revision"] + 1
            self.store.write("feedback", feedback_id, row)
            self._mark_unknown(row["operation_id"], "feedback.create_issue", row.get("task_id"), {
                "feedback_id": feedback_id,
                "run_id": None,
                "candidate_id": None,
            })
            return {
                "feedback_id": feedback_id,
                "state": "UNKNOWN",
                "code": "SIDE_EFFECT_UNKNOWN",
                "operation_id": row["operation_id"],
                "retry_allowed": False,
            }
        if created.get("status") == "failed" or not created.get("ok", True):
            row["state"] = "FAILED"
            row["lease"] = None
            row["revision"] = row["revision"] + 1
            self.store.write("feedback", feedback_id, row)
            return {"feedback_id": feedback_id, "state": "FAILED", "code": "FAILED", "continued": True}
        if created.get("author_id") != row["author_id"]:
            return {"feedback_id": feedback_id, "state": row["state"], "code": "NOT_AUTHORIZED", "reason": "cross_author"}
        row["issue_number"] = created["number"]
        row["state"] = "SYNCED"
        row["lease"] = None
        row["revision"] = row["revision"] + 1
        self.store.write("feedback", feedback_id, row)
        return {"feedback_id": feedback_id, "state": "SYNCED", "issue_number": created["number"], "created": True}

    def _apply_comment(self, row, comment):
        comment_id = comment.get("comment_id")
        author_id = comment.get("author_id")
        seq = comment.get("seq")
        body = comment.get("body")
        if not isinstance(comment_id, str) or not isinstance(seq, int):
            row["rejected_comments"].append({"comment_id": comment_id, "reason": "malformed"})
            return
        seen = row["comments"].get(comment_id)
        if seen is not None:
            if seen.get("body") != body:
                row["rejected_comments"].append({"comment_id": comment_id, "reason": "payload_mismatch"})
            return
        if author_id != row["author_id"]:
            row["rejected_comments"].append({"comment_id": comment_id, "reason": "cross_author"})
            self._trace("feedback.hijack", feedback_id=row["feedback_id"], comment_id=comment_id)
            return
        row["comments"][comment_id] = {"seq": seq, "body": body, "author_id": author_id}
        if seq <= row["progress_seq"]:
            row["out_of_order"].append(comment_id)
            return
        row["progress_seq"] = seq
        if comment.get("status"):
            row["progress_status"] = comment["status"]

    def _publish_observation(self, begun, task_id, chain, key, observed):
        if chain["state"] != "SATISFIED":
            return _denied(409, "DEPENDENCY_UNSATISFIED", chain["state"], {"reason": "not_satisfied"})
        if not allows("SATISFIED", "PUBLISHED", "formal_publish_evidence"):
            return _denied(409, "DEPENDENCY_UNSATISFIED", chain["state"])
        if AUTOMATIC_PUBLISH_AUTHORIZATION or PRODUCTION_VERIFICATION_ENABLED:
            return _denied(403, "POLICY_DENIED", chain["state"], {"production_publish_authorized": False})
        evidence = {
            "candidate_id": chain["candidate_id"],
            "proof_class": "test_double",
            "formal_publish_evidence": True,
            "publish_performed": False,
            "production_publish_authorized": False,
            "github_proof": False,
            "ref": observed.get("evidence_ref") or ("test-double://publish/" + chain["candidate_id"]),
        }
        chain["state"] = "PUBLISHED"
        chain["publish_evidence"] = evidence
        chain["recorded_steps"]["published"] = True
        chain["revision"] = chain["revision"] + 1
        self.store.write("chain", key, chain)
        public = {
            "state": "PUBLISHED",
            "revision": chain["revision"],
            "candidate_id": chain["candidate_id"],
            "publish_evidence": evidence,
            "publish_performed": False,
            "production_publish_authorized": False,
        }
        self._remember(begun, public, "PUBLISHED")
        self._trace("publish.observe", task_id=task_id, candidate_id=chain["candidate_id"], performed=False)
        return _ok("PUBLISHED", public)

    def _mark_unknown(self, operation_id, kind, task_id, chain):
        existing = self.store.read("operation", operation_id)
        if existing is not None:
            return
        self.store.write("operation", operation_id, {
            "operation_id": operation_id,
            "kind": kind,
            "state": "UNKNOWN",
            "reconciled": False,
            "revision": 1,
            "task_id": task_id,
            "run_id": None if not isinstance(chain, dict) else chain.get("run_id"),
            "candidate_id": None if not isinstance(chain, dict) else chain.get("candidate_id"),
            "feedback_id": None if not isinstance(chain, dict) else chain.get("feedback_id"),
        })

    def _set_feedback_state(self, op, state):
        feedback_id = op.get("feedback_id")
        if not feedback_id:
            return
        row = self.store.read("feedback", feedback_id)
        if row is None:
            return
        row["state"] = state
        row["revision"] = row["revision"] + 1
        self.store.write("feedback", feedback_id, row)

    def _apply_reconciled_success(self, op, observed):
        feedback_id = op.get("feedback_id")
        if not feedback_id or observed.get("issue_number") is None:
            return
        row = self.store.read("feedback", feedback_id)
        if row is None:
            return
        row["state"] = "SYNCED"
        row["issue_number"] = observed["issue_number"]
        row["revision"] = row["revision"] + 1
        self.store.write("feedback", feedback_id, row)

    def _begin(
        self, *, actor, scope, operation, candidate_id, idempotency_key, payload,
        expected_revision, kind, object_id, create=False, stop_before_idempotency=False,
    ):
        if not isinstance(idempotency_key, str) or not idempotency_key:
            return _denied(422, "SCHEMA_UNSUPPORTED", "REJECTED", {"reason": "idempotency_key"})
        if not isinstance(scope, dict) or not scope.get("repo") or not scope.get("task_id"):
            return _denied(422, "SCHEMA_UNSUPPORTED", "REJECTED", {"reason": "scope"})
        if not isinstance(expected_revision, int) or expected_revision < 0:
            return _denied(422, "SCHEMA_UNSUPPORTED", "REJECTED", {"reason": "expected_revision"})
        authn = self.auth.authenticate(actor)
        if not isinstance(authn, dict) or authn.get("blocked"):
            return _pending(body={"operation": operation})
        proof = str((actor or {}).get("proof_ref", ""))
        if not authn.get("ok") or (actor or {}).get("kind") != "test" or not proof.startswith("test-proof://"):
            self._trace("auth.denied", operation=operation, stage="authenticate")
            return _denied(403, "NOT_AUTHORIZED", "REJECTED", {"operation": operation})
        if operation == "release.publish":
            self.auth.authorize(actor, scope, operation, candidate_id)
            self._trace("publish.denied", operation=operation)
            return _denied(403, "POLICY_DENIED", "REJECTED", {
                "allow": False,
                "operation": operation,
                "production_publish_authorized": False,
                "reason": "publish_not_authorized",
            })
        authz = self.auth.authorize(actor, scope, operation, candidate_id)
        if not isinstance(authz, dict) or authz.get("blocked"):
            return _pending(body={"operation": operation})
        if not authz.get("ok") or authz.get("allow") is False:
            code = authz.get("code") or "POLICY_DENIED"
            self._trace("auth.denied", operation=operation, stage="authorize", code=code)
            return _denied(403, code, "REJECTED", {"operation": operation})
        if stop_before_idempotency:
            return {"principal": authn.get("principal_id"), "doc": self.store.read(kind, object_id)}
        idem_key = _hash(json.dumps({
            "principal": authn.get("principal_id"),
            "repo": scope["repo"],
            "operation": operation,
            "key": idempotency_key,
        }, sort_keys=True))
        payload_hash = _hash(payload)
        existing = self.store.read("idem", idem_key)
        doc = self.store.read(kind, object_id)
        if (
            doc is not None
            and payload.get("author_id")
            and doc.get("author_id")
            and payload["author_id"] != doc["author_id"]
        ):
            self._trace("feedback.hijack", feedback_id=doc.get("feedback_id"), actor=(actor or {}).get("id"))
            return _denied(403, "NOT_AUTHORIZED", doc.get("state", "REJECTED"), {"reason": "cross_author"})
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                return _denied(409, "IDEMPOTENCY_PAYLOAD_MISMATCH", existing.get("state", "REJECTED"))
            body = dict(existing["public"])
            body["replayed"] = True
            body["lease_token"] = None
            body["current_state"] = None if doc is None else doc.get("state", doc.get("phase"))
            return _ok(body.get("state", existing.get("state")), body, changed=False, code="IDEMPOTENCY_REPLAY", replayed=True)
        current = 0 if doc is None else doc["revision"]
        if create and doc is not None and kind != "cursor" and kind != "snapshot":
            current = doc["revision"]
        if not create and doc is None:
            return _denied(409, "EVIDENCE_MISSING", "ABSENT", {"object_id": object_id})
        if expected_revision != current and not (create and doc is None and expected_revision == 0):
            if not (create and kind in {"cursor", "snapshot"} and expected_revision == current):
                return _denied(409, "CONFLICT_REVISION", "ABSENT" if doc is None else doc.get("state", doc.get("phase")), {
                    "expected_revision": expected_revision,
                    "revision": current,
                })
        return {
            "principal": authn.get("principal_id"),
            "idem_key": idem_key,
            "payload_hash": payload_hash,
            "doc": doc,
            "operation": operation,
        }

    def _remember(self, begun, public, state):
        self.store.write("idem", begun["idem_key"], {
            "payload_hash": begun["payload_hash"],
            "state": state,
            "public": {key: value for key, value in public.items() if key != "lease_token"},
        })

    def _commit_event(self, begun, event_type, doc, **fields):
        event = {
            "event_type": event_type,
            "event_class": "task",
            "operation": begun["operation"],
            "state": doc.get("state", doc.get("phase")),
            "test_double": True,
            "w01_proof": False,
        }
        event.update(fields)
        appended = self.events.append(event)
        if not isinstance(appended, dict) or appended.get("blocked") or not appended.get("applied"):
            self._trace("event.pending", event_type=event_type, applied=False)
            return False
        return True

    def _trace(self, action, **fields):
        fields["action"] = action
        fields["at"] = self.clock()
        fields["production"] = False
        fields["github_proof"] = False
        fields["macci_proof"] = False
        self.store.append_trace(fields)


def _tuple_diff(chain, head, base, merge, tree):
    for field, value in (("head", head), ("base", base), ("merge", merge), ("tree", tree)):
        if value != chain.get(field):
            return field
    return None
