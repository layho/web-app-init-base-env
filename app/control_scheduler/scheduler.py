"""Task, candidate-chain, and version scheduler.

Lease tokens, fencing, and UNKNOWN are decided by the auth port. Event order
and durability are decided by the event port. This module decides which
subject may run, which dependency or reservation is waiting, and which
evidence is allowed to move a chain or a version.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path

from .candidate import candidate_id, normalize_path, paths_overlap, require_id, require_sha
from .errors import SchedulerError
from .store import Store

TASK_PHASES = frozenset({"feedback_sync", "claim"})
CHAIN_PHASES = (
    "develop",
    "review",
    "test",
    "preview_merge",
    "deploy",
    "accept",
    "publish",
)
DEPENDENCY_OK = frozenset({"DEPLOYING", "ACCEPTING", "SATISFIED", "PUBLISHED"})
SUPERSEDE_FROM = frozenset({"OPEN", "TESTING", "INTEGRATING", "DEPLOYING"})
TERMINAL_AUTH = frozenset({"SUCCEEDED", "FAILED", "SUPERSEDED", "CANCELLED"})
HUMAN_AUTH = frozenset({"RETRY_WAIT", "BLOCKED", "FAILED"})
REMOTE_HOLD = frozenset({"PUSH_INTENT", "UNKNOWN_REMOTE", "SHA_MISMATCH", "PENDING"})
ROUTE_CLASS = "isolated-scheduler"
EXPLICIT_REJECTION = frozenset({
    "PROOF_INVALID",
    "NOT_AUTHORIZED",
    "SCHEMA_UNSUPPORTED",
    "CONFLICT_REVISION",
    "RECLAIM_DENIED",
    "CANDIDATE_STALE",
    "LEASE_FENCE_REJECTED",
    "POLICY_DENIED",
    "IDEMPOTENCY_PAYLOAD_MISMATCH",
    "CAPACITY",
    "PRODUCTION_VERIFICATION_DISABLED",
    "FREEZE_ACTIVE",
    "EVIDENCE_MISSING",
    "EVIDENCE_INCONSISTENT",
    "SCHEDULER_BYPASS_DENIED",
    "BLIND_RETRY_DENIED",
    "UNKNOWN_EXECUTOR",
})
AUTHORITY = {
    "ci.test.finished": "macci",
    "run.observed": "macci",
    "deployment.commit": "host_deployer",
    "deployment.health": "host_deployer",
    "authorization.granted": "policy_service",
    "authorization.revoked": "policy_service",
    "diagnostic.observation": "hostops_readonly",
}
_CHANNELS = frozenset({"task", "candidate", "deployment", "resource", "diagnostic", "authorization"})

_PHASE_EVIDENCE = {
    "review": "develop",
    "test": "review",
    "deploy": "deploy",
    "accept": "accept",
    "publish": "publish",
}


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load(value: str):
    return json.loads(value)


def _materialize(row):
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


class Scheduler:
    """Isolated scheduler. ``PRODUCTION_WIRED`` stays false in this package."""

    PRODUCTION_WIRED = False
    GITHUB_EXECUTION = False
    MACCI_PROOF = False

    def __init__(self, root, auth, events, *, clock=None, repo_id: str, producer_id: str = "w04-scheduler",
                 stream_id: str = "w04-scheduler:task", event_authority: str = "agent"):
        self.root = Path(root)
        self.auth = auth
        self.events = events
        self.clock = clock or (lambda: int(time.time()))
        if not isinstance(event_authority, str) or not event_authority or event_authority == "authoritative":
            raise ValueError("invalid event authority")
        self.event_authority = event_authority
        if not isinstance(repo_id, str) or not repo_id or len(repo_id) > 200 or "\n" in repo_id:
            raise ValueError("invalid repo")
        self.repo_id = repo_id
        self.producer_id = producer_id
        self.stream_id = stream_id
        self.store = Store(self.root)

    @classmethod
    def open(cls, root, auth, events, **kwargs) -> "Scheduler":
        return cls(root, auth, events, **kwargs)

    def open_version(self, version: str, *, baseline: str) -> dict:
        version = require_id(version, "version")
        baseline = require_sha(baseline, "baseline")
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM versions").fetchone():
                raise SchedulerError("VERSION_NOT_RELEASED", "a version already exists; activate only after publish or abandon")
            db.execute(
                "INSERT INTO versions(version,state,baseline,created) VALUES(?,?,?,?)",
                (version, "ACTIVE", baseline, self._now()),
            )
        return self._public({"version": version, "state": "ACTIVE"})

    def freeze_version(self, version: str, *, reason: str, evidence_ref: str) -> dict:
        self._evidence(evidence_ref)
        if not isinstance(reason, str) or not reason:
            raise SchedulerError("EVIDENCE_MISSING", "freeze reason is required", status=422)
        with self.store.transaction() as db:
            row = self._version(db, version)
            if row["state"] != "ACTIVE":
                raise SchedulerError("FREEZE_ACTIVE", "only an active version can be frozen", extra={"state": row["state"]})
            db.execute(
                "UPDATE versions SET state='FROZEN', freeze_reason=?, freeze_evidence=? WHERE version=?",
                (reason, evidence_ref, version),
            )
            self._intend_event(db, event_type="version.frozen", event_class="task", task_id="version", run_id="",
                               stage="integrate", state="FROZEN", revision=1, evidence_ref=evidence_ref, app_id="scheduler")
        self._flush_events()
        return self._public({"version": version, "state": "FROZEN"})

    def record_publish_observation(self, version: str, *, main_sha: str, preview_sha: str, observed_tree: str,
                                   candidate_tree: str) -> dict:
        report = self._ancestry(main_sha, preview_sha, observed_tree, candidate_tree)
        with self.store.transaction() as db:
            self._version(db, version)
            db.execute("UPDATE versions SET ancestry_json=? WHERE version=?", (_json(report), version))
        return self._public({"version": version, "ancestry_recorded": True, "is_ancestor": report["is_ancestor"]})

    def publish_version(self, version: str, *, observed_tree: str, candidate_tree: str, main_sha: str,
                        preview_sha: str) -> dict:
        observed_tree = require_sha(observed_tree, "tree")
        candidate_tree = require_sha(candidate_tree, "tree")
        main_sha = require_sha(main_sha, "main")
        preview_sha = require_sha(preview_sha, "preview")
        live = self._ancestry(main_sha, preview_sha, observed_tree, candidate_tree)
        with self.store.transaction() as db:
            row = self._version(db, version)
            if row["state"] not in {"ACTIVE", "FROZEN"}:
                raise SchedulerError("VERSION_NOT_RELEASED", "version cannot be published from this state",
                                     extra={"state": row["state"]})
            stored = _load(row["ancestry_json"]) if row["ancestry_json"] else None
            if stored != live:
                raise SchedulerError("EVIDENCE_INCONSISTENT", "publish ancestry is not the stored server observation",
                                     extra={"changed": False, "state": row["state"]})
            if live["observed_tree"] != observed_tree or live["candidate_tree"] != candidate_tree:
                raise SchedulerError("EVIDENCE_INCONSISTENT", "ancestry observation does not match the trees",
                                     extra={"changed": False})
            if observed_tree == candidate_tree and live["is_ancestor"] is not True:
                raise SchedulerError("FREEZE_ACTIVE", "equal trees do not unfreeze a version",
                                     extra={"changed": False, "state": row["state"]})
            if observed_tree != candidate_tree or live["is_ancestor"] is not True:
                raise SchedulerError("EVIDENCE_MISSING", "publish requires the observed tree and preview ancestry",
                                     extra={"changed": False, "state": row["state"]})
            unfinished = db.execute(
                "SELECT chain_id,state FROM chains WHERE version=? AND state!='SUPERSEDED' AND state!='PUBLISHED'",
                (version,),
            ).fetchall()
            if unfinished:
                raise SchedulerError("VERSION_NOT_RELEASED", "necessary chains are not published",
                                     extra={"changed": False, "state": row["state"], "chains": [item["chain_id"] for item in unfinished]})
            published = db.execute("SELECT * FROM chains WHERE version=? AND state='PUBLISHED'", (version,)).fetchall()
            if not published:
                raise SchedulerError("VERSION_NOT_RELEASED", "a version with no published chain cannot be published",
                                     extra={"changed": False})
            acceptance_ref = ""
            publish_ref = ""
            for chain in published:
                materialized = _materialize(chain)
                if materialized["tree"] != observed_tree:
                    raise SchedulerError(
                        "EVIDENCE_INCONSISTENT",
                        "published chain tree is not the selected version tree",
                        extra={"changed": False, "state": row["state"]},
                    )
                acceptance_ref, publish_ref = self._require_publish_binding(materialized)
            db.execute(
                """UPDATE versions SET state='PUBLISHED', acceptance_ref=?, publish_ref=?, published_tree=?,
                   main_sha=?, preview_sha=? WHERE version=?""",
                (acceptance_ref, publish_ref, observed_tree, main_sha, preview_sha, version),
            )
            self._intend_event(db, event_type="version.published", event_class="task", task_id="version", run_id="",
                               stage="publish", state="PUBLISHED", revision=1, evidence_ref=publish_ref, app_id="scheduler")
        self._flush_events()
        return self._public({"version": version, "state": "PUBLISHED", "production_enabled": False})

    def activate_version(self, version: str, *, baseline: str) -> dict:
        version = require_id(version, "version")
        baseline = require_sha(baseline, "baseline")
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM versions WHERE version=?", (version,)).fetchone():
                raise SchedulerError("CONFLICT_REVISION", "version already exists")
            rows = db.execute("SELECT state FROM versions").fetchall()
            states = {row["state"] for row in rows}
            if "FROZEN" in states:
                raise SchedulerError("FREEZE_ACTIVE", "a frozen version blocks activation", extra={"changed": False})
            if "ACTIVE" in states:
                raise SchedulerError("VERSION_NOT_RELEASED", "the active version is not published", extra={"changed": False})
            if not rows or not states <= {"PUBLISHED", "ABANDONED"}:
                raise SchedulerError("VERSION_NOT_RELEASED", "previous version is not published or abandoned",
                                     extra={"changed": False})
            db.execute(
                "INSERT INTO versions(version,state,baseline,created) VALUES(?,?,?,?)",
                (version, "ACTIVE", baseline, self._now()),
            )
        return self._public({"version": version, "state": "ACTIVE"})

    def open_task(self, *, task_id: str, version: str, app_id: str, worktree: str) -> dict:
        task_id = require_id(task_id, "task")
        app_id = require_id(app_id, "app")
        worktree = normalize_path(worktree)
        with self.store.transaction() as db:
            self._require_active(db, version)
            if db.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
                raise SchedulerError("CONFLICT_REVISION", "task already exists; close has no outgoing edge")
            db.execute(
                "INSERT INTO tasks(task_id,version,app_id,worktree,closed) VALUES(?,?,?,?,0)",
                (task_id, version, app_id, worktree),
            )
        return self._public({"task_id": task_id, "version": version, "worktree": worktree, "closed": False})

    def record_task_acceptance(self, task_id: str, *, evidence_ref: str) -> dict:
        self._evidence(evidence_ref)
        fact = self._server_fact("task_acceptance", subject=task_id, ref=evidence_ref)
        if fact.get("signer_kind") != "user_acceptance" or fact.get("outcome") == "rejected":
            raise SchedulerError("NOT_AUTHORIZED", "task acceptance requires an independent user acceptance", status=403,
                                 extra={"changed": False})
        if fact.get("subject") != task_id or fact.get("ref") != evidence_ref:
            raise SchedulerError("EVIDENCE_INCONSISTENT", "task acceptance is not bound to this task", extra={"changed": False})
        with self.store.transaction() as db:
            task = self._task(db, task_id)
            if task["closed"]:
                raise SchedulerError("CONFLICT_REVISION", "closed task cannot accept a new record")
            db.execute(
                "UPDATE tasks SET acceptance_ref=?, acceptance_signer=? WHERE task_id=?",
                (fact["ref"], fact["signer_kind"], task_id),
            )
        return self._public({"task_id": task_id, "acceptance_recorded": True, "closed": False, "signer_kind": fact["signer_kind"]})

    def close_task(self, task_id: str) -> dict:
        with self.store.transaction() as db:
            task = self._task(db, task_id)
            if task["closed"]:
                raise SchedulerError("CONFLICT_REVISION", "close has no outgoing edge", extra={"changed": False, "state": "CLOSED"})
            if task["acceptance_signer"] != "user_acceptance" or not task["acceptance_ref"]:
                raise SchedulerError("EVIDENCE_MISSING", "task acceptance is not recorded", extra={"changed": False, "closed": False})
            chains = db.execute("SELECT * FROM chains WHERE task_id=? AND state!='SUPERSEDED'", (task_id,)).fetchall()
            if not chains:
                raise SchedulerError("EVIDENCE_MISSING", "task has no necessary chain", extra={"changed": False, "closed": False})
            for chain in chains:
                if chain["state"] != "PUBLISHED":
                    raise SchedulerError("EVIDENCE_MISSING", "satisfied or partial chains cannot close the task",
                                         extra={"changed": False, "closed": False, "chain_id": chain["chain_id"], "state": chain["state"]})
                evidence = _load(chain["evidence_json"]).get("publish")
                if not evidence or evidence.get("candidate_id") != chain["candidate_id"]:
                    raise SchedulerError("EVIDENCE_INCONSISTENT", "publish evidence does not match the chain",
                                         extra={"changed": False, "closed": False})
            db.execute("UPDATE tasks SET closed=1 WHERE task_id=?", (task_id,))
            self._intend_event(db, event_type="task.closed", event_class="task", task_id=task_id, run_id="", stage="close",
                               state="CLOSED", revision=1, evidence_ref=task["acceptance_ref"], app_id=task["app_id"])
        self._flush_events()
        return self._public({"task_id": task_id, "closed": True})

    def register_chain(self, *, chain_id: str, task_id: str, pr_line: str, files, dependencies=(), priority: int = 0,
                       worktree: str, head: str = "", base: str = "", merge: str = "", tree: str = "") -> dict:
        chain_id = require_id(chain_id, "chain")
        pr_line = require_id(pr_line, "pr")
        paths = self._files(files)
        deps = self._deps(dependencies)
        priority = self._priority(priority)
        worktree = normalize_path(worktree)
        shas = self._tuple(head, base, merge, tree)
        spec = {
            "files": paths, "dependencies": deps, "priority": priority, "worktree": worktree,
            "pr_line": pr_line, "head": shas[0], "base": shas[1], "merge": shas[2], "tree": shas[3],
        }
        with self.store.transaction() as db:
            task = self._task(db, task_id)
            self._require_active(db, task["version"])
            if task["worktree"] != worktree:
                raise SchedulerError("EVIDENCE_INCONSISTENT", "worktree does not match the task binding")
            existing = db.execute("SELECT * FROM chains WHERE chain_id=?", (chain_id,)).fetchone()
            if existing:
                if _load(existing["spec_json"]) != spec:
                    raise SchedulerError("CONFLICT_REVISION", "chain specification changed; open a new chain")
                return self._public({"chain_id": chain_id, "created": False, "queue": existing["queue"], "state": existing["state"]})
            if chain_id in deps or self._reaches(db, deps, chain_id):
                raise SchedulerError("CONFLICT_REVISION", "dependency cycle")
            candidate = ""
            state = "UNBOUND"
            if shas[0]:
                candidate = self._register_remote_candidate(task_id, *shas)
                state = "OPEN"
            db.execute(
                """INSERT INTO chains(chain_id,task_id,version,pr_line,worktree,files_json,dependencies_json,priority,
                   created,state,queue,candidate_id,head,base,merge_sha,tree,spec_json)
                   VALUES(?,?,?,?,?,?,?,?,? ,?,'WAITING_DEPENDENCY',?,?,?,?,?,?)""",
                (chain_id, task_id, task["version"], pr_line, worktree, _json(paths), _json(deps), priority,
                 self._stamp(db), state, candidate, shas[0], shas[1], shas[2], shas[3], _json(spec)),
            )
            self._recompute(db, task["version"])
            stored = _materialize(db.execute("SELECT queue,state,candidate_id FROM chains WHERE chain_id=?", (chain_id,)).fetchone())
        return self._public({
            "chain_id": chain_id, "created": True, "queue": stored["queue"], "state": stored["state"],
            "candidate_id": stored["candidate_id"], "worktree": worktree,
        })

    def bind_candidate(self, chain_id: str, *, head: str, base: str, merge: str, tree: str) -> dict:
        shas = self._tuple(head, base, merge, tree)
        if not shas[0]:
            raise SchedulerError("SCHEMA_UNSUPPORTED", "candidate tuple is incomplete", status=422)
        with self.store.transaction() as db:
            chain = self._chain(db, chain_id)
            self._require_active(db, chain["version"])
            if chain["head"] and (chain["head"], chain["base"], chain["merge_sha"], chain["tree"]) != shas:
                raise SchedulerError("CANDIDATE_STALE", "bound candidate changed; replace the chain", extra={"changed": False})
            if chain["head"] == shas[0]:
                return self._public({"chain_id": chain_id, "candidate_id": chain["candidate_id"], "created": False})
            candidate = candidate_id(self.repo_id, chain["task_id"], *shas)
        issued = self.auth.register_candidate(repo=self.repo_id, task=chain["task_id"], head=shas[0], base=shas[1], merge=shas[2], tree=shas[3])
        if issued != candidate:
            raise SchedulerError("EVIDENCE_INCONSISTENT", "auth candidate id does not match the tuple", extra={"changed": False})
        with self.store.transaction() as db:
            chain = self._chain(db, chain_id)
            self._require_active(db, chain["version"])
            if chain["head"] and chain["candidate_id"] != candidate:
                raise SchedulerError("CANDIDATE_STALE", "bound candidate changed; replace the chain", extra={"changed": False})
            state = "OPEN" if chain["state"] == "UNBOUND" else chain["state"]
            db.execute(
                """UPDATE chains SET candidate_id=?, head=?, base=?, merge_sha=?, tree=?, state=? WHERE chain_id=?""",
                (candidate, shas[0], shas[1], shas[2], shas[3], state, chain_id),
            )
            self._recompute(db, chain["version"])
            self._intend_event(db, event_type="candidate.bound", event_class="candidate", task_id=chain["task_id"], run_id="",
                               stage="develop", state="OPEN", candidate_id=candidate, revision=1, app_id=self._app_of(db, chain["task_id"]),
                               head=shas[0], base=shas[1], merge=shas[2], tree=shas[3])
        self._flush_events()
        return self._public({"chain_id": chain_id, "candidate_id": candidate, "state": "OPEN"})

    def replace_candidate(self, chain_id: str, *, head: str, base: str, merge: str, tree: str, new_chain_id: str,
                          pr_line: str) -> dict:
        shas = self._tuple(head, base, merge, tree)
        if not shas[0]:
            raise SchedulerError("SCHEMA_UNSUPPORTED", "replacement tuple is incomplete", status=422)
        new_chain_id = require_id(new_chain_id, "chain")
        pr_line = require_id(pr_line, "pr")
        with self.store.transaction() as db:
            chain = self._chain(db, chain_id)
            self._require_active(db, chain["version"])
            if chain["state"] not in SUPERSEDE_FROM:
                raise SchedulerError("CONFLICT_REVISION", "this chain cannot be superseded from its current state",
                                     extra={"state": chain["state"], "changed": False})
        candidate = self._register_remote_candidate(chain["task_id"], *shas)
        with self.store.transaction() as db:
            chain = self._chain(db, chain_id)
            if chain["state"] not in SUPERSEDE_FROM:
                raise SchedulerError("CONFLICT_REVISION", "this chain cannot be superseded from its current state",
                                     extra={"state": chain["state"]})
            if db.execute("SELECT 1 FROM chains WHERE chain_id=?", (new_chain_id,)).fetchone():
                raise SchedulerError("CONFLICT_REVISION", "replacement chain already exists")
            spec = {
                "files": _load(chain["files_json"]), "dependencies": _load(chain["dependencies_json"]),
                "priority": chain["priority"], "worktree": chain["worktree"], "pr_line": pr_line,
                "head": shas[0], "base": shas[1], "merge": shas[2], "tree": shas[3],
            }
            db.execute(
                "UPDATE chains SET state='SUPERSEDED', queue='RELEASED', superseded_by=? WHERE chain_id=?",
                (new_chain_id, chain_id),
            )
            db.execute(
                """INSERT INTO chains(chain_id,task_id,version,pr_line,worktree,files_json,dependencies_json,priority,
                   created,state,queue,candidate_id,head,base,merge_sha,tree,spec_json)
                   VALUES(?,?,?,?,?,?,?,?,?,'OPEN','READY',?,?,?,?,?,?)""",
                (new_chain_id, chain["task_id"], chain["version"], pr_line, chain["worktree"], chain["files_json"],
                 chain["dependencies_json"], chain["priority"], self._stamp(db), candidate, *shas, _json(spec)),
            )
            self._recompute(db, chain["version"])
            self._intend_event(db, event_type="candidate.superseded", event_class="candidate", task_id=chain["task_id"],
                               run_id="", stage="develop", state="SUPERSEDED", candidate_id=chain["candidate_id"], revision=1,
                               app_id=self._app_of(db, chain["task_id"]), head=chain["head"], base=chain["base"],
                               merge=chain["merge_sha"], tree=chain["tree"])
        self._flush_events()
        return self._public({
            "chain_id": chain_id, "state": "SUPERSEDED", "successor": new_chain_id, "candidate_id": candidate,
            "evidence_copied": False,
        })

    def claim_next(self, *, version: str, phase: str, caller: dict, pinned_pr: str | None = None, chain_id: str | None = None,
                   fault: str | None = None, allow_human: bool = False) -> dict:
        if phase == "integrate" or phase == "close":
            raise SchedulerError("SCHEDULER_BYPASS_DENIED", "this phase is not claimed directly", extra={"pinned_pr_ignored": pinned_pr is not None})
        if phase in TASK_PHASES:
            return self._claim_task(version=version, phase=phase, caller=caller, fault=fault)
        if phase not in CHAIN_PHASES:
            raise SchedulerError("SCHEMA_UNSUPPORTED", "phase is not in the contract", status=422)
        with self.store.transaction() as db:
            self._require_active(db, version)
            if self._version_blocked(db, version):
                return self._public({"claimed": False, "reason": "version_blocked", "pinned_pr_ignored": pinned_pr is not None})
            selected = self._select_chain(db, version, phase, chain_id, allow_human=allow_human)
            if selected is None:
                reason = "human_required" if self._human_waiting(db, version, phase) else "none_ready"
                return self._public({"claimed": False, "reason": reason, "pinned_pr_ignored": pinned_pr is not None})
            round_id, body = self._insert_intent(db, selected, phase, caller, expected_revision=0)
        return self._finish_claim(round_id, body, fault, pinned_pr)

    def stage_prepare(self, *args, **kwargs) -> dict:
        raise SchedulerError("SCHEDULER_BYPASS_DENIED", "a stage cannot prepare a queued chain directly")

    def heartbeat(self, *, round_id: str, caller: dict) -> dict:
        return self._bound_call(round_id, caller, operation="run.heartbeat")

    def renew(self, *, round_id: str, caller: dict) -> dict:
        return self._bound_call(round_id, caller, operation="run.renew")

    def start(self, *, round_id: str, caller: dict) -> dict:
        with self.store.transaction() as db:
            row = self._round(db, round_id)
            if row["auth_state"] != "LEASED" or row["dispatch"] != "CONFIRMED":
                raise SchedulerError("CONFLICT_REVISION", "start requires the current leased round",
                                     extra={"changed": False, "state": row["auth_state"]})
            chain = self._chain(db, row["chain_id"]) if row["chain_id"] else None
            if chain and chain["state"] == "SUPERSEDED":
                raise SchedulerError("CANDIDATE_STALE", "superseded chain cannot start", extra={"changed": False})
        result = self._bound_call(round_id, caller, operation="run.start")
        with self.store.transaction() as db:
            row = self._round(db, round_id)
            if row["chain_id"] and row["phase"] == "test":
                chain = self._chain(db, row["chain_id"])
                if chain["state"] == "OPEN":
                    db.execute("UPDATE chains SET state='TESTING' WHERE chain_id=?", (row["chain_id"],))
            if row["chain_id"] and row["phase"] == "preview_merge":
                db.execute("UPDATE chains SET remote_status='PUSH_INTENT' WHERE chain_id=? AND remote_status='NONE'", (row["chain_id"],))
        result["remote_called"] = False
        return result

    def complete(self, *, round_id: str, caller: dict, result: str, evidence_ref: str,
                 evidence_candidate: str | None = None) -> dict:
        if result not in {"SUCCEEDED", "FAILED"}:
            raise SchedulerError("SCHEMA_UNSUPPORTED", "result is not a terminal run result", status=422)
        if result == "UNKNOWN":
            raise SchedulerError("NOT_AUTHORIZED", "a caller cannot write UNKNOWN", status=403)
        fact = None
        with self.store.transaction() as db:
            row = self._round(db, round_id)
            if row["auth_state"] not in {"LEASED", "RUNNING"} or row["dispatch"] != "CONFIRMED":
                raise SchedulerError("CONFLICT_REVISION", "complete requires the current round",
                                     extra={"changed": False, "state": row["auth_state"]})
            chain = self._chain(db, row["chain_id"]) if row["chain_id"] else None
            if chain:
                bound = evidence_candidate or chain["candidate_id"]
                if bound != chain["candidate_id"] or row["candidate_id"] != chain["candidate_id"]:
                    raise SchedulerError("CANDIDATE_STALE", "evidence candidate does not match the chain",
                                         extra={"changed": False})
                if chain["state"] == "SUPERSEDED":
                    raise SchedulerError("CANDIDATE_STALE", "evidence cannot move a superseded chain", extra={"changed": False})
                self._precheck_complete(chain, row["phase"], result, evidence_ref)
                if result == "SUCCEEDED" and row["phase"] in {"test", "accept", "publish"}:
                    fact = self._phase_fact(chain, row["phase"], evidence_ref)
                elif result == "FAILED" and row["phase"] == "accept":
                    fact = self._reject_acceptance(chain, evidence_ref)
        response = self._bound_call(round_id, caller, operation="run.complete", extra_overlay={"result": result})
        with self.store.transaction() as db:
            row = self._round(db, round_id)
            if row["chain_id"]:
                self._apply_complete(db, row, result, evidence_ref, fact)
        response["evidence_ref"] = evidence_ref
        return response

    def report_caller_timeout(self, *, round_id: str) -> dict:
        with self.store.transaction() as db:
            row = self._round(db, round_id)
            state = row["auth_state"]
        return self._public({
            "round_id": round_id, "state": state, "unknown_written": False, "code": "TIMEOUT_NOT_UNKNOWN",
            "changed": False, "auth_called": False,
        })

    def reconcile_timeout(self, *, run_id: str, fenced: bool, effect_known: bool) -> dict:
        try:
            outcome = self.auth.trusted_fence(run_id, fenced=fenced, effect_known=effect_known)
        except Exception as exc:
            self._reraise(exc)
        state = outcome.get("state")
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM rounds WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise SchedulerError("EVIDENCE_MISSING", "run is not a scheduler round")
            if state == "UNKNOWN" and outcome.get("changed") is True:
                db.execute("UPDATE rounds SET auth_state='UNKNOWN', generation=NULL WHERE round_id=?", (row["round_id"],))
                current = "UNKNOWN"
            else:
                current = row["auth_state"]
        return self._public({
            "run_id": run_id, "state": current, "unknown_written": current == "UNKNOWN",
            "event_unknown_appended": False, "github_fencing": False, "changed": current == "UNKNOWN",
        })

    def classify_unknown(self, *, run_id: str, fenced: bool, classification: str, evidence_ref: str) -> dict:
        if classification not in {"succeeded", "failed", "not_occurred"}:
            raise SchedulerError("SCHEMA_UNSUPPORTED", "classification is not a reconciliation result", status=422)
        self._evidence(evidence_ref)
        try:
            outcome = self.auth.trusted_classify(run_id, fenced=fenced, classification=classification)
        except Exception as exc:
            self._reraise(exc)
        mapped = {"succeeded": "SUCCEEDED", "failed": "FAILED", "not_occurred": "RETRY_WAIT"}[classification]
        if outcome.get("state") != mapped:
            raise SchedulerError("EVIDENCE_INCONSISTENT", "auth classification was not applied", extra={"changed": False})
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM rounds WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["auth_state"] != "UNKNOWN":
                raise SchedulerError("EVIDENCE_MISSING", "classification needs an unknown scheduler round", extra={"changed": False})
            db.execute(
                "UPDATE rounds SET auth_state=?, evidence_ref=? WHERE round_id=?",
                (mapped, evidence_ref, row["round_id"]),
            )
            if row["chain_id"] and classification == "succeeded":
                self._apply_complete(db, row, "SUCCEEDED", evidence_ref, None)
            elif row["chain_id"] and classification == "failed":
                self._apply_complete(db, row, "FAILED", evidence_ref, None)
            elif row["chain_id"] and classification == "not_occurred":
                db.execute("UPDATE chains SET queue='HUMAN_HOLD' WHERE chain_id=? AND queue='HELD'", (row["chain_id"],))
        return self._public({"run_id": run_id, "state": mapped, "resumed": False, "evidence_ref": evidence_ref})

    def advance_serial(self, version: str, *, pinned_pr: str | None = None) -> dict:
        with self.store.transaction() as db:
            self._require_active(db, version)
            if self._version_blocked(db, version):
                return self._public({"advanced": False, "reason": "version_blocked", "pinned_pr_ignored": pinned_pr is not None})
            if self._serial_held(db, version):
                return self._public({"advanced": False, "reason": "serial_held", "pinned_pr_ignored": pinned_pr is not None})
            rows = db.execute(
                """SELECT * FROM chains WHERE version=? AND state='TESTING' AND queue='READY' ORDER BY priority DESC, created, chain_id""",
                (version,),
            ).fetchall()
            chosen = None
            for row in rows:
                if "test" in _load(row["evidence_json"]):
                    chosen = _materialize(row)
                    break
            if chosen is None:
                return self._public({"advanced": False, "reason": "none_ready", "pinned_pr_ignored": pinned_pr is not None})
            db.execute("UPDATE chains SET state='INTEGRATING', queue='SERIAL' WHERE chain_id=?", (chosen["chain_id"],))
            self._intend_event(db, event_type="chain.integrating", event_class="candidate", task_id=chosen["task_id"], run_id="",
                               stage="integrate", state="INTEGRATING", candidate_id=chosen["candidate_id"], revision=1,
                               app_id=self._app_of(db, chosen["task_id"]), head=chosen["head"], base=chosen["base"],
                               merge=chosen["merge_sha"], tree=chosen["tree"])
        self._flush_events()
        return self._public({
            "advanced": True, "chain_id": chosen["chain_id"], "pr_line": chosen["pr_line"], "state": "INTEGRATING",
            "pinned_pr_ignored": pinned_pr is not None, "remote_called": False,
        })

    def observe_remote(self, chain_id: str, *, kind: str, observation_seq: int, remote_sha: str = "", merged: bool | None = None,
                       closed: bool | None = None, evidence_ref: str = "") -> dict:
        if type(observation_seq) is not int or isinstance(observation_seq, bool) or observation_seq < 1:
            raise SchedulerError("SCHEMA_UNSUPPORTED", "observation sequence is invalid", status=422)
        if kind not in {"exact", "sha_mismatch", "lost_response", "ambiguous", "closed_unmerged", "merged_different"}:
            raise SchedulerError("SCHEMA_UNSUPPORTED", "remote observation kind is invalid", status=422)
        payload = {
            "kind": kind, "remote_sha": remote_sha, "merged": merged, "closed": closed, "evidence_ref": evidence_ref,
        }
        with self.store.transaction() as db:
            chain = self._chain(db, chain_id)
            if db.execute("SELECT 1 FROM observations WHERE chain_id=? AND seq=?", (chain_id, observation_seq)).fetchone():
                return self._public({"applied": False, "reason": "duplicate", "state": chain["state"], "remote_status": chain["remote_status"]})
            expected = chain["remote_seq"] + 1
            if observation_seq > expected:
                db.execute(
                    "INSERT INTO observations(chain_id,seq,kind,applied,reason,payload_json) VALUES(?,?,?,0,'withheld',?)",
                    (chain_id, observation_seq, kind, _json(payload)),
                )
                return self._public({"applied": False, "reason": "out_of_order", "state": chain["state"], "remote_status": chain["remote_status"]})
            if observation_seq < expected:
                return self._public({"applied": False, "reason": "duplicate", "state": chain["state"], "remote_status": chain["remote_status"]})
            outcome = self._apply_observation(db, chain, payload)
            db.execute(
                "INSERT INTO observations(chain_id,seq,kind,applied,reason,payload_json) VALUES(?,?,?,?,?,?)",
                (chain_id, observation_seq, kind, 1 if outcome["applied"] else 0, outcome["reason"], _json(payload)),
            )
            db.execute("UPDATE chains SET remote_seq=? WHERE chain_id=?", (observation_seq, chain_id))
            self._drain_observations(db, chain_id)
            stored = self._chain(db, chain_id)
        return self._public({
            "applied": outcome["applied"], "reason": outcome["reason"], "state": stored["state"],
            "remote_status": stored["remote_status"], "blind_retry": False,
        })

    def retry_remote(self, chain_id: str) -> dict:
        with self.store.transaction() as db:
            chain = self._chain(db, chain_id)
            if chain["remote_status"] in REMOTE_HOLD or chain["remote_status"] == "NONE":
                raise SchedulerError("BLIND_RETRY_DENIED", "a lost or pending remote result cannot be retried",
                                     extra={"changed": False, "state": chain["state"], "remote_status": chain["remote_status"]})
        raise SchedulerError("BLIND_RETRY_DENIED", "remote retry is not a scheduler action", extra={"changed": False})

    def intervene(self, *, action: str, subject_id: str, evidence_ref: str, successor_id: str = "", caller: dict | None = None,
                  note: str = "") -> dict:
        if action == "hostops_execute":
            raise SchedulerError("NOT_AN_EXECUTOR", "HostOps diagnostics are not an executor", status=403, extra={"changed": False})
        self._evidence(evidence_ref)
        if action == "abandon_version":
            return self._abandon(subject_id, evidence_ref)
        if action == "supersede_failed":
            return self._supersede_failed(subject_id, evidence_ref)
        if action == "authorize_repair":
            return self._authorize_repair(subject_id, successor_id, evidence_ref)
        if action == "retarget_dependency":
            return self._retarget(subject_id, successor_id, evidence_ref)
        if action in {"retry_phase", "unblock"}:
            if caller is None:
                raise SchedulerError("SCHEMA_UNSUPPORTED", "retry requires the holder caller", status=422)
            return self._retry_phase(subject_id, evidence_ref, caller, action)
        raise SchedulerError("SCHEMA_UNSUPPORTED", "intervention is not recognized", status=422)

    def ingest(self, *, stream_id: str, stream_epoch: int = 1, request: dict | None = None) -> dict:
        del request
        if type(stream_epoch) is not int or isinstance(stream_epoch, bool) or stream_epoch < 1:
            raise SchedulerError("SCHEMA_UNSUPPORTED", "stream epoch is invalid", status=422)
        applied = []
        skipped = []
        gap = False
        reason = None
        stopped_at = None
        watermark = 0
        while True:
            with self.store.transaction() as db:
                row = db.execute(
                    "SELECT seq FROM watermarks WHERE stream_id=? AND stream_epoch=?",
                    (stream_id, stream_epoch),
                ).fetchone()
                cursor = 0 if row is None else int(row["seq"])
            page = self._query_events(stream_id, stream_epoch, cursor, limit=100)
            if page.get("side_effect") not in (False, None):
                raise SchedulerError("SIDE_EFFECT_UNKNOWN", "event read had a side effect", extra={"changed": False})
            items = sorted(page.get("items") or [], key=lambda item: int(item["producer_seq"]))
            if not items:
                gap = bool(page.get("gap")) and cursor == watermark
                watermark = cursor
                break
            progressed = False
            stop = False
            for item in items:
                seq = int(item["producer_seq"])
                if seq <= cursor:
                    continue
                if seq != cursor + 1:
                    gap = True
                    stop = True
                    break
                identity = self._event_identity(item, stream_id, stream_epoch)
                if identity is None:
                    reason = "rejected"
                    stopped_at = seq
                    stop = True
                    break
                with self.store.transaction() as db:
                    if self._event_seen(db, identity):
                        cursor = seq
                        self._save_watermark(db, stream_id, stream_epoch, cursor)
                        skipped.append(seq)
                        progressed = True
                        continue
                    if not self._authoritative(item):
                        self._remember_event(db, identity)
                        cursor = seq
                        self._save_watermark(db, stream_id, stream_epoch, cursor)
                        skipped.append(seq)
                        progressed = True
                        continue
                    if not self._apply_external(db, item):
                        reason = "illegal"
                        stopped_at = seq
                        stop = True
                        break
                    self._remember_event(db, identity)
                    cursor = seq
                    self._save_watermark(db, stream_id, stream_epoch, cursor)
                applied.append(seq)
                progressed = True
            watermark = cursor
            if stop or gap or reason:
                break
            truncated = page.get("truncated")
            if truncated is None:
                truncated = len(items) >= 100
            if not truncated or not progressed:
                break
        body = {
            "applied": applied, "skipped_duplicates": skipped, "gap": gap, "watermark": watermark,
            "side_effect": False,
        }
        if reason:
            body["reason"] = reason
            body["stopped_at"] = stopped_at
        return self._public(body)

    def recover(self) -> dict:
        flushed = self._flush_events()
        with self.store.transaction() as db:
            pending = db.execute(
                "SELECT * FROM rounds WHERE dispatch IN ('INTENT','DISPATCH_UNKNOWN') ORDER BY created, round_id"
            ).fetchall()
            saved = [_materialize(row) for row in pending]
        restored = []
        unknown = []
        for row in saved:
            intent = _load(row["intent_json"])
            self._assert_intent_secret_free(intent)
            try:
                result = self._dispatch(intent["operation"], intent, lease_token="")
            except SchedulerError as exc:
                self._hold_unknown(row["round_id"])
                unknown.append({"round_id": row["round_id"], "code": exc.code})
                continue
            except Exception:
                self._hold_unknown(row["round_id"])
                unknown.append({"round_id": row["round_id"], "code": "SIDE_EFFECT_UNKNOWN"})
                continue
            try:
                view = self._recovery_view(result.body)
            except SchedulerError as exc:
                self._hold_unknown(row["round_id"])
                unknown.append({"round_id": row["round_id"], "code": exc.code})
                continue
            if view is None or view["auth_state"] in (None, "", "UNKNOWN"):
                self._hold_unknown(row["round_id"])
                unknown.append({"round_id": row["round_id"], "code": "DISPATCH_UNKNOWN"})
                continue
            with self.store.transaction() as db:
                self._confirm(db, row["round_id"], view)
                self._intend_event(
                    db, event_type="run.claimed", event_class="task", task_id=intent["task_id"], run_id=view["run_id"],
                    stage=intent.get("phase") or "", state=view["auth_state"], attempt=view["attempt"], revision=view["revision"],
                    candidate_id=intent.get("candidate_id") or "", idempotency_key=intent["idempotency_key"],
                    app_id=intent.get("app_id") or "scheduler", head=intent.get("head") or "", base=intent.get("base") or "",
                    merge=intent.get("merge") or "", tree=intent.get("tree") or "",
                )
            self._flush_events()
            restored.append({
                "round_id": row["round_id"], "run_id": view["run_id"], "lease_token": view["lease_token"],
            })
        inconsistent = self.audit()
        return self._public({
            "restored": restored, "dispatch_unknown": unknown, "inconsistent": inconsistent,
            "tokens_stored": False, "events": flushed,
        })

    def audit(self) -> list:
        found = []
        with self.store.transaction() as db:
            for chain in db.execute("SELECT * FROM chains WHERE state!='SUPERSEDED'"):
                evidence = _load(chain["evidence_json"])
                for phase, record in evidence.items():
                    if not isinstance(record, dict):
                        continue
                    if record.get("candidate_id") != chain["candidate_id"]:
                        found.append({"chain_id": chain["chain_id"], "phase": phase, "reason": "candidate"})
                    tuple_now = (chain["head"], chain["base"], chain["merge_sha"], chain["tree"])
                    recorded = (record.get("head"), record.get("base"), record.get("merge"), record.get("tree"))
                    if record.get("candidate_id") and recorded != tuple_now:
                        found.append({"chain_id": chain["chain_id"], "phase": phase, "reason": "tuple"})
        return found

    def snapshot(self, version: str) -> dict:
        with self.store.transaction() as db:
            version_row = self._version(db, version)
            tasks = [self._task_view(row) for row in db.execute("SELECT * FROM tasks WHERE version=? ORDER BY task_id", (version,))]
            chains = [self._chain_view(row) for row in db.execute("SELECT * FROM chains WHERE version=? ORDER BY priority DESC, created, chain_id", (version,))]
            rounds = [self._round_view(row) for row in db.execute("SELECT * FROM rounds WHERE version=? ORDER BY created, round_id", (version,))]
        return self._public({"version": self._version_view(version_row), "tasks": tasks, "chains": chains, "rounds": rounds})

    def _claim_task(self, *, version: str, phase: str, caller: dict, fault: str | None) -> dict:
        with self.store.transaction() as db:
            self._require_active(db, version)
            if self._version_blocked(db, version):
                return self._public({"claimed": False, "reason": "version_blocked"})
            task_id = str(caller.get("task_id") or "")
            task = self._task(db, task_id)
            if task["version"] != version:
                raise SchedulerError("CONFLICT_REVISION", "task is not in this version")
            if self._active_subject(db, task_id=task_id, phase=phase, candidate_id=""):
                return self._public({"claimed": False, "reason": "duplicate_claim", "state": "active"})
            self._reject_caller_secrets(caller)
            body = self._claim_intent(
                task_id=task_id, app_id=task["app_id"], phase=phase, candidate_id="",
                head="", base="", merge="", tree="", object_id=task_id,
            )
            round_id = self._new_round(db, version, None, task_id, phase, "", body)
        return self._finish_claim(round_id, body, fault, None)

    def _finish_claim(self, round_id: str, body: dict, fault: str | None, pinned_pr: str | None) -> dict:
        try:
            result = self._dispatch(body["operation"], body, lease_token="")
        except SchedulerError as exc:
            self._settle_claim_failure(round_id, exc)
            raise SchedulerError(exc.code, exc.message, status=exc.status, extra={**exc.extra, "changed": False}) from exc
        except Exception as exc:
            self._hold_unknown(round_id)
            raise SchedulerError("SIDE_EFFECT_UNKNOWN", "remote claim result is unknown", status=503, extra={"changed": False}) from exc
        if fault == "after-auth-before-confirm":
            raise SchedulerError("CRASH_INJECTED", "isolated fault before confirm", status=500, extra={"changed": False})
        view = self._auth_view(result.body, replay_required=False)
        if view["auth_state"] in (None, "", "UNKNOWN"):
            self._hold_unknown(round_id)
            raise SchedulerError("SIDE_EFFECT_UNKNOWN", "claim result did not name a usable state", extra={"changed": False})
        with self.store.transaction() as db:
            self._confirm(db, round_id, view)
            row = self._round(db, round_id)
            task = self._task(db, row["task_id"])
            self._intend_event(
                db, event_type="run.claimed", event_class="task", task_id=row["task_id"], run_id=view["run_id"],
                stage=row["phase"], state=view["auth_state"], attempt=view["attempt"], revision=view["revision"],
                candidate_id=row["candidate_id"], idempotency_key=row["idempotency_key"], app_id=task["app_id"],
                head=body.get("head") or "", base=body.get("base") or "", merge=body.get("merge") or "", tree=body.get("tree") or "",
            )
        self._flush_events()
        return self._public({
            "claimed": True, "round_id": round_id, "run_id": view["run_id"], "attempt": view["attempt"],
            "lease_generation": view["generation"], "revision": view["revision"], "state": view["auth_state"],
            "lease_token": view["lease_token"], "chain_id": row["chain_id"], "phase": row["phase"],
            "pinned_pr_ignored": pinned_pr is not None, "candidate_id": row["candidate_id"],
        })

    def _bound_call(self, round_id: str, caller: dict, *, operation: str, extra_overlay: dict | None = None) -> dict:
        with self.store.transaction() as db:
            row = self._round(db, round_id)
            if row["dispatch"] != "CONFIRMED" or not row["run_id"]:
                raise SchedulerError("CONFLICT_REVISION", "round is not confirmed", extra={"changed": False, "state": row["auth_state"]})
            self._reject_caller_secrets(caller)
            if "lease_token" not in caller or caller.get("lease_token") in (None, ""):
                raise SchedulerError("LEASE_FENCE_REJECTED", "current token is required", extra={"changed": False, "state": row["auth_state"]})
            token = str(caller["lease_token"])
            task = self._task(db, row["task_id"])
            intent = self._bound_intent(row, task["app_id"], operation)
            if row["chain_id"]:
                chain = self._chain(db, row["chain_id"])
                intent.update({"head": chain["head"], "base": chain["base"], "merge": chain["merge_sha"], "tree": chain["tree"]})
            if "expected_revision" in caller:
                intent["expected_revision"] = caller["expected_revision"]
            if "lease_generation" in caller:
                intent["lease_generation"] = caller["lease_generation"]
            if extra_overlay:
                for key, value in extra_overlay.items():
                    if key in {"proof", "actor", "lease_token", "signer_kind"}:
                        continue
                    intent[key] = value
            before = row["auth_state"]
        try:
            result = self._dispatch(operation, intent, lease_token=token)
        except SchedulerError as exc:
            raise SchedulerError(exc.code, exc.message, status=exc.status, extra={**exc.extra, "changed": False, "state": before}) from exc
        view = self._auth_view(result.body, replay_required=False)
        with self.store.transaction() as db:
            db.execute(
                "UPDATE rounds SET auth_state=?, generation=?, revision=?, attempt=? WHERE round_id=?",
                (view["auth_state"], view["generation"], view["revision"], view["attempt"], round_id),
            )
        return self._public({
            "round_id": round_id, "run_id": view["run_id"], "state": view["auth_state"],
            "lease_generation": view["generation"], "revision": view["revision"], "attempt": view["attempt"],
            "lease_token": view["lease_token"], "changed": True,
        })

    def _apply_complete(self, db, row, result: str, evidence_ref: str, fact) -> None:
        chain = self._chain(db, row["chain_id"])
        phase = row["phase"]
        evidence = _load(chain["evidence_json"])
        if result == "SUCCEEDED":
            signer = ""
            ref = evidence_ref
            if isinstance(fact, dict):
                signer = str(fact.get("signer_kind") or "")
                ref = str(fact.get("ref") or evidence_ref)
            evidence[phase] = {
                "ref": ref, "candidate_id": chain["candidate_id"], "head": chain["head"],
                "base": chain["base"], "merge": chain["merge_sha"], "tree": chain["tree"], "signer_kind": signer,
            }
        queue = "READY"
        state = chain["state"]
        if phase == "test" and result == "FAILED":
            state = "FAILED"
            queue = "CLOSED_HOLD"
        elif phase == "test" and result == "SUCCEEDED":
            state = "TESTING" if state == "OPEN" else state
            queue = "READY"
        elif phase == "deploy" and result == "SUCCEEDED" and state == "DEPLOYING":
            state = "ACCEPTING"
        elif phase == "deploy" and result == "FAILED":
            queue = "HUMAN_HOLD"
        elif phase == "accept" and result == "SUCCEEDED":
            state = "SATISFIED"
        elif phase == "accept" and result == "FAILED":
            state = "REJECTED"
            queue = "CLOSED_HOLD"
        elif phase == "publish" and result == "SUCCEEDED":
            state = "PUBLISHED"
            queue = "RELEASED"
        elif result == "FAILED":
            queue = "HUMAN_HOLD"
        db.execute(
            "UPDATE chains SET state=?, queue=?, evidence_json=?, revision=revision+1 WHERE chain_id=?",
            (state, queue, _json(evidence), chain["chain_id"]),
        )
        if queue == "RELEASED":
            self._recompute(db, chain["version"])

    def _precheck_complete(self, chain, phase: str, result: str, evidence_ref: str) -> None:
        if result == "SUCCEEDED":
            self._evidence(evidence_ref)
        if phase == "publish" and result == "SUCCEEDED":
            self._evidence(evidence_ref)
            if chain["state"] != "SATISFIED":
                raise SchedulerError("CONFLICT_REVISION", "publish requires a satisfied chain", extra={"changed": False, "state": chain["state"]})
        if phase == "deploy" and chain["state"] != "DEPLOYING":
            raise SchedulerError("CONFLICT_REVISION", "deploy requires a deployed-ready chain", extra={"changed": False, "state": chain["state"]})
        if phase == "preview_merge" and chain["state"] != "INTEGRATING":
            raise SchedulerError("CONFLICT_REVISION", "preview merge requires the integrating chain", extra={"changed": False})

    def _apply_observation(self, db, chain, payload: dict) -> dict:
        kind = payload["kind"]
        if self._chain_unknown(db, chain["chain_id"]) and kind == "exact":
            return {"applied": False, "reason": "version_unknown"}
        if chain["remote_status"] == "UNKNOWN_REMOTE" and kind == "exact":
            return {"applied": False, "reason": "stale_after_unknown"}
        if kind == "exact":
            if chain["state"] != "INTEGRATING" or chain["remote_status"] not in {"NONE", "PUSH_INTENT"}:
                return {"applied": False, "reason": "not_ready"}
            if payload["remote_sha"] != chain["head"] or not payload["evidence_ref"]:
                db.execute("UPDATE chains SET remote_status='SHA_MISMATCH' WHERE chain_id=?", (chain["chain_id"],))
                return {"applied": True, "reason": "sha_mismatch"}
            evidence = _load(chain["evidence_json"])
            evidence["remote"] = {
                "ref": payload["evidence_ref"], "candidate_id": chain["candidate_id"], "head": chain["head"],
                "base": chain["base"], "merge": chain["merge_sha"], "tree": chain["tree"],
            }
            db.execute(
                "UPDATE chains SET state='DEPLOYING', queue='READY', remote_status='EXACT', evidence_json=? WHERE chain_id=?",
                (_json(evidence), chain["chain_id"]),
            )
            self._recompute(db, chain["version"])
            return {"applied": True, "reason": "exact"}
        if kind == "sha_mismatch":
            db.execute("UPDATE chains SET remote_status='SHA_MISMATCH' WHERE chain_id=?", (chain["chain_id"],))
            return {"applied": True, "reason": "sha_mismatch"}
        if kind in {"lost_response", "ambiguous"}:
            db.execute("UPDATE chains SET remote_status='UNKNOWN_REMOTE' WHERE chain_id=?", (chain["chain_id"],))
            return {"applied": True, "reason": "remote_unknown"}
        if kind == "merged_different":
            db.execute("UPDATE chains SET remote_status='PENDING' WHERE chain_id=?", (chain["chain_id"],))
            return {"applied": True, "reason": "pending"}
        if kind == "closed_unmerged":
            if payload["merged"] is False and payload["closed"] is True and not payload["remote_sha"] and chain["remote_status"] != "UNKNOWN_REMOTE":
                db.execute(
                    "UPDATE chains SET state='SUPERSEDED', queue='RELEASED', remote_status='RELEASED' WHERE chain_id=?",
                    (chain["chain_id"],),
                )
                self._recompute(db, chain["version"])
                return {"applied": True, "reason": "released"}
            db.execute("UPDATE chains SET remote_status='PENDING' WHERE chain_id=?", (chain["chain_id"],))
            return {"applied": True, "reason": "pending"}
        return {"applied": False, "reason": "ignored"}

    def _drain_observations(self, db, chain_id: str) -> None:
        while True:
            chain = self._chain(db, chain_id)
            nxt = chain["remote_seq"] + 1
            row = db.execute("SELECT * FROM observations WHERE chain_id=? AND seq=? AND reason='withheld'", (chain_id, nxt)).fetchone()
            if row is None:
                return
            outcome = self._apply_observation(db, chain, _load(row["payload_json"]))
            db.execute(
                "UPDATE observations SET applied=?, reason=? WHERE chain_id=? AND seq=?",
                (1 if outcome["applied"] else 0, outcome["reason"], chain_id, nxt),
            )
            db.execute("UPDATE chains SET remote_seq=? WHERE chain_id=?", (nxt, chain_id))

    def _apply_external(self, db, event: dict) -> bool:
        if event.get("event_type") != "run.observed":
            return True
        candidate = str(event.get("candidate_id") or "")
        chain = db.execute("SELECT * FROM chains WHERE candidate_id=?", (candidate,)).fetchone()
        if chain is None:
            return True
        state = str(event.get("state") or "")
        stage = str(event.get("stage") or "")
        if stage == "test" and state == "FAILED" and chain["state"] == "TESTING":
            db.execute("UPDATE chains SET state='FAILED', queue='CLOSED_HOLD' WHERE chain_id=?", (chain["chain_id"],))
            return True
        if state == "PUBLISHED" and chain["state"] != "SATISFIED":
            return False
        return False

    def _retry_phase(self, round_id: str, evidence_ref: str, caller: dict, action: str) -> dict:
        with self.store.transaction() as db:
            row = self._round(db, round_id)
            if action == "unblock" and row["auth_state"] not in {"BLOCKED", "FAILED", "RETRY_WAIT"}:
                chain = self._chain(db, row["chain_id"]) if row["chain_id"] else None
                if not chain or chain["queue"] != "HUMAN_HOLD":
                    raise SchedulerError("CONFLICT_REVISION", "unblock requires a held failure", extra={"changed": False, "state": row["auth_state"]})
            if row["auth_state"] not in HUMAN_AUTH and action == "retry_phase":
                raise SchedulerError("CONFLICT_REVISION", "retry requires a finished non-running round",
                                     extra={"changed": False, "state": row["auth_state"]})
            chain = self._chain(db, row["chain_id"]) if row["chain_id"] else None
            if chain and chain["state"] in {"FAILED", "REJECTED", "SUPERSEDED"}:
                raise SchedulerError("CONFLICT_REVISION", "a failed chain needs a new chain", extra={"changed": False, "state": chain["state"]})
            version_id = row["version"]
            version = self._version(db, version_id)
            held = bool(chain and chain["queue"] == "HUMAN_HOLD")
            version_state = version["state"]
            phase = row["phase"]
            chain_id = row["chain_id"]
            task_id = row["task_id"]
            previous_run_id = row["run_id"]
            candidate_id = chain["candidate_id"] if chain else ""
            tree = chain["tree"] if chain else ""
            head = chain["head"] if chain else ""
            base = chain["base"] if chain else ""
            merge = chain["merge_sha"] if chain else ""
        self._authorize_manual(
            action, round_id, evidence_ref, version_state=version_state, candidate_id=candidate_id, tree=tree,
            task_id=task_id, head=head, base=base, merge=merge,
        )
        with self.store.transaction() as db:
            if held and chain_id:
                db.execute("UPDATE chains SET queue='READY' WHERE chain_id=? AND queue='HUMAN_HOLD'", (chain_id,))
            self._intend_event(
                db, event_type="intervention.recorded", event_class="authorization" if all((head, base, merge, tree)) else "task",
                task_id=task_id, run_id=previous_run_id or "", stage=action, state="AUTHORIZED", revision=1,
                evidence_ref=evidence_ref, app_id=self._app_of(db, task_id), candidate_id=candidate_id,
                head=head, base=base, merge=merge, tree=tree, operation=action,
            )
        self._flush_events()
        claimed = self.claim_next(
            version=version_id, phase=phase, caller={**caller, "task_id": task_id}, chain_id=chain_id, allow_human=True,
        )
        claimed["previous_run_id"] = previous_run_id
        claimed["evidence_ref"] = evidence_ref
        claimed["resumed_same_run"] = False
        return claimed

    def _supersede_failed(self, chain_id: str, evidence_ref: str) -> dict:
        with self.store.transaction() as db:
            chain = self._chain(db, chain_id)
            if chain["state"] != "FAILED":
                raise SchedulerError("CONFLICT_REVISION", "only a failed chain can be superseded this way",
                                     extra={"state": chain["state"], "changed": False})
            version = self._version(db, chain["version"])
        self._authorize_manual(
            "supersede_failed", chain_id, evidence_ref, version_state=version["state"], candidate_id=chain["candidate_id"],
            tree=chain["tree"], task_id=chain["task_id"], head=chain["head"], base=chain["base"], merge=chain["merge_sha"],
        )
        with self.store.transaction() as db:
            chain = self._chain(db, chain_id)
            if chain["state"] != "FAILED":
                raise SchedulerError("CONFLICT_REVISION", "only a failed chain can be superseded this way",
                                     extra={"state": chain["state"], "changed": False})
            db.execute("UPDATE chains SET state='SUPERSEDED', queue='RELEASED' WHERE chain_id=?", (chain_id,))
            self._recompute(db, chain["version"])
            self._intend_event(
                db, event_type="intervention.recorded", event_class="authorization", task_id=chain["task_id"], run_id="",
                stage="supersede_failed", state="AUTHORIZED", revision=1, evidence_ref=evidence_ref,
                app_id=self._app_of(db, chain["task_id"]), candidate_id=chain["candidate_id"], head=chain["head"],
                base=chain["base"], merge=chain["merge_sha"], tree=chain["tree"], operation="supersede_failed",
            )
        self._flush_events()
        return self._public({"chain_id": chain_id, "state": "SUPERSEDED", "evidence_ref": evidence_ref, "evidence_copied": False})

    def _authorize_repair(self, chain_id: str, successor_id: str, evidence_ref: str) -> dict:
        if not successor_id:
            raise SchedulerError("EVIDENCE_MISSING", "repair requires a new chain", extra={"changed": False})
        with self.store.transaction() as db:
            chain = self._chain(db, chain_id)
            successor = self._chain(db, successor_id)
            version = self._version(db, chain["version"])
            if chain["state"] != "REJECTED":
                raise SchedulerError("CONFLICT_REVISION", "repair starts from a rejected chain", extra={"state": chain["state"]})
            if successor["task_id"] != chain["task_id"] or not successor["candidate_id"]:
                raise SchedulerError("CANDIDATE_STALE", "repair chain is not a new candidate", extra={"changed": False})
            if _load(successor["evidence_json"]):
                raise SchedulerError("EVIDENCE_INCONSISTENT", "old evidence cannot move to the repair chain", extra={"changed": False})
        self._authorize_manual(
            "authorize_repair", chain_id, evidence_ref, version_state=version["state"], candidate_id=successor["candidate_id"],
            tree=successor["tree"], task_id=chain["task_id"], head=successor["head"], base=successor["base"],
            merge=successor["merge_sha"],
        )
        with self.store.transaction() as db:
            chain = self._chain(db, chain_id)
            successor = self._chain(db, successor_id)
            if chain["state"] != "REJECTED":
                raise SchedulerError("CONFLICT_REVISION", "repair starts from a rejected chain", extra={"state": chain["state"], "changed": False})
            if _load(successor["evidence_json"]):
                raise SchedulerError("EVIDENCE_INCONSISTENT", "old evidence cannot move to the repair chain", extra={"changed": False})
            db.execute("UPDATE chains SET state='SUPERSEDED', queue='RELEASED', superseded_by=? WHERE chain_id=?", (successor_id, chain_id))
            self._recompute(db, chain["version"])
            self._intend_event(
                db, event_type="intervention.recorded", event_class="authorization", task_id=chain["task_id"], run_id="",
                stage="authorize_repair", state="AUTHORIZED", revision=1, evidence_ref=evidence_ref,
                app_id=self._app_of(db, chain["task_id"]), candidate_id=successor["candidate_id"], head=successor["head"],
                base=successor["base"], merge=successor["merge_sha"], tree=successor["tree"], operation="authorize_repair",
            )
        self._flush_events()
        return self._public({"chain_id": chain_id, "state": "SUPERSEDED", "successor": successor_id, "evidence_copied": False, "evidence_ref": evidence_ref})

    def _retarget(self, chain_id: str, successor_id: str, evidence_ref: str) -> dict:
        with self.store.transaction() as db:
            chain = self._chain(db, chain_id)
            successor = self._chain(db, successor_id)
            version = self._version(db, chain["version"])
        self._authorize_manual(
            "retarget_dependency", chain_id, evidence_ref, version_state=version["state"], candidate_id=chain["candidate_id"],
            tree=chain["tree"], task_id=chain["task_id"], head=chain["head"], base=chain["base"], merge=chain["merge_sha"],
        )
        with self.store.transaction() as db:
            chain = self._chain(db, chain_id)
            successor = self._chain(db, successor_id)
            if successor["state"] not in DEPENDENCY_OK:
                raise SchedulerError("EVIDENCE_MISSING", "replacement dependency is not integrated", extra={"changed": False, "state": successor["state"]})
            deps = [successor_id if db.execute("SELECT state FROM chains WHERE chain_id=?", (dep,)).fetchone()["state"] == "SUPERSEDED" else dep
                    for dep in _load(chain["dependencies_json"])]
            if successor_id not in deps:
                raise SchedulerError("CONFLICT_REVISION", "no superseded dependency to retarget", extra={"changed": False})
            db.execute("UPDATE chains SET dependencies_json=? WHERE chain_id=?", (_json(deps), chain_id))
            self._recompute(db, chain["version"])
            stored = self._chain(db, chain_id)
            self._intend_event(
                db, event_type="intervention.recorded", event_class="authorization", task_id=chain["task_id"], run_id="",
                stage="retarget_dependency", state="AUTHORIZED", revision=1, evidence_ref=evidence_ref,
                app_id=self._app_of(db, chain["task_id"]), candidate_id=chain["candidate_id"], head=chain["head"],
                base=chain["base"], merge=chain["merge_sha"], tree=chain["tree"], operation="retarget_dependency",
            )
        self._flush_events()
        return self._public({"chain_id": chain_id, "queue": stored["queue"], "dependencies": _load(stored["dependencies_json"]), "evidence_ref": evidence_ref})

    def _abandon(self, version: str, evidence_ref: str) -> dict:
        with self.store.transaction() as db:
            row = self._version(db, version)
            if self._version_blocked(db, version) or self._serial_held(db, version):
                raise SchedulerError("SIDE_EFFECT_UNKNOWN", "version has an unknown or open remote effect", extra={"changed": False})
            if db.execute("SELECT 1 FROM chains WHERE version=? AND remote_status='UNKNOWN_REMOTE'", (version,)).fetchone():
                raise SchedulerError("SIDE_EFFECT_UNKNOWN", "remote outcome is unknown", extra={"changed": False})
            state = row["state"]
        self._authorize_manual("abandon_version", version, evidence_ref, version_state=state, task_id="version")
        with self.store.transaction() as db:
            row = self._version(db, version)
            if self._version_blocked(db, version) or self._serial_held(db, version):
                raise SchedulerError("SIDE_EFFECT_UNKNOWN", "version has an unknown or open remote effect", extra={"changed": False})
            db.execute("UPDATE versions SET state='ABANDONED', publish_ref=? WHERE version=?", (evidence_ref, version))
            self._intend_event(
                db, event_type="intervention.recorded", event_class="task", task_id="version", run_id="",
                stage="abandon_version", state="ABANDONED", revision=1, evidence_ref=evidence_ref, app_id="scheduler",
                operation="abandon_version",
            )
        self._flush_events()
        return self._public({"version": version, "state": "ABANDONED", "evidence_ref": evidence_ref, "production_enabled": False})

    def _select_chain(self, db, version: str, phase: str, chain_id: str | None, allow_human: bool = False):
        query = "SELECT * FROM chains WHERE version=? AND queue IN ('READY','SERIAL') ORDER BY priority DESC, created, chain_id"
        rows = db.execute(query, (version,)).fetchall()
        for row in rows:
            if chain_id and row["chain_id"] != chain_id:
                continue
            if not self._phase_ready(row, phase, resume=allow_human):
                continue
            if self._active_subject(db, task_id=row["task_id"], phase=phase, candidate_id=row["candidate_id"], allow_human=allow_human):
                continue
            return row
        return None

    def _phase_ready(self, row, phase: str, resume: bool = False) -> bool:
        evidence = _load(row["evidence_json"])
        if resume and phase == "test" and row["state"] in {"OPEN", "TESTING"} and row["queue"] == "READY" and row["candidate_id"]:
            return True
        if phase in {"develop", "review", "test"}:
            if row["state"] != "OPEN" or row["queue"] != "READY" or not row["candidate_id"]:
                return False
            needed = {"review": "develop", "test": "review"}.get(phase)
            if needed and needed not in evidence:
                return False
            return phase not in evidence
        if phase == "preview_merge":
            return row["state"] == "INTEGRATING" and row["queue"] == "SERIAL" and row["remote_status"] == "NONE" and "preview_merge" not in evidence
        if phase == "deploy":
            return row["state"] == "DEPLOYING" and "deploy" not in evidence
        if phase == "accept":
            return row["state"] == "ACCEPTING" and "accept" not in evidence
        if phase == "publish":
            return row["state"] == "SATISFIED" and "publish" not in evidence
        return False

    def _human_waiting(self, db, version: str, phase: str) -> bool:
        return db.execute(
            """SELECT 1 FROM rounds WHERE version=? AND phase=? AND auth_state IN ('RETRY_WAIT','BLOCKED','FAILED')
               AND dispatch='CONFIRMED' LIMIT 1""",
            (version, phase),
        ).fetchone() is not None

    def _active_subject(self, db, *, task_id: str, phase: str, candidate_id: str, allow_human: bool = False) -> bool:
        row = db.execute(
            """SELECT auth_state, dispatch FROM rounds WHERE task_id=? AND phase=? AND candidate_id=?
               AND dispatch IN ('CONFIRMED','DISPATCH_UNKNOWN')
               ORDER BY created DESC LIMIT 1""",
            (task_id, phase, candidate_id),
        ).fetchone()
        if row is None:
            return False
        if row["dispatch"] == "DISPATCH_UNKNOWN":
            return True
        if allow_human and row["auth_state"] in HUMAN_AUTH:
            return False
        return row["auth_state"] not in TERMINAL_AUTH

    def _insert_intent(self, db, chain, phase: str, caller: dict, *, expected_revision: int):
        task = self._task(db, chain["task_id"])
        self._reject_caller_secrets(caller)
        body = self._claim_intent(
            task_id=chain["task_id"], app_id=task["app_id"], phase=phase, candidate_id=chain["candidate_id"],
            head=chain["head"], base=chain["base"], merge=chain["merge_sha"], tree=chain["tree"],
            object_id=chain["chain_id"],
        )
        body["expected_revision"] = expected_revision
        round_id = self._new_round(db, chain["version"], chain["chain_id"], chain["task_id"], phase, chain["candidate_id"], body)
        db.execute("UPDATE chains SET queue='HELD' WHERE chain_id=?", (chain["chain_id"],))
        return round_id, body

    def _new_round(self, db, version, chain_id, task_id, phase, candidate_id, body) -> str:
        round_id = "rnd_" + secrets.token_hex(8)
        db.execute(
            """INSERT INTO rounds(round_id,version,chain_id,task_id,phase,candidate_id,idempotency_key,intent_json,dispatch,created)
               VALUES(?,?,?,?,?,?,?,?,'INTENT',?)""",
            (round_id, version, chain_id, task_id, phase, candidate_id, body["idempotency_key"], _json(self._persisted_intent(body)), self._stamp(db)),
        )
        return round_id

    def _confirm(self, db, round_id: str, view: dict) -> None:
        db.execute(
            """UPDATE rounds SET dispatch='CONFIRMED', run_id=?, attempt=?, generation=?, revision=?, auth_state=?
               WHERE round_id=?""",
            (view["run_id"], view["attempt"], view["generation"], view["revision"], view["auth_state"], round_id),
        )

    def _abort_intent(self, round_id: str) -> None:
        with self.store.transaction() as db:
            row = self._round(db, round_id)
            if row["dispatch"] != "INTENT":
                return
            db.execute("UPDATE rounds SET dispatch='ABORTED', auth_state='ABORTED' WHERE round_id=?", (round_id,))
            if row["chain_id"]:
                db.execute("UPDATE chains SET queue='READY' WHERE chain_id=? AND queue='HELD'", (row["chain_id"],))
                chain = self._chain(db, row["chain_id"])
                self._recompute(db, chain["version"])

    def _reject_caller_secrets(self, caller: dict) -> None:
        if not isinstance(caller, dict):
            raise SchedulerError("SCHEMA_UNSUPPORTED", "caller is required", status=422)
        if "received_at" in caller:
            raise SchedulerError("SCHEMA_UNSUPPORTED", "received_at is server-owned", status=422)
        if caller.get("state") == "UNKNOWN" or caller.get("result") == "UNKNOWN":
            raise SchedulerError("NOT_AUTHORIZED", "a caller cannot write UNKNOWN", status=403)

    def _claim_intent(self, *, task_id, app_id, phase, candidate_id, head, base, merge, tree, object_id) -> dict:
        return {
            "operation": "run.claim",
            "task_id": task_id,
            "app_id": app_id,
            "phase": phase,
            "candidate_id": candidate_id or "",
            "head": head or "",
            "base": base or "",
            "merge": merge or "",
            "tree": tree or "",
            "object_id": object_id,
            "idempotency_key": self._idem(),
            "expected_revision": 0,
            "recovery": "",
            "result": "",
            "run_id": "",
            "lease_generation": 0,
            "action": "",
            "release_version": "",
        }

    def _bound_intent(self, row, app_id: str, operation: str) -> dict:
        return {
            "operation": operation,
            "task_id": row["task_id"],
            "app_id": app_id,
            "phase": row["phase"],
            "candidate_id": row["candidate_id"] or "",
            "head": "",
            "base": "",
            "merge": "",
            "tree": "",
            "object_id": row["chain_id"] or row["task_id"],
            "idempotency_key": self._idem(),
            "expected_revision": row["revision"] if row["revision"] is not None else 0,
            "recovery": "",
            "result": "",
            "run_id": row["run_id"] or "",
            "lease_generation": row["generation"] if row["generation"] is not None else 0,
            "action": "",
            "release_version": "",
        }

    def _persisted_intent(self, intent: dict) -> dict:
        kept = {
            "operation": intent.get("operation") or "",
            "task_id": intent.get("task_id") or "",
            "app_id": intent.get("app_id") or "",
            "phase": intent.get("phase") or "",
            "candidate_id": intent.get("candidate_id") or "",
            "head": intent.get("head") or "",
            "base": intent.get("base") or "",
            "merge": intent.get("merge") or "",
            "tree": intent.get("tree") or "",
            "object_id": intent.get("object_id") or "",
            "idempotency_key": intent.get("idempotency_key") or "",
            "expected_revision": int(intent.get("expected_revision") or 0),
            "recovery": intent.get("recovery") or "",
            "result": intent.get("result") or "",
            "run_id": "",
            "lease_generation": 0,
            "action": intent.get("action") or "",
            "release_version": intent.get("release_version") or "",
        }
        self._assert_intent_secret_free(kept)
        return kept

    def _assert_intent_secret_free(self, intent: dict) -> None:
        forbidden = {"proof", "actor", "lease_token", "signature", "nonce", "session_id", "token"}
        found = sorted(forbidden.intersection(intent))
        if found:
            raise SchedulerError("EVIDENCE_INCONSISTENT", "persisted intent contains a secret", extra={"changed": False})

    def _normalize(self, operation: str, intent: dict, *, lease_token: str) -> dict:
        task_id = str(intent.get("task_id") or "")
        phase = str(intent.get("phase") or "")
        app_id = str(intent.get("app_id") or "")
        object_id = str(intent.get("object_id") or task_id)
        scope_name = "policy" if operation == "policy.evaluate" else "run"
        scope = {
            "repo": self.repo_id,
            "task_id": task_id,
            "app_id": app_id,
            "credential_scope": scope_name,
            "phase": phase,
            "object_id": object_id,
        }
        run_id = "" if operation == "run.claim" else str(intent.get("run_id") or "")
        generation = 0 if operation == "run.claim" else int(intent.get("lease_generation") or 0)
        return {
            "schema_version": 2,
            "operation": operation,
            "authorizer": "w04-scheduler",
            "repo_id": self.repo_id,
            "task_id": task_id,
            "app_id": app_id,
            "target_app_id": "",
            "producer_id": self.producer_id,
            "stream_id": f"{self.producer_id}:task",
            "object_id": object_id,
            "phase": phase,
            "action": str(intent.get("action") or ""),
            "result": str(intent.get("result") or ""),
            "candidate_id": str(intent.get("candidate_id") or ""),
            "head": str(intent.get("head") or ""),
            "base": str(intent.get("base") or ""),
            "merge": str(intent.get("merge") or ""),
            "tree": str(intent.get("tree") or ""),
            "policy_revision": 1,
            "credential_scope": scope_name,
            "credential_generation": 0,
            "scope": scope,
            "idempotency_key": str(intent.get("idempotency_key") or ""),
            "expected_revision": int(intent.get("expected_revision") or 0),
            "run_id": run_id,
            "lease_generation": generation,
            "lease_token": lease_token,
            "session_id": "",
            "effect_id": "",
            "requested_operation": "",
            "scope_to_rotate": "",
            "target_signer": "",
            "recovery": "" if operation == "run.claim" else str(intent.get("recovery") or ""),
            "release_version": str(intent.get("release_version") or ""),
            "method": "POST",
            "path": self._route_path(operation, task_id, run_id, str(intent.get("release_version") or "")),
            "query": "",
        }

    def _route_path(self, operation: str, task_id: str, run_id: str, version: str) -> str:
        paths = {
            "run.claim": f"/api/v1/control/tasks/{task_id}:claim" if task_id else "",
            "run.renew": f"/api/v1/control/tasks/{task_id}:renew" if task_id else "",
            "run.start": f"/api/v1/control/runs/{run_id}:start" if run_id else "",
            "run.complete": f"/api/v1/control/runs/{run_id}:complete" if run_id else "",
            "run.heartbeat": f"/api/v1/control/runs/{run_id}:heartbeat" if run_id else "",
            "run.release": f"/api/v1/control/runs/{run_id}:release" if run_id else "",
            "policy.evaluate": "/api/v1/control/policy:evaluate",
            "release.accept": f"/api/v1/control/releases/{version}:accept" if version else "",
            "release.reject": f"/api/v1/control/releases/{version}:reject" if version else "",
            "release.publish": f"/api/v1/control/releases/{version}:publish" if version else "",
        }
        return paths.get(operation, "")

    def _seal(self, operation: str, normalized: dict):
        if not isinstance(normalized, dict) or "proof" in normalized or "actor" in normalized:
            raise SchedulerError("SCHEMA_UNSUPPORTED", "a request cannot be signed before it is normalized", status=422)
        signed = self.auth.sign(operation, dict(normalized))
        if not isinstance(signed, dict):
            raise SchedulerError("EVIDENCE_INCONSISTENT", "signer did not return the request", extra={"changed": False})
        for key, value in normalized.items():
            if signed.get(key) != value:
                raise SchedulerError("EVIDENCE_INCONSISTENT", "signer changed the normalized request", extra={"changed": False})
        if not isinstance(signed.get("proof"), dict) or not signed["proof"]:
            raise SchedulerError("PROOF_INVALID", "trusted signature is missing", status=403, extra={"changed": False})
        return signed

    def _dispatch(self, operation: str, intent: dict, *, lease_token: str):
        normalized = self._normalize(operation, intent, lease_token=lease_token)
        signed = self._seal(operation, normalized)
        return self._execute(operation, signed)

    def _settle_claim_failure(self, round_id: str, exc: SchedulerError) -> None:
        if exc.code in EXPLICIT_REJECTION:
            self._abort_intent(round_id)
            return
        self._hold_unknown(round_id)

    def _hold_unknown(self, round_id: str) -> None:
        with self.store.transaction() as db:
            row = self._round(db, round_id)
            if row["dispatch"] not in {"INTENT", "DISPATCH_UNKNOWN"}:
                return
            db.execute(
                "UPDATE rounds SET dispatch='DISPATCH_UNKNOWN', auth_state='DISPATCH_UNKNOWN' WHERE round_id=?",
                (round_id,),
            )

    def _recovery_view(self, body: dict):
        if not isinstance(body, dict) or type(body.get("replayed")) is not bool:
            return None
        if body["replayed"] is True:
            if body.get("lease_token") is not None:
                raise SchedulerError("LEASE_REPLAY_LEAKED", "replay returned a lease token", extra={"changed": False})
            original = body.get("original_result")
            if not isinstance(original, dict) or "run_id" not in original or "lease_generation" not in original:
                return None
            state = body.get("current_state") or original.get("state")
            if state in (None, ""):
                return None
            return {
                "run_id": original["run_id"],
                "attempt": original.get("attempt"),
                "generation": original["lease_generation"],
                "revision": original.get("revision"),
                "auth_state": state,
                "lease_token": None,
            }
        if body.get("state") != "LEASED" or "run_id" not in body or "lease_generation" not in body:
            return None
        return {
            "run_id": body["run_id"],
            "attempt": body.get("attempt"),
            "generation": body["lease_generation"],
            "revision": body.get("revision"),
            "auth_state": "LEASED",
            "lease_token": body.get("lease_token"),
        }

    def _server_fact(self, kind: str, **binding) -> dict:
        try:
            fact = self.auth.verified_record(kind, **binding)
        except SchedulerError:
            raise
        except Exception as exc:
            self._reraise(exc)
        if not isinstance(fact, dict):
            raise SchedulerError("EVIDENCE_MISSING", "server evidence record is missing", extra={"changed": False})
        return fact

    def _ancestry(self, main_sha: str, preview_sha: str, observed_tree: str, candidate_tree: str) -> dict:
        try:
            report = self.auth.observe_ancestry(
                main_sha=main_sha, preview_sha=preview_sha, observed_tree=observed_tree, candidate_tree=candidate_tree,
            )
        except SchedulerError:
            raise
        except Exception as exc:
            self._reraise(exc)
        if not isinstance(report, dict) or type(report.get("is_ancestor")) is not bool:
            raise SchedulerError("EVIDENCE_MISSING", "ancestry observation is missing", extra={"changed": False})
        if report.get("source") in (None, "", "caller"):
            raise SchedulerError("EVIDENCE_INCONSISTENT", "ancestry observation has no server source", extra={"changed": False})
        return {
            "is_ancestor": report["is_ancestor"],
            "main_sha": main_sha,
            "preview_sha": preview_sha,
            "observed_tree": observed_tree,
            "candidate_tree": candidate_tree,
            "source": report["source"],
        }

    def _binding(self, chain) -> dict:
        return {
            "candidate_id": chain["candidate_id"],
            "tree": chain["tree"],
            "head": chain["head"],
            "base": chain["base"],
            "merge": chain["merge_sha"],
        }

    def _phase_fact(self, chain, phase: str, evidence_ref: str) -> dict:
        binding = self._binding(chain)
        if phase == "test":
            fact = self._server_fact("test", **binding, ref=evidence_ref)
            if fact.get("signer_kind") != "macci":
                raise SchedulerError("NOT_AUTHORIZED", "test evidence requires the independent test signer", status=403,
                                     extra={"changed": False})
        elif phase == "accept":
            fact = self._server_fact("acceptance", **binding, ref=evidence_ref)
            if fact.get("signer_kind") != "user_acceptance" or fact.get("outcome") == "rejected":
                raise SchedulerError("NOT_AUTHORIZED", "chain acceptance requires an independent user acceptance", status=403,
                                     extra={"changed": False})
        elif phase == "publish":
            acceptance = self._server_fact("acceptance", **binding)
            test = self._server_fact("test", **binding)
            if acceptance.get("signer_kind") != "user_acceptance" or acceptance.get("outcome") != "accepted":
                raise SchedulerError("NOT_AUTHORIZED", "publish requires an accepted user record for this candidate", status=403,
                                     extra={"changed": False})
            if acceptance.get("candidate_id") != chain["candidate_id"] or acceptance.get("tree") != chain["tree"]:
                raise SchedulerError("EVIDENCE_INCONSISTENT", "acceptance is not bound to this candidate", extra={"changed": False})
            if test.get("candidate_id") != chain["candidate_id"] or test.get("tree") != chain["tree"] or test.get("signer_kind") != "macci":
                raise SchedulerError("EVIDENCE_INCONSISTENT", "test evidence is not bound to this candidate", extra={"changed": False})
            fact = self._server_fact("publish", **binding, ref=evidence_ref)
        else:
            raise SchedulerError("SCHEMA_UNSUPPORTED", "phase has no server evidence", status=422)
        if fact.get("candidate_id") != chain["candidate_id"] or fact.get("tree") != chain["tree"] or fact.get("ref") != evidence_ref:
            raise SchedulerError("EVIDENCE_INCONSISTENT", "evidence is not bound to this candidate", extra={"changed": False})
        if fact.get("head") not in (None, "", chain["head"]) or fact.get("base") not in (None, "", chain["base"]):
            raise SchedulerError("EVIDENCE_INCONSISTENT", "evidence tuple does not match the chain", extra={"changed": False})
        if fact.get("merge") not in (None, "", chain["merge_sha"]):
            raise SchedulerError("EVIDENCE_INCONSISTENT", "evidence tuple does not match the chain", extra={"changed": False})
        return fact

    def _reject_acceptance(self, chain, evidence_ref: str) -> dict:
        self._evidence(evidence_ref)
        try:
            fact = self.auth.reject_acceptance(**self._binding(chain), ref=evidence_ref)
        except SchedulerError:
            raise
        except Exception as exc:
            self._reraise(exc)
        if not isinstance(fact, dict) or fact.get("outcome") != "rejected" or fact.get("signer_kind") != "user_acceptance":
            raise SchedulerError("NOT_AUTHORIZED", "rejection was not recorded by the user signer", status=403, extra={"changed": False})
        if fact.get("candidate_id") != chain["candidate_id"] or fact.get("tree") != chain["tree"]:
            raise SchedulerError("EVIDENCE_INCONSISTENT", "rejection is not bound to this candidate", extra={"changed": False})
        return fact

    def _require_publish_binding(self, chain) -> tuple:
        chain = _materialize(chain)
        evidence = _load(chain["evidence_json"])
        record = evidence.get("publish")
        remote = evidence.get("remote")
        if not isinstance(record, dict) or not record.get("ref"):
            raise SchedulerError("EVIDENCE_INCONSISTENT", "published chain is missing publish evidence")
        binding = self._binding(chain)
        server = self._server_fact("publish", **binding, ref=record.get("ref"))
        if record.get("ref") != server.get("ref") or record.get("candidate_id") != chain["candidate_id"]:
            raise SchedulerError("EVIDENCE_INCONSISTENT", "publish evidence does not match the server candidate")
        if server.get("candidate_id") != chain["candidate_id"] or record.get("tree") != chain["tree"] or server.get("tree") != chain["tree"]:
            raise SchedulerError("EVIDENCE_INCONSISTENT", "publish evidence tree does not match the chain")
        if record.get("head") != chain["head"] or record.get("base") != chain["base"] or record.get("merge") != chain["merge_sha"]:
            raise SchedulerError("EVIDENCE_INCONSISTENT", "publish evidence tuple does not match the chain")
        acceptance = self._server_fact("acceptance", **binding)
        if acceptance.get("outcome") != "accepted" or acceptance.get("signer_kind") != "user_acceptance":
            raise SchedulerError("NOT_AUTHORIZED", "version publish requires an accepted user record", status=403, extra={"changed": False})
        if acceptance.get("candidate_id") != chain["candidate_id"] or acceptance.get("tree") != chain["tree"]:
            raise SchedulerError("EVIDENCE_INCONSISTENT", "acceptance belongs to a different candidate")
        test = self._server_fact("test", **binding)
        if test.get("candidate_id") != chain["candidate_id"] or test.get("tree") != chain["tree"] or test.get("signer_kind") != "macci":
            raise SchedulerError("EVIDENCE_INCONSISTENT", "test evidence belongs to a different candidate")
        if test.get("outcome") not in {"success", "passed"}:
            raise SchedulerError("EVIDENCE_INCONSISTENT", "test evidence is not a successful outcome", extra={"changed": False})
        for fact in (server, acceptance, test):
            if fact.get("head") != chain["head"] or fact.get("base") != chain["base"] or fact.get("merge") != chain["merge_sha"] or fact.get("tree") != chain["tree"]:
                raise SchedulerError("EVIDENCE_INCONSISTENT", "evidence tuple is not the full selected chain", extra={"changed": False})
        if not isinstance(remote, dict) or remote.get("candidate_id") != chain["candidate_id"] or remote.get("head") != chain["head"]:
            raise SchedulerError("EVIDENCE_INCONSISTENT", "merge observation is not bound to this candidate")
        if remote.get("tree") != chain["tree"]:
            raise SchedulerError("EVIDENCE_INCONSISTENT", "merge observation tree does not match the chain")
        return acceptance.get("ref") or record["ref"], record["ref"]

    def _authorize_manual(self, action: str, subject_id: str, evidence_ref: str, *, version_state: str,
                          candidate_id: str = "", tree: str = "", task_id: str = "", head: str = "", base: str = "",
                          merge: str = "") -> dict:
        self._evidence(evidence_ref)
        fact = self._server_fact("intervention", subject=subject_id, ref=evidence_ref, permission=action)
        if fact.get("subject") != subject_id or fact.get("ref") != evidence_ref or fact.get("permission") != action:
            raise SchedulerError("EVIDENCE_INCONSISTENT", "intervention evidence is not bound to this subject", extra={"changed": False})
        self.auth.bind_subject(
            subject_id, version_state=version_state, candidate_id=candidate_id, tree=tree,
            evidence_ref=evidence_ref, action=action,
        )
        intent = self._claim_intent(
            task_id=task_id or subject_id, app_id="scheduler", phase="integrate", candidate_id=candidate_id,
            head=head, base=base, merge=merge, tree=tree, object_id=subject_id,
        )
        intent["operation"] = "policy.evaluate"
        intent["action"] = action
        intent["run_id"] = ""
        result = self._dispatch("policy.evaluate", intent, lease_token="")
        body = result.body
        if not body.get("allow"):
            code = body.get("code") or "NOT_AUTHORIZED"
            self._audit_intervention(action, subject_id, evidence_ref, state="DENIED", task_id=task_id, candidate_id=candidate_id,
                                     head=head, base=base, merge=merge, tree=tree)
            raise SchedulerError(code, "intervention was not authorized", status=403, extra={"changed": False, "production_enabled": False})
        return body

    def _audit_intervention(self, action, subject_id, evidence_ref, *, state, task_id, candidate_id, head, base, merge, tree) -> None:
        event_class = "authorization" if all((head, base, merge, tree)) else "task"
        with self.store.transaction() as db:
            self._intend_event(
                db, event_type="intervention.recorded", event_class=event_class, task_id=task_id or subject_id,
                run_id="", stage=action, state=state, revision=1, evidence_ref=evidence_ref, app_id="scheduler",
                candidate_id=candidate_id, head=head, base=base, merge=merge, tree=tree, operation=action,
            )
        self._flush_events()

    def _auth_view(self, body: dict, *, replay_required: bool) -> dict:
        replayed = bool(body.get("replayed"))
        if replay_required and not replayed:
            raise SchedulerError("EVIDENCE_INCONSISTENT", "restart replay did not match an existing claim", extra={"changed": False})
        if replayed and body.get("lease_token") is not None:
            raise SchedulerError("LEASE_REPLAY_LEAKED", "replay returned a lease token", extra={"changed": False})
        public = body.get("original_result") if replayed else body
        if not isinstance(public, dict) or "run_id" not in public or "lease_generation" not in public:
            raise SchedulerError("EVIDENCE_INCONSISTENT", "auth result did not include the run projection", extra={"changed": False})
        state = body.get("current_state") if replayed and body.get("current_state") else public.get("state")
        return {
            "run_id": public["run_id"],
            "attempt": public.get("attempt"),
            "generation": public["lease_generation"],
            "revision": public.get("revision"),
            "auth_state": state,
            "lease_token": None if replayed else body.get("lease_token"),
        }

    def _execute(self, operation: str, body: dict):
        try:
            return self.auth.execute(operation, body, route_class=ROUTE_CLASS)
        except SchedulerError:
            raise
        except Exception as exc:
            self._reraise(exc)

    def _reraise(self, exc: Exception):
        code = getattr(exc, "code", None)
        if not code:
            raise exc
        raise SchedulerError(code, str(exc), status=getattr(exc, "status", 409), extra=getattr(exc, "extra", {}) or {}) from exc

    def _register_remote_candidate(self, task_id: str, head: str, base: str, merge: str, tree: str) -> str:
        expected = candidate_id(self.repo_id, task_id, head, base, merge, tree)
        issued = self.auth.register_candidate(repo=self.repo_id, task=task_id, head=head, base=base, merge=merge, tree=tree)
        if issued != expected:
            raise SchedulerError("EVIDENCE_INCONSISTENT", "auth candidate id does not match the tuple", extra={"changed": False})
        return expected

    def _recompute(self, db, version: str) -> None:
        rows = [dict(row) for row in db.execute("SELECT * FROM chains WHERE version=?", (version,))]
        fixed = [row for row in rows if self._holds(row) and row["queue"] in {"HELD", "SERIAL", "HUMAN_HOLD", "CLOSED_HOLD"}]
        flexible = [row for row in rows if row["queue"] in {"READY", "WAITING_DEPENDENCY", "WAITING_RESERVATION"} and row["state"] not in {"SUPERSEDED", "PUBLISHED"}]
        flexible.sort(key=lambda row: (-row["priority"], row["created"], row["chain_id"]))
        holders = list(fixed)
        for row in flexible:
            deps = _load(row["dependencies_json"])
            if not self._deps_met(db, deps):
                desired = "WAITING_DEPENDENCY"
            elif self._overlaps(row, holders):
                desired = "WAITING_RESERVATION"
            else:
                desired = "READY"
                holders.append(row)
            if row["queue"] != desired:
                db.execute("UPDATE chains SET queue=? WHERE chain_id=?", (desired, row["chain_id"]))
                row["queue"] = desired

    def _holds(self, row) -> bool:
        if row["state"] in {"SUPERSEDED", "PUBLISHED"} or row["queue"] in {"WAITING_DEPENDENCY", "WAITING_RESERVATION", "RELEASED"}:
            return False
        return row["remote_status"] != "RELEASED"

    def _overlaps(self, row, holders) -> bool:
        paths = _load(row["files_json"])
        for other in holders:
            if other["chain_id"] == row["chain_id"] or not self._holds(other):
                continue
            other_paths = _load(other["files_json"])
            if any(paths_overlap(left, right) for left in paths for right in other_paths):
                return True
        return False

    def _deps_met(self, db, deps) -> bool:
        for dep in deps:
            row = db.execute("SELECT state FROM chains WHERE chain_id=?", (dep,)).fetchone()
            if row is None or row["state"] not in DEPENDENCY_OK:
                return False
        return True

    def _reaches(self, db, deps, target: str) -> bool:
        graph = {row["chain_id"]: _load(row["dependencies_json"]) for row in db.execute("SELECT chain_id,dependencies_json FROM chains")}
        stack = list(deps)
        seen = set()
        while stack:
            item = stack.pop()
            if item == target:
                return True
            if item in seen:
                continue
            seen.add(item)
            stack.extend(graph.get(item, []))
        return False

    def _serial_held(self, db, version: str) -> bool:
        return db.execute(
            """SELECT 1 FROM chains WHERE version=? AND (state='INTEGRATING' OR remote_status IN ('PUSH_INTENT','UNKNOWN_REMOTE','SHA_MISMATCH','PENDING')) LIMIT 1""",
            (version,),
        ).fetchone() is not None

    def _version_blocked(self, db, version: str) -> bool:
        return db.execute(
            """SELECT 1 FROM rounds WHERE version=? AND (auth_state='UNKNOWN' OR dispatch='DISPATCH_UNKNOWN') LIMIT 1""",
            (version,),
        ).fetchone() is not None

    def _chain_unknown(self, db, chain_id: str) -> bool:
        return db.execute(
            "SELECT 1 FROM rounds WHERE chain_id=? AND auth_state='UNKNOWN' LIMIT 1",
            (chain_id,),
        ).fetchone() is not None

    def _require_active(self, db, version: str) -> None:
        row = self._version(db, version)
        if row["state"] == "FROZEN":
            raise SchedulerError("FREEZE_ACTIVE", "frozen version rejects this candidate", extra={"changed": False, "state": "FROZEN"})
        if row["state"] != "ACTIVE":
            raise SchedulerError("VERSION_NOT_RELEASED", "version is not active", extra={"state": row["state"]})

    def _version(self, db, version: str):
        version = require_id(version, "version")
        row = db.execute("SELECT * FROM versions WHERE version=?", (version,)).fetchone()
        if row is None:
            raise SchedulerError("EVIDENCE_MISSING", "version is not open", status=404)
        return _materialize(row)

    def _task(self, db, task_id: str):
        task_id = require_id(task_id, "task")
        row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise SchedulerError("EVIDENCE_MISSING", "task is not open", status=404)
        return _materialize(row)

    def _chain(self, db, chain_id: str):
        chain_id = require_id(chain_id, "chain")
        row = db.execute("SELECT * FROM chains WHERE chain_id=?", (chain_id,)).fetchone()
        if row is None:
            raise SchedulerError("EVIDENCE_MISSING", "chain is not registered", status=404)
        return _materialize(row)

    def _round(self, db, round_id: str):
        row = db.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
        if row is None:
            raise SchedulerError("EVIDENCE_MISSING", "round is not known", status=404)
        return _materialize(row)

    def _app_of(self, db, task_id: str) -> str:
        task = self._task(db, task_id)
        return task["app_id"]

    def _intend_event(self, db, **fields) -> None:
        if fields.get("state") == "UNKNOWN":
            raise SchedulerError("NOT_AUTHORIZED", "scheduler cannot append UNKNOWN", status=403)
        event_class = fields.get("event_class") or "task"
        channel = event_class if event_class in _CHANNELS else "task"
        if event_class == "evidence":
            channel = "task"
        stream_id = f"{self.producer_id}:{channel}"
        epoch = 1
        seq = self._next_seq(db, stream_id, epoch)
        event_id = "evt_" + hashlib.sha256(
            f"{self.repo_id}|{stream_id}|{epoch}|{seq}|{fields['event_type']}".encode()
        ).hexdigest()[:32]
        idem = fields.get("idempotency_key") or ("idem-w04-" + hashlib.sha256(event_id.encode()).hexdigest()[:32])
        app_id = fields.get("app_id") or "scheduler"
        payload = {
            "schema_version": 2,
            "event_id": event_id,
            "event_type": fields["event_type"],
            "event_class": event_class,
            "repo_id": self.repo_id,
            "producer_id": self.producer_id,
            "stream_id": stream_id,
            "stream_epoch": epoch,
            "producer_seq": seq,
            "occurred_at": str(self._now()),
            "idempotency_key": idem,
            "app_id": app_id,
            "host_id": None,
            "task_id": fields.get("task_id") or "",
            "run_id": fields.get("run_id") or "",
            "attempt": int(fields.get("attempt") or 0),
            "revision": int(fields.get("revision") or 0),
            "stage": fields.get("stage") or "",
            "state": fields.get("state") or "",
            "candidate_id": fields.get("candidate_id") or "",
            "evidence_ref": fields.get("evidence_ref") or "",
            "head": fields.get("head") or "",
            "base": fields.get("base") or "",
            "merge": fields.get("merge") or "",
            "tree": fields.get("tree") or "",
            "operation": fields.get("operation") or fields["event_type"],
            "policy_revision": 1,
        }
        self._assert_intent_secret_free(payload)
        db.execute(
            """INSERT INTO event_intents(event_id,idempotency_key,stream_id,stream_epoch,producer_seq,repo_id,app_id,
               payload_json,status,trusted_source,created) VALUES(?,?,?,?,?,?,?,?,'PENDING','',?)""",
            (event_id, idem, stream_id, epoch, seq, self.repo_id, app_id, _json(payload), self._stamp(db)),
        )

    def _next_seq(self, db, stream_id: str, epoch: int) -> int:
        key = f"seq:{stream_id}:{epoch}"
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        value = 1 if row is None else int(row["value"]) + 1
        db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        return value

    def _flush_events(self) -> dict:
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT * FROM event_intents WHERE status='PENDING' ORDER BY stream_id, producer_seq, event_id"
            ).fetchall()
            pending = [_materialize(row) for row in rows]
        delivered = []
        rejected = []
        waiting = []
        for intent in pending:
            payload = _load(intent["payload_json"])
            try:
                request = self._event_request(payload)
                signed = self.events.sign(dict(request))
                self._assert_signed_same(request, signed)
                result = self.events.append(signed)
            except Exception:
                waiting.append(intent["event_id"])
                break
            if self._event_rejected(result):
                with self.store.transaction() as db:
                    db.execute("UPDATE event_intents SET status='REJECTED' WHERE event_id=?", (intent["event_id"],))
                rejected.append(intent["event_id"])
                break
            observed = self._reconcile_event(payload)
            if observed is None:
                waiting.append(intent["event_id"])
                break
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE event_intents SET status='DELIVERED', trusted_source=? WHERE event_id=?",
                    (observed, intent["event_id"]),
                )
            delivered.append(intent["event_id"])
        return {"delivered": delivered, "rejected": rejected, "pending": waiting}

    def _event_request(self, payload: dict) -> dict:
        request = {
            "schema_version": 2,
            "operation": "event.append",
            "expected_revision": 0,
            "idempotency_key": payload["idempotency_key"],
            "scope": {"repo": self.repo_id, "stream_id": payload["stream_id"]},
            "side_effect": False,
        }
        request.update(payload)
        request.pop("github_execution", None)
        request.pop("macci_proof", None)
        return request

    def _assert_signed_same(self, normalized: dict, signed: dict) -> None:
        if not isinstance(signed, dict) or not isinstance(signed.get("proof"), dict):
            raise SchedulerError("PROOF_INVALID", "event signature is missing", status=403, extra={"changed": False})
        for key, value in normalized.items():
            if signed.get(key) != value:
                raise SchedulerError("EVIDENCE_INCONSISTENT", "event signer changed the normalized document", extra={"changed": False})

    def _event_rejected(self, result) -> bool:
        if not isinstance(result, dict):
            return True
        if result.get("replayed") is True:
            return False
        code = result.get("code")
        if code and code not in {"OK", "IDEMPOTENCY_REPLAY"}:
            return True
        if isinstance(result.get("http"), int) and result["http"] >= 400:
            return True
        if result.get("applied") is False:
            return True
        return False

    def _reconcile_event(self, payload: dict):
        digest = hashlib.sha256(_json(payload).encode()).hexdigest()
        page = self._query_events(payload["stream_id"], int(payload["stream_epoch"]), int(payload["producer_seq"]) - 1, limit=10)
        matched = None
        for item in page.get("items") or []:
            if item.get("stream_id") == payload.get("stream_id") and item.get("producer_seq") == payload.get("producer_seq") and item.get("event_id") not in (None, payload.get("event_id")):
                return None
            if not all(item.get(key) == payload.get(key) for key in ("event_id", "repo_id", "app_id", "stream_id", "stream_epoch", "producer_seq", "idempotency_key")):
                continue
            if any(item.get(key) != value for key, value in payload.items()):
                continue
            declared = item.get("payload_digest")
            if declared is not None and declared != digest:
                continue
            source = item.get("trusted_source")
            if source != self.event_authority:
                continue
            matched = source
        return matched

    def _query_events(self, stream_id: str, epoch: int, after_seq: int, limit: int = 100) -> dict:
        request = {
            "schema_version": 2,
            "operation": "event.read",
            "repo_id": self.repo_id,
            "app_id": "scheduler",
            "producer_id": self.producer_id,
            "stream_id": stream_id,
            "stream_epoch": epoch,
            "after_seq": after_seq,
            "expected_revision": 0,
            "idempotency_key": "idem-w04-read",
            "side_effect": False,
        }
        signed = self.events.sign(dict(request))
        self._assert_signed_same(request, signed)
        return self.events.query(
            request=signed, stream_id=stream_id, stream_epoch=epoch, after_seq=after_seq, limit=limit,
        )

    def _event_identity(self, item: dict, stream_id: str, stream_epoch: int):
        required = ("repo_id", "app_id", "event_id", "trusted_source", "producer_seq")
        if any(not isinstance(item.get(key), str) or not item.get(key) for key in required if key != "producer_seq"):
            return None
        if item.get("stream_id") != stream_id or item.get("stream_epoch") != stream_epoch:
            return None
        if type(item.get("producer_seq")) is not int:
            return None
        return {
            "repo_id": item["repo_id"],
            "app_id": item["app_id"],
            "stream_id": stream_id,
            "stream_epoch": stream_epoch,
            "event_id": item["event_id"],
            "trusted_source": item["trusted_source"],
        }

    def _authoritative(self, item: dict) -> bool:
        expected = AUTHORITY.get(str(item.get("event_type") or ""))
        return bool(expected) and item.get("trusted_source") == expected

    def _event_seen(self, db, identity: dict) -> bool:
        row = db.execute(
            """SELECT 1 FROM applied_events WHERE repo_id=? AND app_id=? AND stream_id=? AND stream_epoch=?
               AND event_id=? AND trusted_source=?""",
            (identity["repo_id"], identity["app_id"], identity["stream_id"], identity["stream_epoch"], identity["event_id"], identity["trusted_source"]),
        ).fetchone()
        return row is not None

    def _remember_event(self, db, identity: dict) -> None:
        db.execute(
            """INSERT OR IGNORE INTO applied_events(repo_id,app_id,stream_id,stream_epoch,event_id,trusted_source)
               VALUES(?,?,?,?,?,?)""",
            (identity["repo_id"], identity["app_id"], identity["stream_id"], identity["stream_epoch"], identity["event_id"], identity["trusted_source"]),
        )

    def _save_watermark(self, db, stream_id: str, stream_epoch: int, seq: int) -> None:
        db.execute(
            """INSERT INTO watermarks(stream_id,stream_epoch,seq) VALUES(?,?,?)
               ON CONFLICT(stream_id,stream_epoch) DO UPDATE SET seq=excluded.seq""",
            (stream_id, stream_epoch, seq),
        )

    def _files(self, files) -> list:
        if not isinstance(files, (list, tuple)) or not 1 <= len(files) <= 1000:
            raise SchedulerError("SCHEMA_UNSUPPORTED", "invalid reservations", status=422)
        paths = sorted({normalize_path(path) for path in files})
        if len(paths) != len(files):
            raise SchedulerError("SCHEMA_UNSUPPORTED", "duplicate reservation", status=422)
        return paths

    def _deps(self, dependencies) -> list:
        if not isinstance(dependencies, (list, tuple)) or len(dependencies) > 100:
            raise SchedulerError("SCHEMA_UNSUPPORTED", "invalid dependencies", status=422)
        return sorted({require_id(dep, "dependency") for dep in dependencies})

    def _priority(self, priority: int) -> int:
        if type(priority) is not int or isinstance(priority, bool) or not -100 <= priority <= 100:
            raise SchedulerError("SCHEMA_UNSUPPORTED", "invalid priority", status=422)
        return priority

    def _tuple(self, head: str, base: str, merge: str, tree: str):
        values = (head, base, merge, tree)
        if all(not value for value in values):
            return ("", "", "", "")
        if not all(values):
            raise SchedulerError("SCHEMA_UNSUPPORTED", "candidate tuple is incomplete", status=422)
        return tuple(require_sha(value, "sha") for value in values)

    def _evidence(self, value: str) -> str:
        if not isinstance(value, str) or not value.startswith("evid-") or len(value) > 200 or any(char.isspace() for char in value):
            raise SchedulerError("EVIDENCE_MISSING", "evidence reference is missing", status=422)
        return value

    def _now(self) -> int:
        return int(self.clock())

    def _stamp(self, db) -> int:
        row = db.execute("SELECT value FROM meta WHERE key='stamp'").fetchone()
        value = max(self._now(), (0 if row is None else int(row["value"])) + 1)
        db.execute(
            "INSERT INTO meta(key,value) VALUES('stamp',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (value,),
        )
        return value

    def _idem(self) -> str:
        return "idem-w04-" + secrets.token_hex(16)

    def _public(self, payload: dict) -> dict:
        body = {
            "schema_version": 2,
            "github_execution": False,
            "macci_proof": False,
            "production_wired": False,
            "joint_integration_ready": False,
        }
        body.update(payload)
        body["github_execution"] = False
        body["macci_proof"] = False
        body["production_wired"] = False
        body["production_enabled"] = False
        return body

    def _version_view(self, row) -> dict:
        return {key: row[key] for key in row.keys()}

    def _task_view(self, row) -> dict:
        return {
            "task_id": row["task_id"], "version": row["version"], "app_id": row["app_id"], "worktree": row["worktree"],
            "acceptance_ref": row["acceptance_ref"], "acceptance_signer": row["acceptance_signer"], "closed": bool(row["closed"]),
        }

    def _chain_view(self, row) -> dict:
        return {
            "chain_id": row["chain_id"], "task_id": row["task_id"], "pr_line": row["pr_line"], "worktree": row["worktree"],
            "files": _load(row["files_json"]), "dependencies": _load(row["dependencies_json"]), "priority": row["priority"],
            "state": row["state"], "queue": row["queue"], "candidate_id": row["candidate_id"], "head": row["head"],
            "base": row["base"], "merge": row["merge_sha"], "tree": row["tree"], "evidence": _load(row["evidence_json"]),
            "remote_status": row["remote_status"], "superseded_by": row["superseded_by"],
        }

    def _round_view(self, row) -> dict:
        return {
            "round_id": row["round_id"], "chain_id": row["chain_id"], "task_id": row["task_id"], "phase": row["phase"],
            "candidate_id": row["candidate_id"], "dispatch": row["dispatch"], "run_id": row["run_id"],
            "attempt": row["attempt"], "lease_generation": row["generation"], "revision": row["revision"],
            "auth_state": row["auth_state"], "evidence_ref": row["evidence_ref"],
        }
