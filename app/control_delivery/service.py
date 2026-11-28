"""Isolated automatic delivery loop for V0.6.0 W05.

Scope is F02, F03, F06, F07, F08, F09, F10, and R08. Merge and deploy are
protocol records only. User acceptance and publish authorization are not
forged: protocol stamps stay ``test_double`` and production flags stay false.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading

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
from app.control_delivery.ports import event_identity_digest
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


def operation_id_for(scope, kind, request):
    scope_exact = {"repo": scope["repo"], "task_id": scope["task_id"]}
    digest = _hash({"scope": scope_exact, "kind": kind, "request": request})
    return "op_" + digest[:32], digest, scope_exact


def _fence_key(kind, key):
    return kind + "__" + key


def _binding_holds(observed, expected):
    author = observed.get("author_id")
    scope = observed.get("scope")
    if not isinstance(author, str) or not author or author != expected.get("author_id"):
        return False
    if observed.get("repo") != expected.get("repo"):
        return False
    if not isinstance(observed.get("request_digest"), str) or observed.get("request_digest") != expected.get("request_digest"):
        return False
    wanted = expected.get("scope") or {}
    if not isinstance(scope, dict) or scope.get("repo") != wanted.get("repo") or scope.get("task_id") != wanted.get("task_id"):
        return False
    return True


def _expected_binding(row, scope, digest, scope_exact):
    return {
        "author_id": row.get("author_id"),
        "repo": scope.get("repo"),
        "scope": scope_exact,
        "request_digest": digest,
    }


def _expected_from_operation(existing, row):
    scope = existing.get("scope") if isinstance(existing.get("scope"), dict) else {}
    return {
        "author_id": row.get("author_id") or existing.get("author_id"),
        "repo": scope.get("repo"),
        "scope": scope,
        "request_digest": existing.get("request_digest"),
    }


def _revision(doc):
    if not isinstance(doc, dict):
        return 0
    return int(doc.get("revision") or 0)


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
        self._local = threading.local()
        self._locks_guard = threading.Lock()
        self._operation_locks = {}

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
        existing = begun["doc"]
        if existing is not None:
            if existing["author_id"] != author_id:
                self._trace("feedback.hijack", feedback_id=feedback_id, actor=actor.get("id"))
                return _denied(403, "NOT_AUTHORIZED", existing["state"], {"reason": "cross_author"})
            return _denied(409, "CONFLICT_REVISION", existing["state"], {"reason": "feedback_exists"})
        if author_id != begun["principal"]:
            return _denied(403, "NOT_AUTHORIZED", "REJECTED", {"reason": "principal"})
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
            "scope_task_id": scope["task_id"],
        }
        public = {"feedback_id": feedback_id, "state": "PENDING", "revision": 1}
        outcome = self._commit_event(
            begun, "feedback.submitted", [("feedback", feedback_id, doc, 0)], public, "PENDING",
            feedback_id=feedback_id,
        )
        if isinstance(outcome, Result):
            return outcome
        if not outcome:
            return _pending(body={"feedback_id": feedback_id})
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
        principal = begun["principal"]
        if row["author_id"] != principal:
            self._trace("feedback.hijack", feedback_id=feedback_id, actor=actor.get("id"))
            return _denied(403, "NOT_AUTHORIZED", row["state"], {"reason": "cross_author"})
        if row["state"] == "UNKNOWN":
            return _denied(409, "SIDE_EFFECT_UNKNOWN", "UNKNOWN", {"feedback_id": feedback_id, "retry_allowed": False})
        lease = row.get("lease")
        if lease and lease["expires_at"] > self.clock() and lease["principal_id"] != principal:
            return _denied(409, "LEASE_FENCE_REJECTED", row["state"], {"changed": False})
        generation = 1 if not lease else lease["generation"] + 1
        expected = row["revision"]
        pending = self.store.read("outbox", self._event_id(begun, "feedback.claimed"))
        if pending is None:
            claim = {"principal_id": principal, "generation": generation,
                     "token": "opaque-" + secrets.token_hex(16), "expires_at": self.clock() + 60}
        else:
            claim = pending.get("pending_lease")
            if (not isinstance(claim, dict) or claim.get("principal_id") != principal
                    or claim.get("generation") != generation or not isinstance(claim.get("token"), str)
                    or type(claim.get("expires_at")) not in (int, float)):
                return _denied(409, "SIDE_EFFECT_UNKNOWN", row["state"], {"reason": "missing_lease_intent"})
        token = claim["token"]
        row["lease"] = claim
        begun["pending_lease"] = claim
        row["state"] = "CLAIMED"
        row["revision"] = expected + 1
        public = {
            "feedback_id": feedback_id,
            "state": "CLAIMED",
            "revision": row["revision"],
            "lease_generation": generation,
        }
        outcome = self._commit_event(
            begun, "feedback.claimed", [("feedback", feedback_id, row, expected)], public, "CLAIMED",
            feedback_id=feedback_id,
        )
        if isinstance(outcome, Result):
            return outcome
        if not outcome:
            return _pending(body={"feedback_id": feedback_id})
        self._trace("feedback.claim", feedback_id=feedback_id, state="CLAIMED", token_issued=True)
        shown = dict(public)
        shown["lease_token"] = token if claim["expires_at"] > self.clock() else None
        return _ok("CLAIMED", shown)

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
        principal = begun["principal"]
        if row["author_id"] != principal:
            self._trace("feedback.hijack", feedback_id=feedback_id, actor=actor.get("id"))
            return _denied(403, "NOT_AUTHORIZED", row["state"], {"reason": "cross_author"})
        lease = row.get("lease") or {}
        if (
            row["state"] != "CLAIMED"
            or lease.get("token") != token
            or lease.get("principal_id") != principal
            or lease.get("expires_at", 0) <= self.clock()
        ):
            self._trace("feedback.fence", feedback_id=feedback_id, state=row["state"])
            return _denied(409, "LEASE_FENCE_REJECTED", row["state"], {"changed": False, "retry_allowed": False})
        if not credential_present:
            return _denied(403, "CREDENTIAL_ABSENT", row["state"], {"remote_called": False})
        before = len(self.remote.calls)
        outcome = self._push_row(principal, scope, row)
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
            cursor = dict(begun["doc"] or {"revision": 0, "cursor": None})
            cursor["revision"] = begun["observed_revision"] + 1
            cursor["state"] = "BLOCKED"
            cursor["repo_id"] = scope["repo"]
            cursor["scope_task_id"] = scope["task_id"]
            failed = self._commit_local(
                begun, [("cursor", "outbound", cursor, begun["observed_revision"])], public, "BLOCKED",
            )
            if isinstance(failed, Result):
                return failed
            return Result(
                ok=False, http=403, code="CREDENTIAL_ABSENT", state="BLOCKED",
                changed=False, body=public, blocked=True,
            )

        rows = [
            row for row in self.store.list("feedback")
            if row["state"] in {"PENDING", "FAILED"}
            and row.get("author_id") == begun["principal"]
            and row.get("repo_id") == scope["repo"]
            and row.get("scope_task_id") == scope["task_id"]
        ]
        rows.sort(key=lambda row: row["feedback_id"])
        results = []
        processed = 0
        remainder = None
        for row in rows:
            if batch_limit is not None and processed >= batch_limit:
                remainder = row["feedback_id"]
                break
            outcome = self._push_row(begun["principal"], scope, row)
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
        cursor = dict(begun["doc"] or {"revision": 0})
        cursor["revision"] = begun["observed_revision"] + 1
        cursor["cursor"] = remainder
        cursor["state"] = "SYNCED"
        cursor["retained_ids"] = sorted(row["feedback_id"] for row in self.store.list("feedback"))
        cursor["repo_id"] = scope["repo"]
        cursor["scope_task_id"] = scope["task_id"]
        failed = self._commit_local(
            begun, [("cursor", "outbound", cursor, begun["observed_revision"])], public, "SYNCED",
        )
        if isinstance(failed, Result):
            return failed
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
        if row["author_id"] != begun["principal"]:
            self._trace("feedback.hijack", feedback_id=feedback_id, actor=actor.get("id"))
            return _denied(403, "NOT_AUTHORIZED", row["state"], {"reason": "cross_author"})
        if not row.get("issue_number"):
            return _denied(409, "DEPENDENCY_UNSATISFIED", row["state"], {"reason": "issue_unlinked"})
        before = sorted(item["feedback_id"] for item in self.store.list("feedback"))
        page = 1
        applied = 0
        while True:
            if page_budget is not None and page > page_budget:
                expected = begun["observed_revision"]
                row["inbound_complete"] = False
                row["revision"] = expected + 1
                public = {
                    "feedback_id": feedback_id,
                    "state": row["state"],
                    "revision": row["revision"],
                    "inbound_complete": False,
                    "complete": False,
                    "retained_ids": before,
                }
                failed = self._commit_local(
                    begun, [("feedback", feedback_id, row, expected)], public, row["state"],
                )
                if isinstance(failed, Result):
                    return failed
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
        expected = begun["observed_revision"]
        row["revision"] = expected + 1
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
        failed = self._commit_local(
            begun, [("feedback", feedback_id, row, expected)], public, row["state"],
        )
        if isinstance(failed, Result):
            return failed
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
            "actor_id": begun["principal"],
            "completion_condition": (
                "every required chain is PUBLISHED and each chain has publish_evidence "
                "and task_acceptance is recorded"
            ),
        }
        pairs = [("task", task_id, task, 0)]
        for chain in built:
            chain["run_id"] = run_id
            pairs.append(("chain", task_id + "__" + chain["chain_id"], chain, 0))
        for feedback_id in feedback_ids:
            row = self.store.read("feedback", feedback_id)
            if row is None:
                continue
            old = row["revision"]
            row = dict(row)
            row["task_id"] = task_id
            row["revision"] = old + 1
            pairs.append(("feedback", feedback_id, row, old))
        public = {
            "task_id": task_id,
            "state": "claim",
            "revision": 1,
            "run_id": run_id,
            "chain_ids": [chain["chain_id"] for chain in built],
            "candidate_ids": [chain["candidate_id"] for chain in built],
            "completion_condition": task["completion_condition"],
        }
        outcome = self._commit_event(
            begun, "task.claimed", pairs, public, "claim", task_id=task_id, run_id=run_id,
        )
        if isinstance(outcome, Result):
            return outcome
        if not outcome:
            return _pending(body={"task_id": task_id})
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
        expected = begun["observed_revision"]
        chain["revision"] = expected + 1
        stored_evidence = self.store.read("evidence", cid)
        evidence_expected = 0 if stored_evidence is None else _revision(stored_evidence)
        evidence["revision"] = evidence_expected + 1
        public = {
            "task_id": task_id,
            "chain_id": chain_id,
            "state": chain["state"],
            "revision": chain["revision"],
            "candidate_id": cid,
            "evidence_valid": chain["evidence_valid"],
            "proof_class": "test_double",
        }
        outcome = self._commit_event(
            begun, "evidence.bound",
            [("chain", key, chain, expected), ("evidence", cid, evidence, evidence_expected)],
            public, chain["state"], task_id=task_id, run_id=run_id, candidate_id=cid,
        )
        if isinstance(outcome, Result):
            return outcome
        if not outcome:
            return _pending(body={"candidate_id": cid})
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
            failed = self._commit_local(begun, [], public, chain["state"])
            if isinstance(failed, Result):
                return failed
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
        expected = begun["observed_revision"]
        chain["revision"] = expected + 1
        pairs = [("chain", key, chain, expected)]
        if evidence is not None:
            evidence["revision"] = int(evidence.get("revision") or 1) + 1
            pairs.append(("evidence", chain["candidate_id"], evidence, int(evidence["revision"]) - 1))
        public = {
            **_limits(),
            "state": chain["state"],
            "revision": chain["revision"],
            "evidence_valid": False,
            "changed_field": changed_field,
            "candidate_id": chain["candidate_id"],
            "prior_evidence_reused": False,
        }
        outcome = self._commit_event(
            begun, "candidate.stale", pairs, public, chain["state"],
            task_id=task_id, candidate_id=chain["candidate_id"],
        )
        if isinstance(outcome, Result):
            return outcome
        if not outcome:
            return _pending(body={"candidate_id": chain["candidate_id"]})
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
        grant = self._acceptance_grant(begun["principal"], chain, signer_kind, decision)
        if isinstance(grant, Result):
            return grant
        guard = "user_acceptance" if grant["decision"] == "accepted" else "user_rejection"
        target = "SATISFIED" if grant["decision"] == "accepted" else "REJECTED"
        if not allows("ACCEPTING", target, guard):
            return _denied(409, "DEPENDENCY_UNSATISFIED", chain["state"])
        expected = begun["observed_revision"]
        chain["state"] = target
        chain["acceptance_decision"] = grant["decision"]
        chain["acceptance_proof_class"] = "test_double"
        chain["acceptance_binding"] = {
            "candidate_id": chain["candidate_id"],
            "head": chain["head"],
            "base": chain["base"],
            "merge": chain["merge"],
            "tree": chain["tree"],
            "decision": grant["decision"],
            "principal_id": begun["principal"],
            "proof_class": "test_double",
            "runtime_acceptance": False,
        }
        chain["recorded_steps"]["accept"] = True
        chain["revision"] = expected + 1
        public = {
            "state": chain["state"],
            "revision": chain["revision"],
            "acceptance_decision": grant["decision"],
            "acceptance_proof_class": "test_double",
            "counts_as_runtime_user_acceptance": False,
            "forged_user_acceptance": False,
            "production_publish_authorized": False,
            "github_proof": False,
            "macci_proof": False,
        }
        outcome = self._commit_event(
            begun, "acceptance.recorded", [("chain", key, chain, expected)], public, chain["state"],
            task_id=task_id, candidate_id=chain["candidate_id"],
        )
        if isinstance(outcome, Result):
            return outcome
        if not outcome:
            return _pending(body={"candidate_id": chain["candidate_id"]})
        self._trace(
            "acceptance.record", task_id=task_id, chain_id=chain_id,
            decision=grant["decision"], state=chain["state"],
        )
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
        for chain in required:
            confirmed = self._acceptance_grant(begun["principal"], chain, "user_acceptance", "accepted")
            if isinstance(confirmed, Result):
                return confirmed
            if confirmed["decision"] != "accepted":
                return _denied(409, "DEPENDENCY_UNSATISFIED", task["phase"], {"reason": "chains_not_accepted"})
        expected = begun["observed_revision"]
        task["task_acceptance"] = {
            "proof_class": "test_double",
            "counts_as_runtime_user_acceptance": False,
            "recorded_at": self.clock(),
            "signer_kind": "user_acceptance",
            "runtime_acceptance": False,
        }
        task["revision"] = expected + 1
        public = {
            "state": task["phase"],
            "revision": task["revision"],
            "task_acceptance": task["task_acceptance"],
            "counts_as_runtime_user_acceptance": False,
        }
        failed = self._commit_local(begun, [("task", task_id, task, expected)], public, task["phase"])
        if isinstance(failed, Result):
            return failed
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
        outcome = self._push_row(begun["principal"], scope, self.store.read("feedback", feedback_id))
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

    def project(self, *, actor, task_id, scope=None):
        if not isinstance(scope, dict) or not scope.get("repo") or not scope.get("task_id"):
            return _denied(403, "NOT_AUTHORIZED", "REJECTED", {"reason": "scope"})
        authn = self.auth.authenticate(actor)
        if not isinstance(authn, dict) or authn.get("blocked"):
            return _pending()
        principal = authn.get("principal_id")
        proof = str((actor or {}).get("proof_ref", ""))
        if (
            not authn.get("ok")
            or (actor or {}).get("kind") != "test"
            or not proof.startswith("test-proof://")
            or not isinstance(principal, str)
            or not principal
        ):
            return _denied(403, "NOT_AUTHORIZED", "REJECTED")
        task = self.store.read("task", task_id)
        if task is None:
            return _denied(409, "EVIDENCE_MISSING", "ABSENT")
        if task.get("repo_id") != scope["repo"] or task.get("task_id") != scope["task_id"]:
            return _denied(403, "NOT_AUTHORIZED", task.get("phase", "REJECTED"), {"reason": "object_scope"})
        authz = self.auth.authorize(
            actor, {"repo": task["repo_id"], "task_id": task["task_id"]}, "task.project", None,
        )
        if not isinstance(authz, dict) or authz.get("blocked"):
            return _pending()
        if authz.get("ok") is not True or authz.get("allow") is not True:
            return _denied(403, authz.get("code") or "NOT_AUTHORIZED", task.get("phase", "REJECTED"))
        view = project_task(task, self._chains(task_id))
        view["completion"] = completion(task, self._chains(task_id))
        return _ok(task["phase"], view, changed=False)

    def feedback(self, feedback_id, *, actor=None, scope=None):
        if not self._read_allowed(actor, scope, "feedback", feedback_id):
            return None
        row = self.store.read("feedback", feedback_id)
        if isinstance(row, dict) and isinstance(row.get("lease"), dict):
            row["lease"] = {key: value for key, value in row["lease"].items() if key != "token"}
        return row

    def traces(self, *, actor=None, scope=None):
        if not self._read_allowed(actor, scope, "trace", None):
            return []
        rows = []
        for row in self.store.traces():
            if row.get("repo_id") not in (None, scope["repo"]):
                continue
            rows.append(row)
        return rows

    def _chains(self, task_id):
        rows = [
            row for row in self.store.list("chain")
            if row.get("task_id") == task_id
        ]
        rows.sort(key=lambda row: row["chain_id"])
        return rows

    def _operation_lock(self, operation_id):
        with self._locks_guard:
            lock = self._operation_locks.get(operation_id)
            if lock is None:
                lock = threading.Lock()
                self._operation_locks[operation_id] = lock
            return lock

    def _read_allowed(self, actor, scope, kind, object_id):
        if not isinstance(actor, dict) or not isinstance(scope, dict):
            return False
        if not scope.get("repo") or not scope.get("task_id"):
            return False
        authn = self.auth.authenticate(actor)
        if not isinstance(authn, dict) or authn.get("blocked") or not authn.get("ok"):
            return False
        principal = authn.get("principal_id")
        proof = str(actor.get("proof_ref", ""))
        if (
            actor.get("kind") != "test"
            or not proof.startswith("test-proof://")
            or not isinstance(principal, str)
            or not principal
        ):
            return False
        doc = None if object_id is None else self.store.read(kind, object_id)
        if isinstance(doc, dict):
            if doc.get("repo_id") and doc.get("repo_id") != scope["repo"]:
                return False
            if doc.get("scope_task_id") and doc.get("scope_task_id") != scope["task_id"]:
                return False
            if kind != "feedback" and doc.get("task_id") and doc.get("task_id") != scope["task_id"]:
                return False
        authz = self.auth.authorize(
            actor, {"repo": scope["repo"], "task_id": scope["task_id"]}, "read." + kind, None,
        )
        if not isinstance(authz, dict) or authz.get("blocked"):
            return False
        return authz.get("ok") is True and authz.get("allow") is True

    def _object_mismatch(self, scope, kind, doc, payload):
        payload = payload or {}
        if payload.get("task_id") and payload["task_id"] != scope["task_id"]:
            if kind in {"task", "chain", "snapshot", "feedback", "cursor"}:
                return "forged_scope"
        if payload.get("chain_id") and kind == "chain" and isinstance(doc, dict):
            if doc.get("chain_id") and payload["chain_id"] != doc["chain_id"]:
                return "forged_scope"
        if not isinstance(doc, dict):
            return None
        if doc.get("repo_id") and doc.get("repo_id") != scope["repo"]:
            return "cross_repo"
        if kind in {"task", "chain", "snapshot"} and doc.get("task_id") and doc["task_id"] != scope["task_id"]:
            return "forged_scope"
        if kind == "feedback" and doc.get("scope_task_id") and doc["scope_task_id"] != scope["task_id"]:
            return "forged_scope"
        if kind == "cursor":
            if doc.get("scope_task_id") and doc["scope_task_id"] != scope["task_id"]:
                return "forged_scope"
        if kind == "operation":
            op_scope = doc.get("scope") or {}
            if op_scope.get("repo") and op_scope.get("repo") != scope["repo"]:
                return "cross_repo"
            if op_scope.get("task_id") and op_scope.get("task_id") != scope["task_id"]:
                return "forged_scope"
        return None

    def _acceptance_grant(self, principal, chain, signer_kind, caller_decision):
        denied_body = {
            "reason": "stand_in_not_real_authorization",
            "forged_user_acceptance": False,
            "counts_as_runtime_user_acceptance": False,
            "github_proof": False,
            "macci_proof": False,
            "production_publish_authorized": False,
        }
        if getattr(self.auth, "is_test_double", False) is not True:
            return _denied(403, "POLICY_DENIED", chain["state"], denied_body)
        status = self.schedule.evidence(chain["candidate_id"])
        if not isinstance(status, dict) or status.get("blocked"):
            return _pending(body={"candidate_id": chain["candidate_id"]})
        if status.get("macci_proof") or status.get("runtime_evidence"):
            return _denied(403, "POLICY_DENIED", chain["state"], denied_body)
        local = chain.get("test_evidence") or {}
        tuple_ok = (
            status.get("known") is True
            and status.get("valid") is True
            and status.get("candidate_id") == chain["candidate_id"]
            and status.get("head") == chain["head"]
            and status.get("base") == chain["base"]
            and status.get("merge") == chain["merge"]
            and status.get("tree") == chain["tree"]
            and chain.get("evidence_valid") is True
            and local.get("candidate_id") == chain["candidate_id"]
            and local.get("head") == chain["head"]
            and local.get("base") == chain["base"]
            and local.get("merge") == chain["merge"]
            and local.get("tree") == chain["tree"]
            and local.get("valid") is not False
        )
        if not tuple_ok:
            return _denied(409, "EVIDENCE_MISSING", chain["state"], {
                "reason": "acceptance_tuple",
                "candidate_id": chain["candidate_id"],
                "forged_user_acceptance": False,
            })
        binding = {
            "repo": chain.get("repo_id"),
            "task_id": chain.get("task_id"),
            "chain_id": chain.get("chain_id"),
            "candidate_id": chain["candidate_id"],
            "head": chain["head"],
            "base": chain["base"],
            "merge": chain["merge"],
            "tree": chain["tree"],
            "decision": caller_decision,
        }
        grant = self.auth.acceptance(principal, binding)
        if not isinstance(grant, dict) or grant.get("blocked"):
            return _pending(body={"candidate_id": chain["candidate_id"]})
        promoted = (
            grant.get("test_double") is not True
            or grant.get("allow") is not True
            or grant.get("ok") is not True
            or grant.get("runtime_acceptance")
            or grant.get("github_proof")
            or grant.get("macci_proof")
            or grant.get("counts_as_runtime_user_acceptance")
            or grant.get("signer_kind") != "user_acceptance"
            or grant.get("decision") not in {"accepted", "rejected"}
            or signer_kind != "user_acceptance"
            or caller_decision != grant.get("decision")
        )
        if promoted:
            return _denied(403, "POLICY_DENIED", chain["state"], denied_body)
        for field in ("repo", "task_id", "chain_id", "candidate_id", "head", "base", "merge", "tree"):
            if grant.get(field) != binding[field]:
                return _denied(409, "EVIDENCE_MISSING", chain["state"], {
                    "reason": "acceptance_tuple",
                    "candidate_id": chain["candidate_id"],
                })
        return grant

    def _trusted_reconcile(self, operation_id, expected):
        observed = self.remote.reconcile(operation_id)
        if not isinstance(observed, dict) or observed.get("blocked"):
            return {"classification": "unknown", "blocked": bool(isinstance(observed, dict) and observed.get("blocked"))}
        if observed.get("operation_id") != operation_id:
            return {"classification": "unknown", "reason": "operation"}
        classification = observed.get("classification")
        if classification not in {"succeeded", "failed", "absent", "unknown"}:
            return {"classification": "unknown", "reason": "classification"}
        if classification == "unknown":
            return {"classification": "unknown", "reason": "remote"}
        if not _binding_holds(observed, expected):
            return {"classification": "unknown", "reason": "unbound", "blocked": False}
        return {
            "classification": classification,
            "issue_number": observed.get("issue_number"),
            "feedback_id": observed.get("feedback_id"),
            "author_id": observed.get("author_id"),
            "repo": observed.get("repo"),
            "scope": observed.get("scope"),
            "request_digest": observed.get("request_digest"),
            "github_proof": False,
            "test_double": observed.get("test_double", True),
        }

    def _push_request(self, scope, row):
        return {
            "feedback_id": row["feedback_id"],
            "author_id": row["author_id"],
            "body": row["body"],
            "attempt": int(row.get("attempt") or 1),
        }

    def _intent_document(self, op_id, digest, scope_exact, principal, row, request):
        return {
            "operation_id": op_id,
            "kind": "feedback.create_issue",
            "state": "INTENT",
            "scope": scope_exact,
            "request_digest": digest,
            "feedback_id": row["feedback_id"],
            "author_id": row["author_id"],
            "attempt": request["attempt"],
            "revision": 1,
            "generation": 1,
            "reconciled": False,
            "principal_id": principal,
            "github_proof": False,
            "test_double": True,
        }

    def _mutate_effect(self, feedback_id, op_doc, expected_generation, feedback_state, issue_number):
        def mutator(view):
            current = view.read("feedback", feedback_id)
            if not isinstance(current, dict):
                view.abort()
                return None
            stored = view.read("operation", op_doc["operation_id"])
            generation = 0 if stored is None else int(stored.get("generation") or 0)
            if generation != expected_generation:
                view.abort()
                return "conflict"
            updated = dict(current)
            updated["state"] = feedback_state
            updated["operation_id"] = op_doc["operation_id"]
            updated["lease"] = None
            if issue_number is not None:
                updated["issue_number"] = issue_number
            updated["revision"] = _revision(current) + 1
            op_doc["generation"] = generation + 1
            op_doc["github_proof"] = False
            view.stage("feedback", feedback_id, updated)
            view.stage("operation", op_doc["operation_id"], dict(op_doc))
            return updated

        return self.store.mutate(mutator)

    def _synced_result(self, feedback_id, issue_number, created):
        return {
            "feedback_id": feedback_id,
            "state": "SYNCED",
            "issue_number": issue_number,
            "created": created,
        }

    def _push_row(self, principal, scope, row):
        feedback_id = row["feedback_id"]
        if row.get("author_id") != principal or row.get("repo_id") != scope["repo"] or row.get("scope_task_id") != scope["task_id"]:
            self._trace("feedback.hijack", feedback_id=feedback_id, actor=principal)
            return {
                "feedback_id": feedback_id,
                "state": row.get("state"),
                "code": "NOT_AUTHORIZED",
                "reason": "cross_author" if row.get("author_id") != principal else "object_scope",
            }
        if row.get("state") == "UNKNOWN":
            return {
                "feedback_id": feedback_id,
                "state": "UNKNOWN",
                "code": "SIDE_EFFECT_UNKNOWN",
                "operation_id": row.get("operation_id"),
                "retry_allowed": False,
            }
        request = self._push_request(scope, row)
        op_id, digest, scope_exact = operation_id_for(scope, "feedback.create_issue", request)
        with self._operation_lock(op_id):
            existing = self.store.read("operation", op_id)
            if isinstance(existing, dict) and existing.get("state") == "SUCCEEDED" and existing.get("issue_number") is not None:
                self._mutate_effect(feedback_id, dict(existing), int(existing.get("generation") or 0), "SYNCED", existing["issue_number"])
                return self._synced_result(feedback_id, existing["issue_number"], False)
            if isinstance(existing, dict) and existing.get("state") in {"INTENT", "UNKNOWN"}:
                return self._recover_push(row, existing)
            if isinstance(existing, dict) and existing.get("state") == "FAILED":
                return {"feedback_id": feedback_id, "state": "FAILED", "code": "FAILED", "continued": True}
            if isinstance(existing, dict) and existing.get("state") == "RETRY_WAIT":
                return {
                    "feedback_id": feedback_id,
                    "state": row.get("state"),
                    "code": "SIDE_EFFECT_UNKNOWN",
                    "retry_allowed": False,
                    "operation_id": op_id,
                }
            if existing is None:
                observed = self._trusted_reconcile(op_id, _expected_binding(row, scope, digest, scope_exact))
                if observed.get("reason") == "unbound":
                    observed = {"classification": "unproven"}
                if observed.get("classification") == "succeeded" and observed.get("issue_number") is not None:
                    if observed.get("author_id") not in (None, row["author_id"]):
                        return {
                            "feedback_id": feedback_id,
                            "state": row.get("state"),
                            "code": "NOT_AUTHORIZED",
                            "reason": "cross_author",
                        }
                    op_doc = self._intent_document(op_id, digest, scope_exact, principal, row, request)
                    op_doc["state"] = "SUCCEEDED"
                    op_doc["reconciled"] = True
                    op_doc["classification"] = "succeeded"
                    op_doc["issue_number"] = observed["issue_number"]
                    op_doc["revision"] = 1
                    self._mutate_effect(feedback_id, op_doc, 0, "SYNCED", observed["issue_number"])
                    return self._synced_result(feedback_id, observed["issue_number"], False)
                if observed.get("classification") == "unknown":
                    op_doc = self._intent_document(op_id, digest, scope_exact, principal, row, request)
                    op_doc["state"] = "UNKNOWN"
                    op_doc["classification"] = "unknown"
                    self._mutate_effect(feedback_id, op_doc, 0, "UNKNOWN", None)
                    return {
                        "feedback_id": feedback_id,
                        "state": "UNKNOWN",
                        "code": "SIDE_EFFECT_UNKNOWN",
                        "operation_id": op_id,
                        "retry_allowed": False,
                    }
                if observed.get("classification") == "failed":
                    op_doc = self._intent_document(op_id, digest, scope_exact, principal, row, request)
                    op_doc["state"] = "FAILED"
                    op_doc["reconciled"] = True
                    op_doc["classification"] = "failed"
                    self._mutate_effect(feedback_id, op_doc, 0, "FAILED", None)
                    return {"feedback_id": feedback_id, "state": "FAILED", "code": "FAILED", "continued": True}
            looked = self.remote.lookup_issue(feedback_id)
            if isinstance(looked, dict) and looked.get("blocked"):
                return {"feedback_id": feedback_id, "state": row.get("state"), "code": "INTERFACE_PENDING", "blocked": True}
            if isinstance(looked, dict) and looked.get("found"):
                if looked.get("author_id") != row["author_id"]:
                    self._trace("feedback.hijack", feedback_id=feedback_id, issue_author=looked.get("author_id"))
                    return {
                        "feedback_id": feedback_id,
                        "state": row.get("state"),
                        "code": "NOT_AUTHORIZED",
                        "reason": "cross_author",
                        "issue_number": None,
                    }
                op_doc = self._intent_document(op_id, digest, scope_exact, principal, row, request)
                op_doc["state"] = "SUCCEEDED"
                op_doc["reconciled"] = True
                op_doc["classification"] = "succeeded"
                op_doc["issue_number"] = looked["number"]
                self._mutate_effect(feedback_id, op_doc, 0, "SYNCED", looked["number"])
                return self._synced_result(feedback_id, looked["number"], False)
            op_doc = self._intent_document(op_id, digest, scope_exact, principal, row, request)

            def stage_intent(view):
                current = view.read("operation", op_id)
                if current is not None:
                    view.abort()
                    return current
                view.stage("operation", op_id, op_doc)
                return op_doc

            staged = self.store.mutate(stage_intent)
            if not isinstance(staged, dict) or staged.get("state") != "INTENT":
                current = self.store.read("operation", op_id)
                if isinstance(current, dict):
                    return self._recover_push(row, current)
                return {"feedback_id": feedback_id, "state": row.get("state"), "code": "CONFLICT_REVISION"}
            created = self.remote.create_issue({
                "feedback_id": feedback_id,
                "author_id": row["author_id"],
                "body": row["body"],
                "operation_id": op_id,
                "request_digest": digest,
                "scope": scope_exact,
            })
            if not isinstance(created, dict) or created.get("status") == "unknown":
                observed = self._trusted_reconcile(op_id, _expected_binding(row, scope, digest, scope_exact))
                return self._apply_push_classification(row, op_doc, observed)
            if created.get("status") == "failed" or not created.get("ok", True):
                op_doc["state"] = "FAILED"
                op_doc["reconciled"] = True
                op_doc["classification"] = "failed"
                self._mutate_effect(feedback_id, op_doc, 1, "FAILED", None)
                return {"feedback_id": feedback_id, "state": "FAILED", "code": "FAILED", "continued": True}
            if created.get("author_id") != row["author_id"]:
                return {
                    "feedback_id": feedback_id,
                    "state": row.get("state"),
                    "code": "NOT_AUTHORIZED",
                    "reason": "cross_author",
                }
            op_doc["state"] = "SUCCEEDED"
            op_doc["reconciled"] = True
            op_doc["classification"] = "succeeded"
            op_doc["issue_number"] = created["number"]
            self._mutate_effect(feedback_id, op_doc, 1, "SYNCED", created["number"])
            return self._synced_result(feedback_id, created["number"], created.get("status") == "created")

    def _recover_push(self, row, existing):
        feedback_id = row["feedback_id"]
        observed = self._trusted_reconcile(existing["operation_id"], _expected_from_operation(existing, row))
        return self._apply_push_classification(row, dict(existing), observed)

    def _apply_push_classification(self, row, op_doc, observed):
        feedback_id = row["feedback_id"]
        classification = observed.get("classification")
        generation = int(op_doc.get("generation") or 1)
        if classification == "succeeded" and observed.get("issue_number") is not None:
            if observed.get("author_id") not in (None, row.get("author_id")):
                return {
                    "feedback_id": feedback_id,
                    "state": row.get("state"),
                    "code": "NOT_AUTHORIZED",
                    "reason": "cross_author",
                }
            op_doc["state"] = "SUCCEEDED"
            op_doc["reconciled"] = True
            op_doc["classification"] = "succeeded"
            op_doc["issue_number"] = observed["issue_number"]
            op_doc["revision"] = int(op_doc.get("revision") or 1)
            self._mutate_effect(feedback_id, op_doc, generation, "SYNCED", observed["issue_number"])
            return self._synced_result(feedback_id, observed["issue_number"], False)
        if classification == "failed":
            op_doc["state"] = "FAILED"
            op_doc["reconciled"] = True
            op_doc["classification"] = "failed"
            op_doc["revision"] = int(op_doc.get("revision") or 1)
            self._mutate_effect(feedback_id, op_doc, generation, "FAILED", None)
            return {"feedback_id": feedback_id, "state": "FAILED", "code": "FAILED", "continued": True}
        if classification == "absent":
            op_doc["state"] = "RETRY_WAIT"
            op_doc["reconciled"] = True
            op_doc["classification"] = "absent"
            op_doc["revision"] = int(op_doc.get("revision") or 1)
            self._mutate_effect(feedback_id, op_doc, generation, "RETRY_WAIT", None)
            return {
                "feedback_id": feedback_id,
                "state": "RETRY_WAIT",
                "code": "SIDE_EFFECT_UNKNOWN",
                "operation_id": op_doc["operation_id"],
                "retry_allowed": False,
            }
        op_doc["state"] = "UNKNOWN"
        op_doc["reconciled"] = False
        op_doc["classification"] = "unknown"
        op_doc["revision"] = 1
        self._mutate_effect(feedback_id, op_doc, generation, "UNKNOWN", None)
        return {
            "feedback_id": feedback_id,
            "state": "UNKNOWN",
            "code": "SIDE_EFFECT_UNKNOWN",
            "operation_id": op_doc["operation_id"],
            "retry_allowed": False,
        }

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
        self._local.repo = scope["repo"]
        self._local.task = scope["task_id"]
        authn = self.auth.authenticate(actor)
        if not isinstance(authn, dict) or authn.get("blocked"):
            return _pending(body={"operation": operation})
        principal = authn.get("principal_id")
        proof = str((actor or {}).get("proof_ref", ""))
        if (
            not authn.get("ok")
            or (actor or {}).get("kind") != "test"
            or not proof.startswith("test-proof://")
            or not isinstance(principal, str)
            or not principal
        ):
            self._trace("auth.denied", operation=operation, stage="authenticate")
            return _denied(403, "NOT_AUTHORIZED", "REJECTED", {"operation": operation})
        doc = self.store.read(kind, object_id)
        mismatch = self._object_mismatch(scope, kind, doc, payload)
        if mismatch:
            self._trace("auth.denied", operation=operation, stage="object", reason=mismatch)
            state = "REJECTED" if not isinstance(doc, dict) else doc.get("state", doc.get("phase", "REJECTED"))
            return _denied(403, "NOT_AUTHORIZED", state, {"reason": mismatch, "operation": operation})
        if payload.get("author_id") and payload["author_id"] != principal:
            return _denied(403, "NOT_AUTHORIZED", "REJECTED", {"reason": "principal", "operation": operation})
        if (
            isinstance(doc, dict)
            and payload.get("author_id")
            and doc.get("author_id")
            and payload["author_id"] != doc["author_id"]
        ):
            self._trace("feedback.hijack", feedback_id=doc.get("feedback_id"), actor=(actor or {}).get("id"))
            return _denied(403, "NOT_AUTHORIZED", doc.get("state", "REJECTED"), {"reason": "cross_author"})
        object_scope = {"repo": scope["repo"], "task_id": scope["task_id"]}
        if isinstance(doc, dict):
            if doc.get("chain_id"):
                object_scope["chain_id"] = doc["chain_id"]
            if doc.get("candidate_id"):
                object_scope["candidate_id"] = doc["candidate_id"]
        elif payload.get("chain_id"):
            object_scope["chain_id"] = payload["chain_id"]
        bound_candidate = candidate_id or object_scope.get("candidate_id")
        if operation == "release.publish":
            self.auth.authorize(actor, object_scope, operation, bound_candidate)
            self._trace("publish.denied", operation=operation)
            return _denied(403, "POLICY_DENIED", "REJECTED", {
                "allow": False,
                "operation": operation,
                "production_publish_authorized": False,
                "reason": "publish_not_authorized",
            })
        authz = self.auth.authorize(actor, object_scope, operation, bound_candidate)
        if not isinstance(authz, dict) or authz.get("blocked"):
            return _pending(body={"operation": operation})
        if authz.get("ok") is not True or authz.get("allow") is not True:
            code = authz.get("code") or "POLICY_DENIED"
            self._trace("auth.denied", operation=operation, stage="authorize", code=code)
            return _denied(403, code, "REJECTED", {"operation": operation})
        current = 0 if doc is None else doc["revision"]
        begun = {
            "principal": principal,
            "doc": doc,
            "operation": operation,
            "observed_revision": current,
            "observed_generation": 0 if doc is None else int(doc.get("generation") or current or 0),
            "scope": {"repo": scope["repo"], "task_id": scope["task_id"]},
            "object_kind": kind,
            "object_id": object_id,
        }
        if stop_before_idempotency:
            return begun
        idem_key = _hash({
            "principal": principal,
            "repo": scope["repo"],
            "task_id": scope["task_id"],
            "operation": operation,
            "key": idempotency_key,
        })
        payload_hash = _hash(payload)
        existing = self.store.read("idem", idem_key)
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                return _denied(409, "IDEMPOTENCY_PAYLOAD_MISMATCH", existing.get("state", "REJECTED"))
            body = dict(existing["public"])
            body["replayed"] = True
            body["lease_token"] = None
            body["current_state"] = None if doc is None else doc.get("state", doc.get("phase"))
            return _ok(body.get("state", existing.get("state")), body, changed=False, code="IDEMPOTENCY_REPLAY", replayed=True)
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
        begun["idem_key"] = idem_key
        begun["payload_hash"] = payload_hash
        begun["observed_revision"] = current
        return begun

    def _remember(self, begun, public, state):
        self.store.write("idem", begun["idem_key"], {
            "payload_hash": begun["payload_hash"],
            "state": state,
            "public": {key: value for key, value in public.items() if key != "lease_token"},
        })

    def _commit_local(self, begun, pairs, public, state):
        idem_body = {
            "payload_hash": begun["payload_hash"],
            "state": state,
            "public": {key: value for key, value in public.items() if key != "lease_token"},
        }

        def mutator(view):
            owner = begun.get("event_id")
            for kind, key, doc, expected in pairs:
                current = view.read(kind, key)
                if _revision(current) != expected:
                    view.abort()
                    return "conflict"
                fence = view.read("fence", _fence_key(kind, key))
                if isinstance(fence, dict) and fence.get("state") == "open" and fence.get("event_id") != owner:
                    view.abort()
                    return "conflict"
            for kind, key, doc, expected in pairs:
                view.stage(kind, key, doc)
            if begun.get("idem_key"):
                view.stage("idem", begun["idem_key"], idem_body)
            if begun.get("event_id") and begun.get("receipt"):
                outbox = view.read("outbox", begun["event_id"])
                if not isinstance(outbox, dict) or outbox.get("event_id") != owner or outbox.get("request_digest") != begun["payload_hash"] or _hash(outbox.get("event")) != begun["event_digest"]:
                    view.abort()
                    return "conflict"
                if isinstance(outbox, dict):
                    outbox = dict(outbox)
                    outbox.pop("pending_lease", None)
                    outbox["receipt"] = begun["receipt"]
                    outbox["delivery_state"] = "RECEIPT_RECORDED"
                    outbox["authority"] = "W01"
                    outbox["local_is_authority"] = False
                    view.stage("outbox", begun["event_id"], outbox)
                    # This is a local business commit witness, not a W01 or
                    # production receipt. The recovery reader must independently
                    # validate the actual W01 acknowledgement before promotion.
                    view.stage("business-commit", owner, {
                        "event_id": owner,
                        "principal_id": begun["principal"],
                        "scope": begun["scope"],
                        "operation": begun["operation"],
                        "request_digest": begun["payload_hash"],
                        "idem_key": begun["idem_key"],
                        "state": state,
                        "changes": [
                            {"kind": kind, "key": key, "expected_revision": expected,
                             "revision": _revision(doc), "digest": _hash(doc)}
                            for kind, key, doc, expected in pairs
                        ],
                        "event_digest": _hash(outbox["event"]),
                        "intent_binding": outbox["event"].get("note"),
                        "expected_revision": begun["observed_revision"],
                        "receipt_digest": _hash(begun["receipt"]),
                        "result": idem_body["public"],
                        "authority": "W05_LOCAL",
                        "production_effective": False,
                    })
                for kind, key, doc, expected in pairs:
                    view.stage("fence", _fence_key(kind, key), {
                        "state": "closed",
                        "event_id": begun["event_id"],
                        "revision": 1,
                    })
            return "ok"

        result = self.store.mutate(mutator)
        if result != "ok":
            return _denied(409, "CONFLICT_REVISION", state, {"reason": "cas"})
        return None

    def _stage_outbox(self, begun, event):
        event_id = event["event_id"]
        kind = begun["object_kind"]
        key = begun["object_id"]

        def mutator(view):
            existing = view.read("outbox", event_id)
            if isinstance(existing, dict):
                return existing
            plan = begun.get("change_plan")
            if not isinstance(plan, list) or not plan:
                plan = [{"kind": kind, "key": key, "expected_revision": begun["observed_revision"]}]
            current = None
            for entry in plan:
                current = view.read(entry["kind"], entry["key"])
                if _revision(current) != entry["expected_revision"]:
                    view.abort()
                    return "conflict"
                fence_id = _fence_key(entry["kind"], entry["key"])
                fence = view.read("fence", fence_id)
                if isinstance(fence, dict) and fence.get("state") == "open" and fence.get("event_id") != event_id:
                    view.abort()
                    return "conflict"
            counter_key = "w05_task"
            counter = view.read("producer", counter_key) or {"revision": 0, "next_seq": 1, "epoch": 1}
            seq = int(counter.get("next_seq") or 1)
            epoch = int(counter.get("epoch") or 1)
            event["producer_seq"] = seq
            event["stream_epoch"] = epoch
            event["stream_id"] = "w05:task"
            outbox = {
                "event_id": event_id,
                "revision": 1,
                "delivery_state": "PENDING",
                "authority": "W01",
                "local_is_authority": False,
                "event": event,
                "receipt": None,
                "scope": {"repo": begun["scope"]["repo"], "task_id": begun["scope"]["task_id"]},
                "request_digest": begun["payload_hash"],
                "operation_id": event_id,
                "change_plan": plan,
            }
            if "pending_lease" in begun:
                outbox["pending_lease"] = begun["pending_lease"]
            view.stage("producer", counter_key, {
                "revision": _revision(counter) + 1,
                "next_seq": seq + 1,
                "epoch": epoch,
                "stream_id": "w05:task",
            })
            view.stage("outbox", event_id, outbox)
            for entry in plan:
                fenced = view.read(entry["kind"], entry["key"])
                view.stage("fence", _fence_key(entry["kind"], entry["key"]), {
                    "state": "open",
                    "event_id": event_id,
                    "revision": 1,
                    "object_revision": _revision(fenced),
                })
            return outbox

        return self.store.mutate(mutator)

    def _exact_receipt(self, event, response):
        if not isinstance(response, dict) or response.get("applied") is not True:
            return None
        if response.get("blocked") or response.get("fact_state") != "ACCEPTED":
            return None
        receipt = response.get("receipt")
        if not isinstance(receipt, dict):
            return None
        if (
            receipt.get("event_id") != event.get("event_id")
            or receipt.get("producer_seq") != event.get("producer_seq")
            or receipt.get("stream_id") != event.get("stream_id")
            or receipt.get("stream_epoch") != event.get("stream_epoch")
        ):
            return None
        body = dict(receipt)
        body["authority"] = "W01"
        body["w01_proof"] = False
        body["test_double"] = True
        body["fact_state"] = "ACCEPTED"
        return body

    def _receipt_from_query(self, event, queried):
        if (not isinstance(queried, dict) or queried.get("ok") is not True
                or queried.get("blocked") is not False or queried.get("classification") != "present"
                or queried.get("event_id") != event["event_id"] or queried.get("authority") != "W01"):
            return None
        items = queried.get("items")
        if not isinstance(items, list) or len(items) != 1 or _hash(items[0]) != _hash(event):
            return None
        return {
            "authority": "W01",
            "event_id": event["event_id"],
            "stream_id": event["stream_id"],
            "stream_epoch": event["stream_epoch"],
            "producer_seq": event["producer_seq"],
            "fact_state": "ACCEPTED",
            "test_double": True,
            "w01_proof": False,
        }

    def _dispatch(self, event):
        queried = self.events.query(
            stream_id=event["stream_id"],
            stream_epoch=event["stream_epoch"],
            after_seq=0,
            limit=10,
            event_id=event["event_id"],
            producer_seq=event.get("producer_seq"),
            repo_id=event.get("repo_id"),
            event_type=event.get("event_type"),
            idempotency_key=event.get("idempotency_key"),
            state=event.get("state"),
            operation=event.get("operation"),
        )
        receipt = self._receipt_from_query(event, queried)
        if receipt is not None:
            return receipt
        if not _absence_proven(event, queried):
            return None
        appended = self.events.append(event)
        return self._exact_receipt(event, appended)

    @staticmethod
    def _event_id(begun, event_type):
        return "evt_" + _hash({
            "idem": begun["idem_key"],
            "event_type": event_type,
            "payload": begun["payload_hash"],
            "object": begun["object_id"],
            "kind": begun["object_kind"],
        })[:40]

    def _commit_event(self, begun, event_type, pairs, public, state, **fields):
        event_id = self._event_id(begun, event_type)
        event = {
            "event_id": event_id,
            "schema_version": 2,
            "event_type": event_type,
            "event_class": "task",
            "repo_id": begun["scope"]["repo"],
            "producer_id": "w05",
            "stream_id": "w05:task",
            "stream_epoch": 1,
            "occurred_at": self.clock(),
            "idempotency_key": begun["idem_key"],
            "operation": begun["operation"],
            "state": state,
            "task_id": begun["scope"]["task_id"],
            "run_id": fields.get("run_id") or ("run_" + str(begun["object_id"])),
            "attempt": 1,
            "revision": pairs[0][2].get("revision", 1) if pairs and isinstance(pairs[0][2], dict) else 1,
            "stage": state,
            "app_id": "base",
            "host_id": None,
            "test_double": True,
            "w01_proof": False,
        }
        event.update({key: value for key, value in fields.items() if key != "received_at"})
        # Bind the authorized request and complete business change plan to
        # the event before dispatch. An eventual W01 adapter must sign this
        # digest in its accepted schema, not just the event coordinates.
        event["note"] = "w05-intent-v1:" + _hash({
            "principal_id": begun["principal"],
            "scope": begun["scope"],
            "operation": begun["operation"],
            "request_digest": begun["payload_hash"],
            "expected_revision": begun["observed_revision"],
            "changes": [
                {"kind": kind, "key": key, "expected_revision": expected,
                 "revision": _revision(doc), "digest": _hash(doc)}
                for kind, key, doc, expected in pairs
            ],
            "result": {key: value for key, value in public.items() if key != "lease_token"},
        })
        planned = dict(begun)
        planned["change_plan"] = [
            {"kind": item_kind, "key": item_key, "expected_revision": expected}
            for item_kind, item_key, _doc, expected in pairs
        ]
        staged = self._stage_outbox(planned, event)
        if staged == "conflict":
            return _denied(409, "CONFLICT_REVISION", state, {"reason": "cas"})
        if not isinstance(staged, dict):
            self._trace("event.pending", event_type=event_type, applied=False)
            return False
        staged_event = staged.get("event")
        if (not isinstance(staged_event, dict) or staged_event.get("event_id") != event_id
                or staged_event.get("note") != event["note"]
                or staged.get("request_digest") != begun["payload_hash"]
                or staged.get("change_plan") != planned["change_plan"]):
            return _denied(409, "CONFLICT_REVISION", state, {"reason": "outbox_intent_mismatch"})
        event_digest = _hash(staged_event)
        receipt = self._dispatch(staged_event)
        if receipt is None:
            self._trace("event.pending", event_type=event_type, applied=False)
            return False
        committed = dict(begun)
        committed["event_id"] = event_id
        committed["event_digest"] = event_digest
        committed["receipt"] = receipt
        failed = self._commit_local(committed, pairs, public, state)
        if failed is not None:
            return failed
        return True

    def _trace(self, action, **fields):
        repo = getattr(self._local, "repo", None)
        task = getattr(self._local, "task", None)
        if repo and "repo_id" not in fields:
            fields["repo_id"] = repo
        if task and "task_id" not in fields:
            fields["task_id"] = task
        fields["action"] = action
        fields["at"] = self.clock()
        fields["production"] = False
        fields["github_proof"] = False
        fields["macci_proof"] = False
        self.store.append_trace(fields)


def _absence_proven(event, queried):
    if (not isinstance(queried, dict) or queried.get("classification") != "absent"
            or queried.get("ok") is not True or queried.get("blocked") is not False
            or queried.get("event_id") != event["event_id"] or queried.get("items") != []):
        return False
    proof = queried.get("absence_proof")
    if not isinstance(proof, dict) or proof.get("accepted") is not False or proof.get("trusted") is not True:
        return False
    if proof.get("authority") != "W01" or not isinstance(proof.get("author"), str) or not proof.get("author"):
        return False
    if proof.get("event_id") != event.get("event_id") or proof.get("producer_seq") != event.get("producer_seq"):
        return False
    if proof.get("stream_id") != event.get("stream_id") or proof.get("stream_epoch") != event.get("stream_epoch"):
        return False
    return proof.get("payload_digest") == event_identity_digest(event)


def _tuple_diff(chain, head, base, merge, tree):
    for field, value in (("head", head), ("base", base), ("merge", merge), ("tree", tree)):
        if value != chain.get(field):
            return field
    return None
