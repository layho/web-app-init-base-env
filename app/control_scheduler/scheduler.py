"""Task, candidate-chain, and version scheduler.

Lease tokens, fencing, and UNKNOWN are decided by the auth port. Event order
and durability are decided by the event port. This module decides which
subject may run, which dependency or reservation is waiting, and which
evidence is allowed to move a chain or a version.
"""

from __future__ import annotations

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
                 stream_id: str = "w04-scheduler:task"):
        self.root = Path(root)
        self.auth = auth
        self.events = events
        self.clock = clock or (lambda: int(time.time()))
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
        self._append_event(event_type="version.frozen", event_class="task", task_id="", run_id="", stage="integrate",
                           state="FROZEN", revision=1, evidence_ref=evidence_ref)
        return self._public({"version": version, "state": "FROZEN"})

    def publish_version(self, version: str, *, observed_tree: str, candidate_tree: str, main_sha: str,
                        preview_sha: str, preview_is_ancestor: bool, acceptance_ref: str, publish_ref: str,
                        acceptance_signer: str) -> dict:
        observed_tree = require_sha(observed_tree, "tree")
        candidate_tree = require_sha(candidate_tree, "tree")
        main_sha = require_sha(main_sha, "main")
        preview_sha = require_sha(preview_sha, "preview")
        self._evidence(acceptance_ref)
        self._evidence(publish_ref)
        if type(preview_is_ancestor) is not bool:
            raise SchedulerError("SCHEMA_UNSUPPORTED", "ancestry must be a boolean", status=422)
        with self.store.transaction() as db:
            row = self._version(db, version)
            if row["state"] not in {"ACTIVE", "FROZEN"}:
                raise SchedulerError("VERSION_NOT_RELEASED", "version cannot be published from this state",
                                     extra={"state": row["state"]})
            if observed_tree == candidate_tree and not preview_is_ancestor:
                raise SchedulerError("FREEZE_ACTIVE", "equal trees do not unfreeze a version",
                                     extra={"changed": False, "state": row["state"]})
            if observed_tree != candidate_tree or not preview_is_ancestor:
                raise SchedulerError("EVIDENCE_MISSING", "publish requires the observed tree and preview ancestry",
                                     extra={"changed": False, "state": row["state"]})
            if acceptance_signer != "user_acceptance":
                raise SchedulerError("NOT_AUTHORIZED", "version acceptance requires user_acceptance", status=403,
                                     extra={"changed": False})
            unfinished = db.execute(
                "SELECT chain_id,state FROM chains WHERE version=? AND state!='SUPERSEDED' AND state!='PUBLISHED'",
                (version,),
            ).fetchall()
            if unfinished:
                raise SchedulerError("VERSION_NOT_RELEASED", "necessary chains are not published",
                                     extra={"changed": False, "state": row["state"], "chains": [item["chain_id"] for item in unfinished]})
            published = db.execute("SELECT evidence_json,candidate_id FROM chains WHERE version=? AND state='PUBLISHED'", (version,)).fetchall()
            if not published:
                raise SchedulerError("VERSION_NOT_RELEASED", "a version with no published chain cannot be published",
                                     extra={"changed": False})
            for chain in published:
                evidence = _load(chain["evidence_json"])
                record = evidence.get("publish")
                if not record or record.get("candidate_id") != chain["candidate_id"] or record.get("ref") != record.get("ref"):
                    raise SchedulerError("EVIDENCE_INCONSISTENT", "published chain is missing publish evidence")
                if not record.get("ref"):
                    raise SchedulerError("EVIDENCE_INCONSISTENT", "published chain is missing publish evidence")
            db.execute(
                """UPDATE versions SET state='PUBLISHED', acceptance_ref=?, publish_ref=?, published_tree=?,
                   main_sha=?, preview_sha=? WHERE version=?""",
                (acceptance_ref, publish_ref, observed_tree, main_sha, preview_sha, version),
            )
        self._append_event(event_type="version.published", event_class="task", task_id="", run_id="", stage="publish",
                           state="PUBLISHED", revision=1, evidence_ref=publish_ref)
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

    def record_task_acceptance(self, task_id: str, *, evidence_ref: str, signer_kind: str) -> dict:
        self._evidence(evidence_ref)
        with self.store.transaction() as db:
            task = self._task(db, task_id)
            if task["closed"]:
                raise SchedulerError("CONFLICT_REVISION", "closed task cannot accept a new record")
            if signer_kind != "user_acceptance":
                raise SchedulerError("NOT_AUTHORIZED", "task acceptance requires user_acceptance", status=403,
                                     extra={"changed": False})
            db.execute(
                "UPDATE tasks SET acceptance_ref=?, acceptance_signer=? WHERE task_id=?",
                (evidence_ref, signer_kind, task_id),
            )
        return self._public({"task_id": task_id, "acceptance_recorded": True, "closed": False})

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
        self._append_event(event_type="task.closed", event_class="task", task_id=task_id, run_id="", stage="close",
                           state="CLOSED", revision=1, evidence_ref=task["acceptance_ref"])
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
        self._append_event(event_type="candidate.bound", event_class="candidate", task_id=chain["task_id"], run_id="",
                           stage="develop", state="OPEN", candidate_id=candidate, revision=1)
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
        self._append_event(event_type="candidate.superseded", event_class="candidate", task_id=chain["task_id"],
                           run_id="", stage="develop", state="SUPERSEDED", candidate_id=chain["candidate_id"], revision=1)
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

    def complete(self, *, round_id: str, caller: dict, result: str, evidence_ref: str, signer_kind: str = "",
                 evidence_candidate: str | None = None) -> dict:
        if result not in {"SUCCEEDED", "FAILED"}:
            raise SchedulerError("SCHEMA_UNSUPPORTED", "result is not a terminal run result", status=422)
        if result == "UNKNOWN":
            raise SchedulerError("NOT_AUTHORIZED", "a caller cannot write UNKNOWN", status=403)
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
                self._precheck_complete(chain, row["phase"], result, evidence_ref, signer_kind)
        response = self._bound_call(round_id, caller, operation="run.complete", extra_overlay={"result": result})
        with self.store.transaction() as db:
            row = self._round(db, round_id)
            if row["chain_id"]:
                self._apply_complete(db, row, result, evidence_ref, signer_kind)
        response["evidence_ref"] = evidence_ref if result == "SUCCEEDED" else evidence_ref
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
                self._apply_complete(db, row, "SUCCEEDED", evidence_ref, "")
            elif row["chain_id"] and classification == "failed":
                self._apply_complete(db, row, "FAILED", evidence_ref, "")
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
        self._append_event(event_type="chain.integrating", event_class="candidate", task_id=chosen["task_id"], run_id="",
                           stage="integrate", state="INTEGRATING", candidate_id=chosen["candidate_id"], revision=1)
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
        request = request or {"side_effect": False}
        page = self.events.query(request=request, stream_id=stream_id, stream_epoch=stream_epoch, after_seq=0, limit=100)
        items = sorted(page.get("items") or [], key=lambda item: int(item["producer_seq"]))
        with self.store.transaction() as db:
            row = db.execute("SELECT seq FROM watermarks WHERE stream_id=? AND stream_epoch=?", (stream_id, stream_epoch)).fetchone()
            watermark = 0 if row is None else int(row["seq"])
            applied = []
            gap = False
            for item in items:
                seq = int(item["producer_seq"])
                if seq <= watermark:
                    continue
                if seq != watermark + 1:
                    gap = True
                    break
                key = str(item.get("idempotency_key") or "")
                known = key and db.execute("SELECT 1 FROM applied_events WHERE idempotency_key=?", (key,)).fetchone()
                if known or item.get("origin") == "control_scheduler":
                    watermark = seq
                    continue
                if not self._apply_external(db, item):
                    gap = False
                    db.execute(
                        "INSERT INTO watermarks(stream_id,stream_epoch,seq) VALUES(?,?,?) ON CONFLICT(stream_id,stream_epoch) DO UPDATE SET seq=excluded.seq",
                        (stream_id, stream_epoch, watermark),
                    )
                    return self._public({"applied": applied, "gap": False, "stopped_at": seq, "reason": "illegal", "watermark": watermark})
                watermark = seq
                applied.append(seq)
            db.execute(
                "INSERT INTO watermarks(stream_id,stream_epoch,seq) VALUES(?,?,?) ON CONFLICT(stream_id,stream_epoch) DO UPDATE SET seq=excluded.seq",
                (stream_id, stream_epoch, watermark),
            )
        return self._public({"applied": applied, "gap": gap, "watermark": watermark, "side_effect": False})

    def recover(self) -> dict:
        with self.store.transaction() as db:
            pending = db.execute("SELECT * FROM rounds WHERE dispatch='INTENT'").fetchall()
        restored = []
        unknown = []
        for row in pending:
            body = _load(row["body_json"])
            try:
                result = self._execute("run.claim", body)
            except SchedulerError as exc:
                with self.store.transaction() as db:
                    db.execute("UPDATE rounds SET dispatch='DISPATCH_UNKNOWN', auth_state='DISPATCH_UNKNOWN' WHERE round_id=?", (row["round_id"],))
                unknown.append({"round_id": row["round_id"], "code": exc.code})
                continue
            view = self._auth_view(result.body, replay_required=True)
            with self.store.transaction() as db:
                self._confirm(db, row["round_id"], view)
            restored.append({"round_id": row["round_id"], "run_id": view["run_id"], "lease_token": None})
        inconsistent = self.audit()
        return self._public({"restored": restored, "dispatch_unknown": unknown, "inconsistent": inconsistent, "tokens_stored": False})

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
            body = self._auth_body(caller, operation="run.claim", overlay={
                "task_id": task_id, "phase": phase, "candidate_id": "", "head": "", "base": "", "merge": "", "tree": "",
                "idempotency_key": self._idem(), "expected_revision": 0, "app_id": task["app_id"], "recovery": "",
            })
            round_id = self._new_round(db, version, None, task_id, phase, "", body)
        return self._finish_claim(round_id, body, fault, None)

    def _finish_claim(self, round_id: str, body: dict, fault: str | None, pinned_pr: str | None) -> dict:
        try:
            result = self._execute("run.claim", body)
        except SchedulerError as exc:
            self._abort_intent(round_id)
            raise SchedulerError(exc.code, exc.message, status=exc.status, extra={**exc.extra, "changed": False}) from exc
        if fault == "after-auth-before-confirm":
            raise SchedulerError("CRASH_INJECTED", "isolated fault before confirm", status=500, extra={"changed": False})
        view = self._auth_view(result.body, replay_required=False)
        with self.store.transaction() as db:
            self._confirm(db, round_id, view)
            row = self._round(db, round_id)
        self._append_event(event_type="run.claimed", event_class="task", task_id=row["task_id"], run_id=view["run_id"],
                           stage=row["phase"], state=view["auth_state"], attempt=view["attempt"], revision=view["revision"],
                           candidate_id=row["candidate_id"], idempotency_key=row["idempotency_key"])
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
            overlay = {
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "phase": row["phase"],
                "candidate_id": row["candidate_id"],
                "idempotency_key": self._idem(),
                "expected_revision": row["revision"] if row["revision"] is not None else 0,
                "lease_generation": row["generation"],
            }
            if row["chain_id"]:
                chain = self._chain(db, row["chain_id"])
                overlay.update({"head": chain["head"], "base": chain["base"], "merge": chain["merge_sha"], "tree": chain["tree"]})
            if "expected_revision" in caller:
                overlay["expected_revision"] = caller["expected_revision"]
            if "lease_generation" in caller:
                overlay["lease_generation"] = caller["lease_generation"]
            if "lease_token" not in caller:
                raise SchedulerError("LEASE_FENCE_REJECTED", "current token is required", extra={"changed": False, "state": row["auth_state"]})
            overlay["lease_token"] = caller["lease_token"]
            if extra_overlay:
                overlay.update(extra_overlay)
            body = self._auth_body(caller, operation=operation, overlay=overlay)
            before = row["auth_state"]
        try:
            result = self._execute(operation, body)
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

    def _apply_complete(self, db, row, result: str, evidence_ref: str, signer_kind: str) -> None:
        chain = self._chain(db, row["chain_id"])
        phase = row["phase"]
        evidence = _load(chain["evidence_json"])
        if result == "SUCCEEDED":
            evidence[phase] = {
                "ref": evidence_ref, "candidate_id": chain["candidate_id"], "head": chain["head"],
                "base": chain["base"], "merge": chain["merge_sha"], "tree": chain["tree"], "signer_kind": signer_kind,
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

    def _precheck_complete(self, chain, phase: str, result: str, evidence_ref: str, signer_kind: str) -> None:
        if result == "SUCCEEDED":
            self._evidence(evidence_ref)
        if phase == "accept" and result == "SUCCEEDED" and signer_kind != "user_acceptance":
            raise SchedulerError("NOT_AUTHORIZED", "chain acceptance requires user_acceptance", status=403, extra={"changed": False})
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
            if row["auth_state"] == "RETRY_WAIT":
                pass
            chain = self._chain(db, row["chain_id"]) if row["chain_id"] else None
            if chain and chain["state"] in {"FAILED", "REJECTED", "SUPERSEDED"}:
                raise SchedulerError("CONFLICT_REVISION", "a failed chain needs a new chain", extra={"changed": False, "state": chain["state"]})
            if chain and chain["queue"] == "HUMAN_HOLD":
                db.execute("UPDATE chains SET queue='READY' WHERE chain_id=?", (chain["chain_id"],))
            version = row["version"]
            phase = row["phase"]
            chain_id = row["chain_id"]
            task_id = row["task_id"]
            previous_run_id = row["run_id"]
        claimed = self.claim_next(
            version=version, phase=phase, caller={**caller, "task_id": task_id}, chain_id=chain_id, allow_human=True,
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
            db.execute("UPDATE chains SET state='SUPERSEDED', queue='RELEASED' WHERE chain_id=?", (chain_id,))
            self._recompute(db, chain["version"])
        return self._public({"chain_id": chain_id, "state": "SUPERSEDED", "evidence_ref": evidence_ref, "evidence_copied": False})

    def _authorize_repair(self, chain_id: str, successor_id: str, evidence_ref: str) -> dict:
        if not successor_id:
            raise SchedulerError("EVIDENCE_MISSING", "repair requires a new chain", extra={"changed": False})
        with self.store.transaction() as db:
            chain = self._chain(db, chain_id)
            successor = self._chain(db, successor_id)
            if chain["state"] != "REJECTED":
                raise SchedulerError("CONFLICT_REVISION", "repair starts from a rejected chain", extra={"state": chain["state"]})
            if successor["task_id"] != chain["task_id"] or not successor["candidate_id"]:
                raise SchedulerError("CANDIDATE_STALE", "repair chain is not a new candidate", extra={"changed": False})
            if _load(successor["evidence_json"]):
                raise SchedulerError("EVIDENCE_INCONSISTENT", "old evidence cannot move to the repair chain", extra={"changed": False})
            db.execute("UPDATE chains SET state='SUPERSEDED', queue='RELEASED', superseded_by=? WHERE chain_id=?", (successor_id, chain_id))
            self._recompute(db, chain["version"])
        return self._public({"chain_id": chain_id, "state": "SUPERSEDED", "successor": successor_id, "evidence_copied": False, "evidence_ref": evidence_ref})

    def _retarget(self, chain_id: str, successor_id: str, evidence_ref: str) -> dict:
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
        return self._public({"chain_id": chain_id, "queue": stored["queue"], "dependencies": _load(stored["dependencies_json"]), "evidence_ref": evidence_ref})

    def _abandon(self, version: str, evidence_ref: str) -> dict:
        with self.store.transaction() as db:
            row = self._version(db, version)
            if self._version_blocked(db, version) or self._serial_held(db, version):
                raise SchedulerError("SIDE_EFFECT_UNKNOWN", "version has an unknown or open remote effect", extra={"changed": False})
            if db.execute("SELECT 1 FROM chains WHERE version=? AND remote_status='UNKNOWN_REMOTE'", (version,)).fetchone():
                raise SchedulerError("SIDE_EFFECT_UNKNOWN", "remote outcome is unknown", extra={"changed": False})
            db.execute("UPDATE versions SET state='ABANDONED', publish_ref=? WHERE version=?", (evidence_ref, version))
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
            """SELECT auth_state FROM rounds WHERE task_id=? AND phase=? AND candidate_id=? AND dispatch='CONFIRMED'
               ORDER BY created DESC LIMIT 1""",
            (task_id, phase, candidate_id),
        ).fetchone()
        if row is None:
            return False
        if allow_human and row["auth_state"] in HUMAN_AUTH:
            return False
        return row["auth_state"] not in TERMINAL_AUTH

    def _insert_intent(self, db, chain, phase: str, caller: dict, *, expected_revision: int):
        task = self._task(db, chain["task_id"])
        body = self._auth_body(caller, operation="run.claim", overlay={
            "task_id": chain["task_id"], "phase": phase, "candidate_id": chain["candidate_id"],
            "head": chain["head"], "base": chain["base"], "merge": chain["merge_sha"], "tree": chain["tree"],
            "idempotency_key": self._idem(), "expected_revision": expected_revision, "app_id": task["app_id"],
            "recovery": "",
        })
        round_id = self._new_round(db, chain["version"], chain["chain_id"], chain["task_id"], phase, chain["candidate_id"], body)
        db.execute("UPDATE chains SET queue='HELD' WHERE chain_id=?", (chain["chain_id"],))
        return round_id, body

    def _new_round(self, db, version, chain_id, task_id, phase, candidate_id, body) -> str:
        round_id = "rnd_" + secrets.token_hex(8)
        db.execute(
            """INSERT INTO rounds(round_id,version,chain_id,task_id,phase,candidate_id,idempotency_key,body_json,dispatch,created)
               VALUES(?,?,?,?,?,?,?,?,'INTENT',?)""",
            (round_id, version, chain_id, task_id, phase, candidate_id, body["idempotency_key"], _json(body), self._stamp(db)),
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

    def _auth_body(self, caller: dict, *, operation: str, overlay: dict) -> dict:
        if not isinstance(caller, dict):
            raise SchedulerError("SCHEMA_UNSUPPORTED", "caller is required", status=422)
        if "received_at" in caller:
            raise SchedulerError("SCHEMA_UNSUPPORTED", "received_at is server-owned", status=422)
        if caller.get("state") == "UNKNOWN" or caller.get("result") == "UNKNOWN":
            raise SchedulerError("NOT_AUTHORIZED", "a caller cannot write UNKNOWN", status=403)
        body = dict(caller)
        for dropped in ("ttl", "occurred_at", "received_at", "expires_at", "state"):
            body.pop(dropped, None)
        if operation == "run.claim":
            body.pop("run_id", None)
            body.pop("lease_token", None)
            body.pop("lease_generation", None)
            body["recovery"] = ""
        body["schema_version"] = 2
        body.update(overlay)
        body["schema_version"] = 2
        if operation == "run.claim":
            body.pop("run_id", None)
            body["recovery"] = ""
        return body

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

    def _append_event(self, **fields) -> None:
        if fields.get("state") == "UNKNOWN":
            raise SchedulerError("NOT_AUTHORIZED", "scheduler cannot append UNKNOWN", status=403)
        body = {
            "schema_version": 2,
            "event_id": "evt_" + secrets.token_hex(8),
            "event_type": fields["event_type"],
            "event_class": fields.get("event_class", "task"),
            "repo_id": self.repo_id,
            "producer_id": self.producer_id,
            "stream_id": self.stream_id,
            "stream_epoch": 1,
            "occurred_at": self._now(),
            "idempotency_key": fields.get("idempotency_key") or self._idem(),
            "app_id": fields.get("app_id", ""),
            "host_id": "",
            "task_id": fields.get("task_id", ""),
            "run_id": fields.get("run_id", ""),
            "attempt": fields.get("attempt") or 0,
            "revision": fields.get("revision") or 0,
            "stage": fields.get("stage", ""),
            "state": fields.get("state", ""),
            "candidate_id": fields.get("candidate_id", ""),
            "evidence_ref": fields.get("evidence_ref", ""),
            "origin": "control_scheduler",
            "github_execution": False,
            "macci_proof": False,
        }
        result = self.events.append(body)
        key = body["idempotency_key"]
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO applied_events(idempotency_key) VALUES(?)", (key,))
        if isinstance(result, dict) and result.get("producer_seq") is None and "producer_seq" in result:
            return

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
