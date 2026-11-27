"""Isolated W04 scheduler counterexamples.

The auth and event objects in this file are test doubles. They are not W02,
not W01, not GitHub, and not MacCI. A passing test does not prove a production
lease, a GitHub write, or a MacCI receipt.

W01 ``33e65dc0925f03e60518a5c91a0e2254fd0fd12f`` and W02
``5dd0eb38e175a8a905836fbdbac5633ff9eb19ae`` are not in this tree. A double
that signs a finished document is not that joint adapter.
"""

import importlib.util
import json
import secrets
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.control_scheduler import GITHUB_EXECUTION, JOINT_INTEGRATION_READY, MACCI_PROOF, PRODUCTION_WIRED, W01_HEAD, W02_HEAD
from app.control_scheduler.candidate import candidate_id
from app.control_scheduler.errors import SchedulerError
from app.control_scheduler.scheduler import Scheduler

HEAD = "a" * 40
BASE = "b" * 40
MERGE = "c" * 40
TREE = "d" * 40
HEAD_B = "e" * 40
MERGE_B = "f" * 40
TREE_B = "1" * 40
BASELINE = "2" * 40
MAIN = "3" * 40
PREVIEW = "4" * 40
REPO = "layho/web-app-init-base-env"


class PortError(Exception):
    def __init__(self, code, status, detail, extra=None):
        super().__init__(detail)
        self.code = code
        self.status = status
        self.extra = {"changed": False, **(extra or {})}


class AuthResult:
    def __init__(self, body):
        self.status = 200
        self.body = body


class Clock:
    def __init__(self, value=1_700_000_000):
        self.value = value

    def __call__(self):
        return self.value


class AuthDouble:
    """Stand-in for ControlIdentityService. Not a lease implementation to ship.

    ``sign`` accepts only a finished business document. Replay ignores the
    proof nonce, which is what W02's payload hash does. This object is still
    not W02.
    """

    github_execution = False
    macci_proof = False

    def __init__(self, clock, path=None):
        self.clock = clock
        self.path = Path(path) if path else None
        self.runs = {}
        self.idem = {}
        self.candidates = {}
        self.calls = []
        self.signed = []
        self.facts = []
        self.bindings = {}
        self.grants = []
        self.ancestry = {}
        self.fail_code = None
        self.drop_after_commit = False
        self.ambiguous_replay = False
        self.accept_kind = "user_acceptance"
        self.fence_calls = []
        self.classify_calls = []
        self._load()

    def _load(self):
        if self.path is None or not self.path.exists():
            return
        saved = json.loads(self.path.read_text())
        self.runs = saved["runs"]
        self.idem = {tuple(json.loads(key)): value for key, value in saved["idem"].items()}
        self.candidates = saved["candidates"]
        self.facts = saved["facts"]
        self.bindings = saved["bindings"]
        self.grants = saved["grants"]
        self.ancestry = {tuple(json.loads(key)): value for key, value in saved["ancestry"].items()}
        self.ambiguous_replay = saved["ambiguous_replay"]
        self.accept_kind = saved["accept_kind"]

    def _save(self):
        if self.path is None:
            return
        payload = {
            "runs": self.runs,
            "idem": {json.dumps(list(key)): value for key, value in self.idem.items()},
            "candidates": self.candidates,
            "facts": self.facts,
            "bindings": self.bindings,
            "grants": self.grants,
            "ancestry": {json.dumps(list(key)): value for key, value in self.ancestry.items()},
            "ambiguous_replay": self.ambiguous_replay,
            "accept_kind": self.accept_kind,
        }
        self.path.write_text(json.dumps(payload))

    def register_candidate(self, *, repo, task, head, base, merge, tree):
        issued = candidate_id(repo, task, head, base, merge, tree)
        self.candidates[issued] = {"repo": repo, "task": task, "head": head, "base": base, "merge": merge, "tree": tree}
        self._save()
        return issued

    def sign(self, operation, normalized):
        if not isinstance(normalized, dict) or "proof" in normalized or "actor" in normalized:
            raise PortError("SCHEMA_UNSUPPORTED", 422, "request was signed before it was normalized")
        self.signed.append({"operation": operation, "presented": json.loads(json.dumps(normalized))})
        signed = dict(normalized)
        signed["actor"] = {"id": "executor-double", "kind": "executor"}
        signed["proof"] = {"double": True, "nonce": secrets.token_hex(8), "alg": "double"}
        return signed

    def note_record(self, kind, **fields):
        fact = {"kind": kind, **fields}
        self.facts = [
            item for item in self.facts
            if not (
                item.get("kind") == kind
                and item.get("ref") == fact.get("ref")
                and item.get("candidate_id") == fact.get("candidate_id")
                and item.get("subject") == fact.get("subject")
            )
        ]
        self.facts.append(fact)
        self._save()

    def note_ancestry(self, *, main_sha, preview_sha, observed_tree, candidate_tree, is_ancestor):
        self.ancestry[(main_sha, preview_sha, observed_tree, candidate_tree)] = {
            "is_ancestor": bool(is_ancestor),
            "main_sha": main_sha,
            "preview_sha": preview_sha,
            "observed_tree": observed_tree,
            "candidate_tree": candidate_tree,
            "source": "server-observation",
        }
        self._save()

    def note_grant(self, action, subject):
        self.grants.append([action, subject])
        self._save()

    def bind_subject(self, subject_id, **state):
        self.bindings[subject_id] = dict(state)
        self._save()

    def observe_ancestry(self, *, main_sha, preview_sha, observed_tree, candidate_tree):
        found = self.ancestry.get((main_sha, preview_sha, observed_tree, candidate_tree))
        if found is None:
            raise PortError("EVIDENCE_MISSING", 409, "ancestry")
        return dict(found)

    def verified_record(self, kind, **binding):
        rows = [fact for fact in self.facts if fact.get("kind") == kind]
        if binding.get("candidate_id") and kind == "acceptance":
            rejected = any(
                fact.get("candidate_id") == binding["candidate_id"] and fact.get("outcome") == "rejected"
                for fact in rows
            )
            if rejected:
                raise PortError("NOT_AUTHORIZED", 403, "acceptance was rejected")
        matched = []
        for fact in rows:
            if all(fact.get(key) == value for key, value in binding.items() if value is not None):
                matched.append(fact)
        if binding.get("ref"):
            by_ref = [fact for fact in rows if fact.get("ref") == binding["ref"]]
            if by_ref and not matched:
                raise PortError("EVIDENCE_INCONSISTENT", 409, "record is bound to a different candidate")
        if not matched:
            raise PortError("EVIDENCE_MISSING", 409, "server record missing")
        fact = dict(matched[-1])
        if kind == "acceptance" and fact.get("signer_kind") != "user_acceptance":
            raise PortError("NOT_AUTHORIZED", 403, "signer")
        if kind == "test" and fact.get("signer_kind") != "macci":
            raise PortError("NOT_AUTHORIZED", 403, "test signer")
        return fact

    def reject_acceptance(self, **binding):
        if self.accept_kind != "user_acceptance":
            raise PortError("NOT_AUTHORIZED", 403, "signer")
        fact = {"kind": "acceptance", "outcome": "rejected", "signer_kind": self.accept_kind, **binding}
        self.facts.append(fact)
        self._save()
        return dict(fact)

    def execute(self, operation, body, *, route_class):
        self.calls.append({"operation": operation, "route_class": route_class, "body": json.loads(json.dumps(body))})
        if route_class != "isolated-scheduler":
            raise PortError("NOT_AUTHORIZED", 403, "route is not the isolated scheduler")
        if body.get("schema_version") != 2 or "received_at" in body or "occurred_at" in body or "ttl" in body:
            raise PortError("SCHEMA_UNSUPPORTED", 422, "caller tried to own server fields")
        if body.get("state") == "UNKNOWN" or body.get("result") == "UNKNOWN":
            raise PortError("NOT_AUTHORIZED", 403, "caller cannot write UNKNOWN")
        if self.fail_code:
            code = self.fail_code
            self.fail_code = None
            raise PortError(code, 403, code)
        proof = body.get("proof")
        if not isinstance(proof, dict) or proof.get("double") is not True:
            raise PortError("PROOF_INVALID", 403, "proof")
        if operation == "policy.evaluate":
            return self._policy(body)
        if not body.get("idempotency_key"):
            raise PortError("SCHEMA_UNSUPPORTED", 422, "idempotency")
        digest = self._business(body)
        key = ("executor-double", operation, body.get("idempotency_key"))
        existing = self.idem.get(key)
        if existing:
            if existing["digest"] != digest:
                raise PortError("IDEMPOTENCY_PAYLOAD_MISMATCH", 409, "payload")
            if self.ambiguous_replay:
                self._save()
                return AuthResult({"state": "LEASED", "run_id": existing["run_id"]})
            current = self.runs[existing["run_id"]]["state"]
            self._save()
            return AuthResult({
                "replayed": True, "lease_token": None, "original_result": dict(existing["public"]),
                "current_state": current, "github_execution": False, "macci_proof": False,
            })
        if operation == "run.claim":
            public, token = self._claim(body)
        elif operation == "run.heartbeat":
            public, token = self._touch(body, rotate=False)
        elif operation == "run.renew":
            public, token = self._touch(body, rotate=True)
        elif operation == "run.start":
            public, token = self._move(body, source={"LEASED"}, target="RUNNING")
        elif operation == "run.complete":
            if body.get("result") not in {"SUCCEEDED", "FAILED"}:
                raise PortError("SCHEMA_UNSUPPORTED", 422, "result")
            public, token = self._move(body, source={"LEASED", "RUNNING"}, target=body["result"])
        else:
            raise PortError("NOT_AUTHORIZED", 403, "operation")
        self.idem[key] = {"digest": digest, "public": dict(public), "run_id": public["run_id"]}
        self._save()
        if self.drop_after_commit:
            self.drop_after_commit = False
            self._save()
            raise OSError("lost response")
        return AuthResult({**public, "lease_token": token, "replayed": False, "github_execution": False, "macci_proof": False})

    def _business(self, body):
        visible = {key: value for key, value in body.items() if key not in {"proof", "actor"}}
        return json.dumps(visible, sort_keys=True, separators=(",", ":"))

    def _policy(self, body):
        subject = body.get("object_id")
        binding = self.bindings.get(subject)
        if not isinstance(binding, dict) or binding.get("action") != body.get("action"):
            raise PortError("EVIDENCE_MISSING", 409, "subject binding")
        action = binding["action"]
        if binding.get("version_state") == "FROZEN" and [action, subject] not in self.grants:
            return AuthResult({
                "allow": False, "code": "FREEZE_ACTIVE", "changed": False, "replayed": False, "production_enabled": False,
            })
        return AuthResult({
            "allow": True, "code": "OK", "changed": True, "replayed": False, "production_enabled": False,
            "github_execution": False, "macci_proof": False,
        })

    def trusted_fence(self, run_id, *, fenced, effect_known):
        self.fence_calls.append({"run_id": run_id, "fenced": fenced, "effect_known": effect_known})
        run = self.runs.get(run_id)
        if not fenced or effect_known or run is None or run["state"] not in {"LEASED", "RUNNING"}:
            state = None if run is None else run["state"]
            raise PortError("EVIDENCE_MISSING", 409, "unknown requires fencing", extra={"state": state})
        run["state"] = "UNKNOWN"
        run["token"] = None
        return {"state": "UNKNOWN", "changed": True, "github_fencing": False, "code": "SIDE_EFFECT_UNKNOWN"}

    def trusted_classify(self, run_id, *, fenced, classification):
        self.classify_calls.append(classification)
        mapped = {"succeeded": "SUCCEEDED", "failed": "FAILED", "not_occurred": "RETRY_WAIT"}
        run = self.runs.get(run_id)
        if not fenced or run is None or run["state"] != "UNKNOWN" or classification not in mapped:
            raise PortError("EVIDENCE_MISSING", 409, "classification")
        run["state"] = mapped[classification]
        return {"state": run["state"], "changed": True, "github_fencing": False}

    def _claim(self, body):
        if body.get("run_id"):
            raise PortError("SCHEMA_UNSUPPORTED", 422, "client run id")
        if body.get("recovery") == "timeout":
            raise PortError("RECLAIM_DENIED", 409, "timeout does not reclaim")
        subject = (body.get("app_id"), body.get("task_id"), body.get("phase"), body.get("candidate_id") or "")
        active = [run for run in self.runs.values() if run["subject"] == subject and run["state"] not in {"SUCCEEDED", "FAILED", "SUPERSEDED", "CANCELLED"}]
        if any(run["state"] == "UNKNOWN" for run in active):
            raise PortError("RECLAIM_DENIED", 409, "unknown blocks claim", extra={"state": "UNKNOWN"})
        for run in active:
            if run["state"] == "RETRY_WAIT":
                run["state"] = "SUPERSEDED"
                run["token"] = None
        active = [run for run in active if run["state"] not in {"SUCCEEDED", "FAILED", "SUPERSEDED", "CANCELLED"}]
        if active:
            raise PortError("RECLAIM_DENIED", 409, "active lease", extra={"state": active[0]["state"]})
        if body.get("expected_revision") != 0:
            raise PortError("CONFLICT_REVISION", 409, "revision")
        attempt = 1 + max((run["attempt"] for run in self.runs.values() if run["subject"] == subject), default=0)
        run_id = "run_" + secrets.token_hex(8)
        token = secrets.token_urlsafe(18)
        self.runs[run_id] = {
            "state": "LEASED", "generation": 1, "revision": 1, "token": token, "expires": self.clock() + 60,
            "subject": subject, "attempt": attempt, "phase": body.get("phase"),
        }
        public = {"state": "LEASED", "run_id": run_id, "attempt": attempt, "lease_generation": 1, "revision": 1}
        return public, token

    def _bound(self, body):
        run = self.runs.get(body.get("run_id"))
        generation = body.get("lease_generation")
        token = body.get("lease_token")
        if run is None or token in (None, "") or str(token) == str(generation):
            raise PortError("LEASE_FENCE_REJECTED", 409, "token", extra={"state": None if run is None else run["state"]})
        if generation != run["generation"] or token != run["token"] or self.clock() >= run["expires"]:
            raise PortError("LEASE_FENCE_REJECTED", 409, "fence", extra={"state": run["state"]})
        if body.get("expected_revision") != run["revision"]:
            raise PortError("CONFLICT_REVISION", 409, "revision", extra={"state": run["state"]})
        return run

    def _touch(self, body, *, rotate):
        run = self._bound(body)
        if run["state"] not in {"LEASED", "RUNNING"}:
            raise PortError("CONFLICT_REVISION", 409, "state", extra={"state": run["state"]})
        if rotate:
            # Jump by more than one so a scheduler that increments locally cannot match.
            run["generation"] += 3
            run["token"] = secrets.token_urlsafe(18)
        run["expires"] = self.clock() + 60
        run["revision"] += 1
        public = {
            "state": run["state"], "run_id": body["run_id"], "attempt": run["attempt"],
            "lease_generation": run["generation"], "revision": run["revision"],
        }
        return public, run["token"] if rotate else None

    def _move(self, body, *, source, target):
        run = self._bound(body)
        if run["state"] not in source:
            raise PortError("CONFLICT_REVISION", 409, "state", extra={"state": run["state"]})
        run["state"] = target
        run["revision"] += 1
        public = {
            "state": target, "run_id": body["run_id"], "attempt": run["attempt"],
            "lease_generation": run["generation"], "revision": run["revision"],
        }
        return public, None


class EventDouble:
    """Stand-in for Engine.append and Engine.query. It does not store W01's outbox.

    The scheduler supplies ``producer_seq`` and a stable ``event_id``. This
    double checks that proof was attached after that document existed, then
    replays the same business document under a new nonce. It is not W01.
    """

    github_execution = False
    macci_proof = False

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.events = []
        self.hold = set()
        self.idem = {}
        self.presented = []
        self.reads = []
        self.reject_next = False
        self.fail_before_store = False
        self._load()

    def _load(self):
        if self.path is None or not self.path.exists():
            return
        saved = json.loads(self.path.read_text())
        self.events = saved["events"]
        self.idem = saved["idem"]

    def _save(self):
        if self.path is None:
            return
        self.path.write_text(json.dumps({"events": self.events, "idem": self.idem}))

    def sign(self, normalized):
        if not isinstance(normalized, dict) or "proof" in normalized or "actor" in normalized:
            raise PortError("SCHEMA_UNSUPPORTED", 422, "event was signed before it was normalized")
        self.presented.append(json.loads(json.dumps(normalized, default=str)))
        signed = dict(normalized)
        signed["actor"] = {"id": "w04-scheduler", "kind": "executor"}
        signed["proof"] = {"double": True, "nonce": secrets.token_hex(8)}
        return signed

    def append(self, body, crash=None):
        if self.fail_before_store:
            self.fail_before_store = False
            raise OSError("event delivery lost")
        if "received_at" in body or body.get("state") == "UNKNOWN":
            raise PortError("NOT_AUTHORIZED", 403, "caller event rejected")
        if type(body.get("producer_seq")) is not int or isinstance(body.get("producer_seq"), bool):
            raise PortError("SCHEMA_UNSUPPORTED", 422, "producer_seq is assigned with the event intent")
        if not body.get("event_id") or not body.get("idempotency_key"):
            raise PortError("SCHEMA_UNSUPPORTED", 422, "event identity")
        proof = body.get("proof")
        if not isinstance(proof, dict) or proof.get("double") is not True:
            raise PortError("PROOF_INVALID", 403, "proof")
        if self.reject_next:
            self.reject_next = False
            return {"http": 409, "code": "EVIDENCE_MISSING", "applied": False, "replayed": False, "producer_seq": None}
        digest = json.dumps({key: value for key, value in body.items() if key not in {"proof", "actor"}}, sort_keys=True, default=str)
        existing = self.idem.get(body["idempotency_key"])
        if existing:
            if existing["digest"] != digest:
                return {"http": 409, "code": "IDEMPOTENCY_PAYLOAD_MISMATCH", "applied": False, "replayed": False}
            stored = existing["event"]
            return {
                "http": 200, "applied": True, "replayed": True, "event_id": stored["event_id"],
                "producer_seq": stored["producer_seq"], "trusted_source": stored["trusted_source"],
                "github_execution": False, "macci_proof": False,
            }
        stored = {key: value for key, value in body.items() if key not in {"proof", "actor"}}
        stored["trusted_source"] = "agent"
        stored["assigned_by"] = "event_port"
        self.events.append(stored)
        self.idem[body["idempotency_key"]] = {"digest": digest, "event": stored}
        self._save()
        return {
            "http": 200, "accepted": True, "applied": True, "replayed": False,
            "producer_seq": stored["producer_seq"], "event_id": stored["event_id"],
            "trusted_source": "agent", "assigned_by": "event_port", "storage": "test-double",
            "github_execution": False, "macci_proof": False,
        }

    def query(self, *, request, stream_id, stream_epoch, after_seq=0, task_id=None, limit=100):
        proof = request.get("proof") if isinstance(request, dict) else None
        if not isinstance(proof, dict) or proof.get("double") is not True:
            raise PortError("PROOF_INVALID", 403, "read proof")
        if request.get("stream_id") != stream_id or request.get("after_seq") != after_seq:
            raise PortError("EVIDENCE_INCONSISTENT", 409, "cursor")
        self.reads.append(after_seq)
        items = [
            dict(event) for event in self.events
            if event["stream_id"] == stream_id and event["stream_epoch"] == stream_epoch
            and event["producer_seq"] > after_seq and event["producer_seq"] not in self.hold
        ]
        items.sort(key=lambda event: event["producer_seq"])
        truncated = len(items) > limit
        page = items[:limit]
        return {
            "items": page, "truncated": truncated,
            "next_after_seq": page[-1]["producer_seq"] if truncated and page else None,
            "side_effect": False, "gap": False, "github_execution": False, "macci_proof": False,
        }

    def inject(self, body):
        self.events.append(dict(body))
        self._save()


@pytest.fixture
def world(tmp_path):
    tmp_path.chmod(0o700)
    clock = Clock()
    auth = AuthDouble(clock)
    events = EventDouble()
    scheduler = Scheduler.open(tmp_path, auth, events, clock=clock, repo_id=REPO)
    return scheduler, auth, events, clock, tmp_path


def caller(**extra):
    """Holder presentation. Proof and role strings here are not the signature."""

    body = {"presented_by": "holder"}
    body.update(extra)
    return body


def external_event(seq, **extra):
    item = {
        "repo_id": REPO,
        "app_id": "app-1",
        "stream_id": "remote:candidate",
        "stream_epoch": 1,
        "producer_seq": seq,
        "event_id": extra.pop("event_id", f"evt-ext-{seq}"),
        "event_type": extra.pop("event_type", "run.observed"),
        "trusted_source": extra.pop("trusted_source", "macci"),
        "idempotency_key": extra.pop("idempotency_key", f"idem-ext-{seq}"),
        "state": extra.pop("state", "FAILED"),
        "stage": extra.pop("stage", "test"),
        "origin": extra.pop("origin", "remote"),
    }
    item.update(extra)
    return item


def note_phase_fact(scheduler, phase, chain):
    kind = {"test": "test", "accept": "acceptance", "publish": "publish"}[phase]
    signer = {"test": "macci", "accept": "user_acceptance", "publish": "release"}[phase]
    outcome = "passed" if phase == "test" else "accepted"
    scheduler.auth.note_record(
        kind, candidate_id=chain["candidate_id"], tree=chain["tree"], head=chain["head"], base=chain["base"],
        merge=chain["merge"], ref=f"evid-{phase}-{chain['chain_id']}", signer_kind=signer, outcome=outcome,
        subject=chain["chain_id"],
    )


def boot(scheduler, version="0.6.0", task="task-1"):
    scheduler.open_version(version, baseline=BASELINE)
    scheduler.open_task(task_id=task, version=version, app_id="app-1", worktree="wt/task-1")


def register(scheduler, chain="chain-a", files=None, deps=(), priority=0, pr="pr-a", head=HEAD, base=BASE, merge=MERGE, tree=TREE, task="task-1"):
    return scheduler.register_chain(
        chain_id=chain, task_id=task, pr_line=pr, files=files or ["app/core"], dependencies=deps,
        priority=priority, worktree="wt/task-1", head=head, base=base, merge=merge, tree=tree,
    )


def run_phase(scheduler, version, phase, chain_id=None):
    claimed = scheduler.claim_next(version=version, phase=phase, caller=caller(), chain_id=chain_id)
    assert claimed["claimed"] is True, claimed
    if phase in {"test", "accept", "publish"} and claimed.get("chain_id"):
        chain = next(item for item in scheduler.snapshot(version)["chains"] if item["chain_id"] == claimed["chain_id"])
        note_phase_fact(scheduler, phase, chain)
    holder = caller(lease_token=claimed["lease_token"], lease_generation=claimed["lease_generation"], expected_revision=claimed["revision"])
    started = scheduler.start(round_id=claimed["round_id"], caller=holder)
    finished = scheduler.complete(
        round_id=claimed["round_id"],
        caller=caller(lease_token=claimed["lease_token"], lease_generation=started["lease_generation"], expected_revision=started["revision"]),
        result="SUCCEEDED",
        evidence_ref=f"evid-{phase}-{claimed['chain_id'] or 'task'}",
    )
    return claimed, finished


def drive_to_integrating(scheduler, version, chain_id):
    for phase in ("develop", "review", "test"):
        run_phase(scheduler, version, phase, chain_id)
    advanced = scheduler.advance_serial(version)
    assert advanced["advanced"] is True
    assert advanced["chain_id"] == chain_id
    return advanced


def drive_to_published(scheduler, version, chain_id, head=HEAD):
    drive_to_integrating(scheduler, version, chain_id)
    run_phase(scheduler, version, "preview_merge", chain_id)
    observed = scheduler.observe_remote(chain_id, kind="exact", observation_seq=1, remote_sha=head, evidence_ref=f"evid-remote-{chain_id}")
    assert observed["state"] == "DEPLOYING"
    run_phase(scheduler, version, "deploy", chain_id)
    run_phase(scheduler, version, "accept", chain_id)
    run_phase(scheduler, version, "publish", chain_id)
    return scheduler.snapshot(version)


def test_candidate_encoding_matches_contract_vector():
    assert candidate_id(REPO, "tsk_example_w00", "a" * 40, "b" * 40, "c" * 40, "d" * 40) == (
        "cand_839071261f5ac4e98fd497f62f26c7be47d3221b6938eab31efeef0d6f031f08"
    )


def test_package_is_not_production_wired():
    for path in Path("app/control_scheduler").glob("*.py"):
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not (stripped.startswith("import ") or stripped.startswith("from ")):
                continue
            for banned in ("integration_queue", "preview_journal", "merge_preview", "control_routes", "app.main", "manage.sh", "app.operations", "app.auth", "control_identity", "control_events", "control_authz"):
                assert banned not in stripped
    assert PRODUCTION_WIRED is False
    assert JOINT_INTEGRATION_READY is False
    assert GITHUB_EXECUTION is False
    assert MACCI_PROOF is False
    assert W01_HEAD == "33e65dc0925f03e60518a5c91a0e2254fd0fd12f"
    assert W02_HEAD == "5dd0eb38e175a8a905836fbdbac5633ff9eb19ae"


def test_private_directory_required(tmp_path):
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError):
        Scheduler.open(tmp_path, AuthDouble(Clock()), EventDouble(), repo_id=REPO)


def test_f01_task_candidate_and_round_share_one_scheduler(world):
    scheduler, auth, events, _clock, _root = world
    boot(scheduler)
    register(scheduler)
    task_claim = scheduler.claim_next(version="0.6.0", phase="claim", caller=caller(task_id="task-1"))
    chain_claim = scheduler.claim_next(version="0.6.0", phase="develop", caller=caller())
    snap = scheduler.snapshot("0.6.0")
    assert task_claim["claimed"] and chain_claim["claimed"]
    assert {item["task_id"] for item in snap["tasks"]} == {"task-1"}
    assert {item["task_id"] for item in snap["chains"]} == {"task-1"}
    assert {item["task_id"] for item in snap["rounds"]} == {"task-1"}
    assert {item["phase"] for item in snap["rounds"]} == {"claim", "develop"}
    assert auth.github_execution is False
    assert events.github_execution is False
    assert all(event["state"] != "UNKNOWN" for event in events.events)


def test_s01_serial_integration_ignores_pinned_pr(world):
    scheduler, _auth, _events, _clock, _root = world
    boot(scheduler)
    register(scheduler, "chain-low", files=["app/low"], priority=1, pr="pr-196", head=HEAD, merge=MERGE, tree=TREE)
    register(scheduler, "chain-high", files=["app/high"], priority=40, pr="pr-2", head=HEAD_B, merge=MERGE_B, tree=TREE_B)
    for chain in ("chain-low", "chain-high"):
        for phase in ("develop", "review", "test"):
            run_phase(scheduler, "0.6.0", phase, chain)
    first = scheduler.advance_serial("0.6.0", pinned_pr="pr-196")
    second = scheduler.advance_serial("0.6.0", pinned_pr="pr-196")
    assert first["chain_id"] == "chain-high"
    assert first["pinned_pr_ignored"] is True
    assert first["remote_called"] is False
    assert second["advanced"] is False and second["reason"] == "serial_held"
    with pytest.raises(SchedulerError) as raised:
        scheduler.stage_prepare("chain-low")
    assert raised.value.code == "SCHEDULER_BYPASS_DENIED"


def test_s02_version_publish_is_more_than_integration_or_equal_trees(world):
    scheduler, _auth, _events, _clock, _root = world
    boot(scheduler)
    register(scheduler)
    drive_to_integrating(scheduler, "0.6.0", "chain-a")
    run_phase(scheduler, "0.6.0", "preview_merge", "chain-a")
    scheduler.observe_remote("chain-a", kind="exact", observation_seq=1, remote_sha=HEAD, evidence_ref="evid-remote-chain-a")
    with pytest.raises(SchedulerError) as blocked:
        scheduler.activate_version("0.7.0", baseline=BASELINE)
    assert blocked.value.code == "VERSION_NOT_RELEASED"
    scheduler.freeze_version("0.6.0", reason="acceptance freeze", evidence_ref="evid-freeze")
    scheduler.auth.note_ancestry(main_sha=MAIN, preview_sha=PREVIEW, observed_tree=TREE, candidate_tree=TREE, is_ancestor=False)
    scheduler.record_publish_observation("0.6.0", main_sha=MAIN, preview_sha=PREVIEW, observed_tree=TREE, candidate_tree=TREE)
    with pytest.raises(SchedulerError) as frozen:
        scheduler.publish_version(
            "0.6.0", observed_tree=TREE, candidate_tree=TREE, main_sha=MAIN, preview_sha=PREVIEW,
        )
    assert frozen.value.code == "FREEZE_ACTIVE"
    assert scheduler.snapshot("0.6.0")["version"]["state"] == "FROZEN"


def test_s03_pre_head_binding_and_queued_reservation(world):
    scheduler, auth, _events, _clock, _root = world
    boot(scheduler)
    bound = scheduler.register_chain(
        chain_id="early", task_id="task-1", pr_line="pr-early", files=["app/core"], worktree="wt/task-1",
    )
    assert bound["state"] == "UNBOUND"
    assert bound["candidate_id"] == ""
    waiting = scheduler.claim_next(version="0.6.0", phase="develop", caller=caller())
    assert waiting["claimed"] is False
    with pytest.raises(SchedulerError) as mismatch:
        scheduler.register_chain(chain_id="other-tree", task_id="task-1", pr_line="pr-other", files=["app/other"], worktree="wt/elsewhere")
    assert mismatch.value.code == "EVIDENCE_INCONSISTENT"
    overlap = register(scheduler, "overlap", files=["app/core/file.py"], pr="pr-overlap", head=HEAD_B, merge=MERGE_B, tree=TREE_B)
    assert overlap["queue"] == "WAITING_RESERVATION"
    assert auth.calls == [] or all(call["operation"] != "run.claim" for call in auth.calls)
    scheduler.bind_candidate("early", head=HEAD, base=BASE, merge=MERGE, tree=TREE)
    claimed = scheduler.claim_next(version="0.6.0", phase="develop", caller=caller(), chain_id="early")
    assert claimed["claimed"] is True
    assert claimed["candidate_id"].startswith("cand_")


def test_s04_spec_change_dependency_loss_and_candidate_replacement(world):
    scheduler, _auth, _events, _clock, _root = world
    boot(scheduler)
    register(scheduler, "upstream", files=["app/upstream"], pr="pr-up")
    downstream = register(scheduler, "downstream", files=["app/downstream"], deps=("upstream",), pr="pr-down", head=HEAD_B, merge=MERGE_B, tree=TREE_B)
    assert downstream["queue"] == "WAITING_DEPENDENCY"
    with pytest.raises(SchedulerError) as changed:
        register(scheduler, "upstream", files=["app/upstream/changed"])
    assert changed.value.code == "CONFLICT_REVISION"
    scheduler.observe_remote("upstream", kind="closed_unmerged", observation_seq=1, merged=False, closed=True)
    assert scheduler.snapshot("0.6.0")["chains"][0]["queue"] == "WAITING_DEPENDENCY" or any(
        chain["chain_id"] == "downstream" and chain["queue"] == "WAITING_DEPENDENCY" for chain in scheduler.snapshot("0.6.0")["chains"]
    )
    blocked = scheduler.claim_next(version="0.6.0", phase="develop", caller=caller(), chain_id="downstream")
    assert blocked["claimed"] is False


def test_s05_priority_claim_without_active_pr_bypass(world):
    scheduler, auth, _events, _clock, _root = world
    boot(scheduler)
    register(scheduler, "low", files=["app/low"], priority=1, pr="pr-196")
    register(scheduler, "high", files=["app/high"], priority=20, pr="pr-2", head=HEAD_B, merge=MERGE_B, tree=TREE_B)
    claimed = scheduler.claim_next(version="0.6.0", phase="develop", caller=caller(), pinned_pr="pr-196")
    assert claimed["chain_id"] == "high"
    assert claimed["pinned_pr_ignored"] is True
    assert sum(call["operation"] == "run.claim" for call in auth.calls) == 1


def test_s06_timeout_does_not_write_unknown_until_trusted_fence(world):
    scheduler, auth, events, clock, _root = world
    boot(scheduler)
    register(scheduler, "one", files=["app/one"])
    register(scheduler, "two", files=["app/two"], pr="pr-two", head=HEAD_B, merge=MERGE_B, tree=TREE_B)
    claimed, _finished = run_phase(scheduler, "0.6.0", "develop", "one")
    # A fresh running round is the one that can time out.
    running = scheduler.claim_next(version="0.6.0", phase="develop", caller=caller(), chain_id="two")
    started = scheduler.start(
        round_id=running["round_id"],
        caller=caller(lease_token=running["lease_token"], lease_generation=running["lease_generation"], expected_revision=running["revision"]),
    )
    clock.value += 10_000
    report = scheduler.report_caller_timeout(round_id=running["round_id"])
    assert report["unknown_written"] is False
    assert report["auth_called"] is False
    assert auth.runs[running["run_id"]]["state"] == "RUNNING"
    assert all(event.get("state") != "UNKNOWN" for event in events.events)
    auth.clock = Clock(clock.value)
    with pytest.raises(SchedulerError) as stale:
        scheduler.heartbeat(
            round_id=running["round_id"],
            caller=caller(lease_token=running["lease_token"], lease_generation=started["lease_generation"], expected_revision=started["revision"], ttl=99999, occurred_at=clock.value),
        )
    assert stale.value.code == "LEASE_FENCE_REJECTED"
    assert auth.runs[running["run_id"]]["state"] == "RUNNING"
    auth.clock = Clock(1_700_000_000)
    with pytest.raises(SchedulerError) as denied:
        scheduler.reconcile_timeout(run_id=running["run_id"], fenced=False, effect_known=False)
    assert denied.value.code == "EVIDENCE_MISSING"
    assert auth.runs[running["run_id"]]["state"] == "RUNNING"
    written = scheduler.reconcile_timeout(run_id=running["run_id"], fenced=True, effect_known=False)
    assert written["unknown_written"] is True
    assert written["event_unknown_appended"] is False
    assert written["github_fencing"] is False
    blocked = scheduler.claim_next(version="0.6.0", phase="review", caller=caller(), chain_id="one")
    assert blocked["reason"] == "version_blocked"
    classified = scheduler.classify_unknown(run_id=running["run_id"], fenced=True, classification="not_occurred", evidence_ref="evid-not-occurred")
    assert classified["state"] == "RETRY_WAIT"
    assert classified["resumed"] is False
    again = scheduler.claim_next(version="0.6.0", phase="develop", caller=caller(), chain_id="two")
    assert again["claimed"] is False and again["reason"] == "human_required"
    with pytest.raises(SchedulerError):
        scheduler.start(round_id=running["round_id"], caller=caller(lease_token="stale-token", lease_generation=1, expected_revision=1))
    auth.note_record("intervention", subject=running["round_id"], ref="evid-retry", permission="retry_phase")
    retried = scheduler.intervene(action="retry_phase", subject_id=running["round_id"], evidence_ref="evid-retry", caller=caller())
    assert retried["claimed"] is True
    assert retried["run_id"] != running["run_id"]
    assert retried["resumed_same_run"] is False
    assert auth.runs[running["run_id"]]["state"] == "SUPERSEDED"


def test_s07_remote_unknown_mismatch_and_closed_unmerged(world):
    scheduler, _auth, _events, _clock, _root = world
    boot(scheduler)
    register(scheduler, "push", files=["app/push"])
    register(scheduler, "held", files=["app/push/file.py"], pr="pr-held", head=HEAD_B, merge=MERGE_B, tree=TREE_B)
    drive_to_integrating(scheduler, "0.6.0", "push")
    run_phase(scheduler, "0.6.0", "preview_merge", "push")
    early = scheduler.observe_remote("push", kind="exact", observation_seq=2, remote_sha=HEAD, evidence_ref="evid-late")
    assert early["reason"] == "out_of_order"
    assert early["state"] != "DEPLOYING"
    lost = scheduler.observe_remote("push", kind="lost_response", observation_seq=1)
    assert lost["remote_status"] == "UNKNOWN_REMOTE"
    assert scheduler.snapshot("0.6.0")["chains"][0]["state"] != "DEPLOYING" or any(
        chain["chain_id"] == "push" and chain["state"] == "INTEGRATING" for chain in scheduler.snapshot("0.6.0")["chains"]
    )
    drained = scheduler.snapshot("0.6.0")
    push = next(chain for chain in drained["chains"] if chain["chain_id"] == "push")
    assert push["state"] == "INTEGRATING"
    assert push["remote_status"] == "UNKNOWN_REMOTE"
    with pytest.raises(SchedulerError) as retry:
        scheduler.retry_remote("push")
    assert retry.value.code == "BLIND_RETRY_DENIED"
    mismatch = scheduler.observe_remote("held", kind="sha_mismatch", observation_seq=1, remote_sha=HEAD)
    assert mismatch["remote_status"] == "SHA_MISMATCH"
    assert mismatch["state"] != "DEPLOYING"
    released = scheduler.observe_remote("held", kind="closed_unmerged", observation_seq=2, merged=False, closed=True)
    assert released["reason"] == "released"
    protected = register(scheduler, "merged", files=["app/merged"], pr="pr-merged", head="9" * 40, merge="8" * 40, tree="7" * 40)
    kept = scheduler.observe_remote("merged", kind="merged_different", observation_seq=1, remote_sha=HEAD, merged=True, closed=True)
    assert kept["reason"] == "pending"
    assert protected["created"] is True
    assert next(chain["state"] for chain in scheduler.snapshot("0.6.0")["chains"] if chain["chain_id"] == "merged") != "SUPERSEDED"


def test_s08_human_intervention_and_failed_chain(world):
    scheduler, auth, _events, _clock, _root = world
    boot(scheduler)
    register(scheduler)
    for phase in ("develop", "review"):
        run_phase(scheduler, "0.6.0", phase, "chain-a")
    claimed = scheduler.claim_next(version="0.6.0", phase="test", caller=caller())
    started = scheduler.start(
        round_id=claimed["round_id"],
        caller=caller(lease_token=claimed["lease_token"], lease_generation=claimed["lease_generation"], expected_revision=claimed["revision"]),
    )
    failed = scheduler.complete(
        round_id=claimed["round_id"],
        caller=caller(lease_token=claimed["lease_token"], lease_generation=started["lease_generation"], expected_revision=started["revision"]),
        result="FAILED",
        evidence_ref="evid-fail",
    )
    assert failed["state"] == "FAILED"
    assert next(chain["state"] for chain in scheduler.snapshot("0.6.0")["chains"] if chain["chain_id"] == "chain-a") == "FAILED"
    with pytest.raises(SchedulerError) as hostops:
        scheduler.intervene(action="hostops_execute", subject_id="chain-a", evidence_ref="evid-hostops")
    assert hostops.value.code == "NOT_AN_EXECUTOR"
    with pytest.raises(SchedulerError) as resumed:
        scheduler.intervene(action="retry_phase", subject_id=claimed["round_id"], evidence_ref="evid-retry", caller=caller())
    assert resumed.value.code == "CONFLICT_REVISION"
    assert auth.runs[claimed["run_id"]]["state"] == "FAILED"
    auth.note_record("intervention", subject="chain-a", ref="evid-supersede", permission="supersede_failed")
    superseded = scheduler.intervene(action="supersede_failed", subject_id="chain-a", evidence_ref="evid-supersede")
    assert superseded["evidence_copied"] is False
    assert superseded["state"] == "SUPERSEDED"


def test_concurrency_allows_one_claim(world):
    scheduler, auth, _events, _clock, _root = world
    boot(scheduler)
    register(scheduler)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _index: scheduler.claim_next(version="0.6.0", phase="develop", caller=caller()), range(4)))
    assert sum(item["claimed"] for item in results) == 1
    assert sum(call["operation"] == "run.claim" for call in auth.calls) == 1


def test_duplicate_claim_does_not_mint_a_second_run(world):
    scheduler, auth, _events, _clock, _root = world
    boot(scheduler)
    register(scheduler)
    first = scheduler.claim_next(version="0.6.0", phase="develop", caller=caller(actor="executor-a"))
    second = scheduler.claim_next(version="0.6.0", phase="develop", caller=caller(actor="executor-b"))
    assert first["claimed"] is True and second["claimed"] is False
    assert sum(call["operation"] == "run.claim" for call in auth.calls) == 1
    assert len(auth.runs) == 1


def test_candidate_change_does_not_reuse_evidence(world):
    scheduler, _auth, _events, _clock, _root = world
    boot(scheduler)
    register(scheduler)
    for phase in ("develop", "review"):
        run_phase(scheduler, "0.6.0", phase, "chain-a")
    claimed = scheduler.claim_next(version="0.6.0", phase="test", caller=caller())
    scheduler.start(
        round_id=claimed["round_id"],
        caller=caller(lease_token=claimed["lease_token"], lease_generation=claimed["lease_generation"], expected_revision=claimed["revision"]),
    )
    replaced = scheduler.replace_candidate("chain-a", head=HEAD_B, base=BASE, merge=MERGE_B, tree=TREE_B, new_chain_id="chain-b", pr_line="pr-b")
    assert replaced["evidence_copied"] is False
    with pytest.raises(SchedulerError) as stale:
        scheduler.complete(
            round_id=claimed["round_id"],
            caller=caller(lease_token=claimed["lease_token"], lease_generation=claimed["lease_generation"], expected_revision=claimed["revision"] + 1),
            result="SUCCEEDED",
            evidence_ref="evid-old-test",
            evidence_candidate=replaced["candidate_id"],
        )
    assert stale.value.code == "CANDIDATE_STALE"
    snap = scheduler.snapshot("0.6.0")
    old = next(chain for chain in snap["chains"] if chain["chain_id"] == "chain-a")
    new = next(chain for chain in snap["chains"] if chain["chain_id"] == "chain-b")
    assert old["state"] == "SUPERSEDED"
    assert new["evidence"] == {}
    assert new["candidate_id"] != old["candidate_id"]


def test_restart_replays_claim_without_a_second_token(world):
    scheduler, auth, _events, _clock, root = world
    boot(scheduler)
    register(scheduler)
    with pytest.raises(SchedulerError) as crashed:
        scheduler.claim_next(version="0.6.0", phase="develop", caller=caller(), fault="after-auth-before-confirm")
    assert crashed.value.code == "CRASH_INJECTED"
    assert len(auth.runs) == 1
    token = next(iter(auth.runs.values()))["token"]
    assert token.encode() not in (root / "scheduler.sqlite3").read_bytes()
    restored = Scheduler.open(root, auth, EventDouble(), clock=Clock(), repo_id=REPO)
    report = restored.recover()
    assert report["tokens_stored"] is False
    assert report["restored"][0]["lease_token"] is None
    assert len(auth.runs) == 1
    assert report["restored"][0]["run_id"] == next(iter(auth.runs))
    snap = restored.snapshot("0.6.0")
    assert "lease_token" not in json.dumps(snap)
    assert snap["rounds"][0]["dispatch"] == "CONFIRMED"
    assert snap["rounds"][0]["run_id"] == report["restored"][0]["run_id"]


def test_out_of_order_events_do_not_publish_ahead_of_failure(world):
    scheduler, _auth, events, _clock, _root = world
    boot(scheduler)
    registered = register(scheduler)
    for phase in ("develop", "review"):
        run_phase(scheduler, "0.6.0", phase, "chain-a")
    claimed = scheduler.claim_next(version="0.6.0", phase="test", caller=caller())
    scheduler.start(
        round_id=claimed["round_id"],
        caller=caller(lease_token=claimed["lease_token"], lease_generation=claimed["lease_generation"], expected_revision=claimed["revision"]),
    )
    events.hold.add(1)
    events.inject(external_event(
        2, candidate_id=registered["candidate_id"], state="PUBLISHED", stage="publish", idempotency_key="idem-ext-2",
    ))
    gap = scheduler.ingest(stream_id="remote:candidate")
    assert gap["gap"] is True
    assert gap["applied"] == []
    assert next(chain["state"] for chain in scheduler.snapshot("0.6.0")["chains"] if chain["chain_id"] == "chain-a") == "TESTING"
    events.hold.clear()
    events.inject(external_event(
        1, candidate_id=registered["candidate_id"], state="FAILED", stage="test", idempotency_key="idem-ext-1",
        origin="control_scheduler",
    ))
    applied = scheduler.ingest(stream_id="remote:candidate")
    assert applied["applied"] == [1]
    assert applied["reason"] == "illegal"
    assert next(chain["state"] for chain in scheduler.snapshot("0.6.0")["chains"] if chain["chain_id"] == "chain-a") == "FAILED"


def test_close_requires_every_chain_and_user_acceptance(world):
    scheduler, _auth, _events, _clock, _root = world
    boot(scheduler)
    register(scheduler, "chain-a", files=["app/a"], pr="pr-a")
    register(scheduler, "chain-b", files=["app/b"], pr="pr-b", head=HEAD_B, merge=MERGE_B, tree=TREE_B)
    drive_to_published(scheduler, "0.6.0", "chain-a")
    with pytest.raises(SchedulerError) as partial:
        scheduler.close_task("task-1")
    assert partial.value.code == "EVIDENCE_MISSING"
    assert scheduler.snapshot("0.6.0")["tasks"][0]["closed"] is False
    scheduler.auth.note_record("task_acceptance", subject="task-1", ref="evid-task-accept", signer_kind="agent", outcome="accepted")
    with pytest.raises(SchedulerError) as signer:
        scheduler.record_task_acceptance("task-1", evidence_ref="evid-task-accept")
    assert signer.value.code == "NOT_AUTHORIZED"
    scheduler.auth.note_record("task_acceptance", subject="task-1", ref="evid-task-accept", signer_kind="user_acceptance", outcome="accepted")
    scheduler.record_task_acceptance("task-1", evidence_ref="evid-task-accept")
    with pytest.raises(SchedulerError):
        scheduler.close_task("task-1")
    drive_to_published(scheduler, "0.6.0", "chain-b", head=HEAD_B)
    closed = scheduler.close_task("task-1")
    assert closed["closed"] is True
    with pytest.raises(SchedulerError) as reopened:
        scheduler.close_task("task-1")
    assert reopened.value.code == "CONFLICT_REVISION"
    scheduler.auth.note_ancestry(main_sha=MAIN, preview_sha=PREVIEW, observed_tree=TREE, candidate_tree=TREE, is_ancestor=True)
    scheduler.record_publish_observation("0.6.0", main_sha=MAIN, preview_sha=PREVIEW, observed_tree=TREE, candidate_tree=TREE)
    published = scheduler.publish_version(
        "0.6.0", observed_tree=TREE, candidate_tree=TREE, main_sha=MAIN, preview_sha=PREVIEW,
    )
    assert published["state"] == "PUBLISHED"
    assert published["production_enabled"] is False
    activated = scheduler.activate_version("0.7.0", baseline="5" * 40)
    assert activated["state"] == "ACTIVE"


def test_renew_stores_auth_generation_and_rejects_the_old_token(world):
    scheduler, auth, _events, _clock, _root = world
    boot(scheduler)
    register(scheduler)
    claimed = scheduler.claim_next(version="0.6.0", phase="develop", caller=caller())
    renewed = scheduler.renew(
        round_id=claimed["round_id"],
        caller=caller(lease_token=claimed["lease_token"], lease_generation=claimed["lease_generation"], expected_revision=claimed["revision"]),
    )
    stored = scheduler.snapshot("0.6.0")["rounds"][0]["lease_generation"]
    assert stored == auth.runs[claimed["run_id"]]["generation"] == renewed["lease_generation"]
    assert stored != claimed["lease_generation"] + 1
    with pytest.raises(SchedulerError) as stale:
        scheduler.heartbeat(
            round_id=claimed["round_id"],
            caller=caller(lease_token=claimed["lease_token"], lease_generation=claimed["lease_generation"], expected_revision=renewed["revision"]),
        )
    assert stale.value.code == "LEASE_FENCE_REJECTED"
    assert auth.runs[claimed["run_id"]]["state"] == "LEASED"
    followed = scheduler.heartbeat(
        round_id=claimed["round_id"],
        caller=caller(lease_token=renewed["lease_token"], lease_generation=stored, expected_revision=renewed["revision"]),
    )
    assert followed["state"] == "LEASED"
    assert followed["lease_generation"] == stored


def test_idempotent_register_and_cycle(world):
    scheduler, _auth, _events, _clock, _root = world
    boot(scheduler)
    first = register(scheduler)
    second = register(scheduler)
    assert first["created"] is True and second["created"] is False
    register(scheduler, "left", files=["app/left"], deps=("right",), pr="pr-left", head=HEAD_B, merge=MERGE_B, tree=TREE_B)
    with pytest.raises(SchedulerError) as cycle:
        register(scheduler, "right", files=["app/right"], deps=("left",), pr="pr-right", head="6" * 40, merge="7" * 40, tree="8" * 40)
    assert cycle.value.code == "CONFLICT_REVISION"


def _intent_rows(root):
    with sqlite3.connect(root / "scheduler.sqlite3") as db:
        return db.execute("SELECT event_id, status, payload_json FROM event_intents ORDER BY producer_seq").fetchall()


def test_joint_packages_are_not_in_this_tree():
    assert importlib.util.find_spec("app.control_identity") is None
    assert importlib.util.find_spec("app.control_events") is None
    assert importlib.util.find_spec("app.control_authz") is None


def test_c1_sign_after_normalize_and_secret_free_intent(world):
    scheduler, auth, _events, _clock, root = world
    boot(scheduler)
    register(scheduler)
    claimed = scheduler.claim_next(
        version="0.6.0", phase="develop",
        caller={"proof": {"double": True, "nonce": "caller-nonce"}, "lease_token": "secret-token", "signer_kind": "user_acceptance"},
    )
    assert claimed["claimed"] is True
    presented = auth.signed[0]["presented"]
    assert "proof" not in presented and "actor" not in presented
    assert presented["idempotency_key"].startswith("idem-w04-")
    assert "secret-token" not in json.dumps(presented)
    assert "caller-nonce" not in json.dumps(presented)
    assert auth.calls[0]["body"]["proof"]["nonce"] != "caller-nonce"
    raw = (root / "scheduler.sqlite3").read_bytes()
    assert b"caller-nonce" not in raw
    assert b"secret-token" not in raw
    assert b"proof" not in raw
    assert b"lease_token" not in raw
    assert presented["idempotency_key"].encode() in raw


def test_c1_explicit_rejection_releases_and_unknown_holds(world):
    scheduler, auth, _events, _clock, root = world
    boot(scheduler)
    register(scheduler)
    auth.fail_code = "PROOF_INVALID"
    with pytest.raises(SchedulerError) as rejected:
        scheduler.claim_next(version="0.6.0", phase="develop", caller=caller())
    assert rejected.value.code == "PROOF_INVALID"
    released = scheduler.snapshot("0.6.0")
    assert released["rounds"][0]["dispatch"] == "ABORTED"
    assert next(item["queue"] for item in released["chains"] if item["chain_id"] == "chain-a") == "READY"
    again = scheduler.claim_next(version="0.6.0", phase="develop", caller=caller())
    assert again["claimed"] is True
    assert len(auth.runs) == 1

    auth.drop_after_commit = True
    register(scheduler, "other", files=["app/other"], pr="pr-other", head=HEAD_B, merge=MERGE_B, tree=TREE_B)
    with pytest.raises(SchedulerError) as lost:
        scheduler.claim_next(version="0.6.0", phase="develop", caller=caller(), chain_id="other")
    assert lost.value.code == "SIDE_EFFECT_UNKNOWN"
    held = scheduler.snapshot("0.6.0")
    other = next(item for item in held["rounds"] if item["chain_id"] == "other")
    assert other["dispatch"] == "DISPATCH_UNKNOWN"
    assert next(item["queue"] for item in held["chains"] if item["chain_id"] == "other") == "HELD"
    blocked = scheduler.claim_next(version="0.6.0", phase="develop", caller=caller(), chain_id="other")
    assert blocked["claimed"] is False
    assert len(auth.runs) == 2
    raw = (root / "scheduler.sqlite3").read_bytes()
    assert all(run["token"].encode() not in raw for run in auth.runs.values())
    auth.ambiguous_replay = True
    ambiguous = scheduler.recover()
    assert ambiguous["restored"] == []
    assert scheduler.snapshot("0.6.0")["rounds"]
    assert any(item["dispatch"] == "DISPATCH_UNKNOWN" for item in scheduler.snapshot("0.6.0")["rounds"])
    auth.ambiguous_replay = False
    recovered = scheduler.recover()
    assert recovered["restored"]
    assert recovered["restored"][0]["lease_token"] is None
    assert len(auth.runs) == 2
    assert all(item["dispatch"] != "DISPATCH_UNKNOWN" for item in scheduler.snapshot("0.6.0")["rounds"])


def test_c1_cross_process_recover_does_not_guess_success(tmp_path):
    tmp_path.chmod(0o700)
    auth = AuthDouble(Clock(), path=tmp_path / "auth-double.json")
    scheduler = Scheduler.open(tmp_path, auth, EventDouble(), clock=Clock(), repo_id=REPO)
    boot(scheduler)
    register(scheduler)
    auth.drop_after_commit = True
    with pytest.raises(SchedulerError):
        scheduler.claim_next(version="0.6.0", phase="develop", caller=caller())
    auth.ambiguous_replay = True
    auth._save()
    script = r"""
import json, sys
from pathlib import Path
from app.control_scheduler.scheduler import Scheduler
from tests.test_v060_w04_scheduler import AuthDouble, EventDouble, Clock, REPO
root = Path(sys.argv[1])
auth = AuthDouble(Clock(), path=root / "auth-double.json")
scheduler = Scheduler.open(root, auth, EventDouble(), clock=Clock(), repo_id=REPO)
report = scheduler.recover()
round_row = scheduler.snapshot("0.6.0")["rounds"][0]
print(json.dumps({
    "dispatch": round_row["dispatch"],
    "restored": len(report["restored"]),
    "token": None if not report["restored"] else report["restored"][0]["lease_token"],
    "runs": len(auth.runs),
}))
"""
    repo = Path(__file__).resolve().parents[1]

    def run_child():
        return subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            cwd=repo, check=False, capture_output=True, text=True,
        )

    first = run_child()
    assert first.returncode == 0, first.stderr
    guessed = json.loads(first.stdout)
    assert guessed == {"dispatch": "DISPATCH_UNKNOWN", "restored": 0, "token": None, "runs": 1}
    auth.ambiguous_replay = False
    auth._save()
    second = run_child()
    assert second.returncode == 0, second.stderr
    recovered = json.loads(second.stdout)
    assert recovered["dispatch"] == "CONFIRMED"
    assert recovered["restored"] == 1
    assert recovered["token"] is None
    assert recovered["runs"] == 1


def test_c2_delivery_reconciles_after_loss_and_stops_on_rejection(world):
    scheduler, _auth, events, _clock, root = world
    boot(scheduler)
    scheduler.register_chain(chain_id="early", task_id="task-1", pr_line="pr-early", files=["app/core"], worktree="wt/task-1")
    events.reject_next = True
    scheduler.bind_candidate("early", head=HEAD, base=BASE, merge=MERGE, tree=TREE)
    assert [row[1] for row in _intent_rows(root)] == ["REJECTED"]
    assert events.events == []
    events.fail_before_store = True
    scheduler.freeze_version("0.6.0", reason="hold", evidence_ref="evid-freeze")
    assert [row[1] for row in _intent_rows(root)] == ["REJECTED", "PENDING"]
    events.fail_before_store = False
    restored = Scheduler.open(root, scheduler.auth, events, clock=Clock(), repo_id=REPO)
    restored.recover()
    assert [row[1] for row in _intent_rows(root)] == ["REJECTED", "DELIVERED"]
    assert len(events.events) == 1
    assert events.events[0]["producer_seq"] == 1
    assert events.events[0]["trusted_source"] == "agent"
    assert "proof" not in events.events[0]
    first_id = events.events[0]["event_id"]
    restored.recover()
    assert len(events.events) == 1
    assert events.events[0]["event_id"] == first_id


def test_c2_ingest_pages_past_one_hundred_and_keeps_order(world):
    scheduler, _auth, events, _clock, root = world
    boot(scheduler)
    registered = register(scheduler)
    for seq in range(1, 106):
        events.inject(external_event(
            seq, event_type="diagnostic.observation", trusted_source="hostops_readonly",
            state="VISIBLE", stage="diagnose", candidate_id=registered["candidate_id"], origin="control_scheduler",
        ))
    first = scheduler.ingest(stream_id="remote:candidate", request={"after_seq": 0, "origin": "caller"})
    assert first["watermark"] == 105
    assert first["applied"] == list(range(1, 106))
    assert events.reads[:2] == [0, 100]
    restarted = Scheduler.open(root, scheduler.auth, events, clock=Clock(), repo_id=REPO)
    again = restarted.ingest(stream_id="remote:candidate", request={"after_seq": 0, "origin": "control_scheduler"})
    assert again["applied"] == []
    assert again["watermark"] == 105
    events.inject(external_event(107, event_type="diagnostic.observation", trusted_source="hostops_readonly", state="VISIBLE", stage="diagnose", candidate_id=registered["candidate_id"]))
    gap = restarted.ingest(stream_id="remote:candidate")
    assert gap["gap"] is True
    assert gap["watermark"] == 105
    events.inject(external_event(
        106, event_id="evt-ext-1", idempotency_key="idem-ext-1", event_type="diagnostic.observation",
        trusted_source="hostops_readonly", state="VISIBLE", stage="diagnose", candidate_id=registered["candidate_id"],
    ))
    events.inject(external_event(
        108, event_id="evt-different", idempotency_key="idem-ext-1", event_type="diagnostic.observation",
        trusted_source="hostops_readonly", state="VISIBLE", stage="diagnose", candidate_id=registered["candidate_id"],
    ))
    events.inject(external_event(109, trusted_source="", candidate_id=registered["candidate_id"]))
    paged = restarted.ingest(stream_id="remote:candidate")
    assert 106 in paged["skipped_duplicates"]
    assert 108 in paged["applied"]
    assert paged["reason"] == "rejected"
    assert paged["watermark"] == 108
    assert next(item["state"] for item in restarted.snapshot("0.6.0")["chains"] if item["chain_id"] == "chain-a") == "OPEN"


def test_c3_publish_binds_server_records_not_caller_strings(world):
    scheduler, auth, _events, _clock, root = world
    boot(scheduler)
    register(scheduler)
    drive_to_published(scheduler, "0.6.0", "chain-a")
    with sqlite3.connect(root / "scheduler.sqlite3") as db:
        raw = db.execute("SELECT evidence_json FROM chains WHERE chain_id='chain-a'").fetchone()[0]
        evidence = json.loads(raw)
        evidence["publish"]["tree"] = TREE_B
        db.execute("UPDATE chains SET evidence_json=? WHERE chain_id='chain-a'", (json.dumps(evidence),))
    auth.note_ancestry(main_sha=MAIN, preview_sha=PREVIEW, observed_tree=TREE, candidate_tree=TREE, is_ancestor=True)
    scheduler.record_publish_observation("0.6.0", main_sha=MAIN, preview_sha=PREVIEW, observed_tree=TREE, candidate_tree=TREE)
    with pytest.raises(SchedulerError) as tampered:
        scheduler.publish_version("0.6.0", observed_tree=TREE, candidate_tree=TREE, main_sha=MAIN, preview_sha=PREVIEW)
    assert tampered.value.code == "EVIDENCE_INCONSISTENT"
    assert scheduler.snapshot("0.6.0")["version"]["state"] == "ACTIVE"
    chain = next(item for item in scheduler.snapshot("0.6.0")["chains"] if item["chain_id"] == "chain-a")
    with sqlite3.connect(root / "scheduler.sqlite3") as db:
        raw = db.execute("SELECT evidence_json FROM chains WHERE chain_id='chain-a'").fetchone()[0]
        evidence = json.loads(raw)
        evidence["publish"]["tree"] = chain["tree"]
        db.execute("UPDATE chains SET evidence_json=? WHERE chain_id='chain-a'", (json.dumps(evidence),))
    for fact in auth.facts:
        if fact.get("kind") == "publish" and fact.get("ref") == "evid-publish-chain-a":
            fact["candidate_id"] = "cand_other"
    with pytest.raises(SchedulerError) as crossed:
        scheduler.publish_version("0.6.0", observed_tree=TREE, candidate_tree=TREE, main_sha=MAIN, preview_sha=PREVIEW)
    assert crossed.value.code == "EVIDENCE_INCONSISTENT"


def test_c3_forged_role_wrong_tree_rejected_acceptance_and_frozen_repair(world):
    scheduler, auth, events, _clock, _root = world
    boot(scheduler)
    register(scheduler, "chain-a", files=["app/a"], pr="pr-a")
    drive_to_integrating(scheduler, "0.6.0", "chain-a")
    run_phase(scheduler, "0.6.0", "preview_merge", "chain-a")
    scheduler.observe_remote("chain-a", kind="exact", observation_seq=1, remote_sha=HEAD, evidence_ref="evid-remote-chain-a")
    run_phase(scheduler, "0.6.0", "deploy", "chain-a")
    chain = next(item for item in scheduler.snapshot("0.6.0")["chains"] if item["chain_id"] == "chain-a")
    auth.note_record(
        "acceptance", candidate_id=chain["candidate_id"], tree=TREE_B, head=chain["head"], base=chain["base"],
        merge=chain["merge"], ref="evid-accept-chain-a", signer_kind="user_acceptance", outcome="accepted", subject="chain-a",
    )
    claimed = scheduler.claim_next(version="0.6.0", phase="accept", caller=caller(), chain_id="chain-a")
    started = scheduler.start(
        round_id=claimed["round_id"],
        caller=caller(lease_token=claimed["lease_token"], lease_generation=claimed["lease_generation"], expected_revision=claimed["revision"]),
    )
    with pytest.raises(SchedulerError) as wrong_tree:
        scheduler.complete(
            round_id=claimed["round_id"],
            caller=caller(lease_token=claimed["lease_token"], lease_generation=started["lease_generation"], expected_revision=started["revision"]),
            result="SUCCEEDED", evidence_ref="evid-accept-chain-a",
        )
    assert wrong_tree.value.code == "EVIDENCE_INCONSISTENT"
    auth.note_record(
        "acceptance", candidate_id=chain["candidate_id"], tree=chain["tree"], head=chain["head"], base=chain["base"],
        merge=chain["merge"], ref="evid-accept-chain-a", signer_kind="agent", outcome="accepted", subject="chain-a",
    )
    with pytest.raises(SchedulerError) as forged:
        scheduler.complete(
            round_id=claimed["round_id"],
            caller=caller(lease_token=claimed["lease_token"], lease_generation=started["lease_generation"], expected_revision=started["revision"], signer_kind="user_acceptance"),
            result="SUCCEEDED", evidence_ref="evid-accept-chain-a",
        )
    assert forged.value.code == "NOT_AUTHORIZED"
    auth.note_record(
        "acceptance", candidate_id=chain["candidate_id"], tree=chain["tree"], head=chain["head"], base=chain["base"],
        merge=chain["merge"], ref="evid-accept-chain-a", signer_kind="user_acceptance", outcome="rejected", subject="chain-a",
    )
    with pytest.raises(SchedulerError) as rejected:
        scheduler.complete(
            round_id=claimed["round_id"],
            caller=caller(lease_token=claimed["lease_token"], lease_generation=started["lease_generation"], expected_revision=started["revision"], signer_kind="user_acceptance"),
            result="SUCCEEDED", evidence_ref="evid-accept-chain-a",
        )
    assert rejected.value.code == "NOT_AUTHORIZED"
    failed = scheduler.complete(
        round_id=claimed["round_id"],
        caller=caller(lease_token=claimed["lease_token"], lease_generation=started["lease_generation"], expected_revision=started["revision"]),
        result="FAILED", evidence_ref="evid-reject-chain-a",
    )
    assert failed["state"] == "FAILED"
    register(scheduler, "repair", files=["app/repair"], pr="pr-repair", head=HEAD_B, merge=MERGE_B, tree=TREE_B)
    scheduler.freeze_version("0.6.0", reason="freeze", evidence_ref="evid-freeze-repair")
    auth.note_record("intervention", subject="chain-a", ref="evid-repair", permission="authorize_repair")
    with pytest.raises(SchedulerError) as frozen:
        scheduler.intervene(action="authorize_repair", subject_id="chain-a", successor_id="repair", evidence_ref="evid-repair")
    assert frozen.value.code == "FREEZE_ACTIVE"
    assert next(item["state"] for item in scheduler.snapshot("0.6.0")["chains"] if item["chain_id"] == "chain-a") == "REJECTED"
    auth.note_grant("authorize_repair", "chain-a")
    repaired = scheduler.intervene(action="authorize_repair", subject_id="chain-a", successor_id="repair", evidence_ref="evid-repair")
    assert repaired["state"] == "SUPERSEDED"
    assert repaired["evidence_copied"] is False
    assert repaired["production_enabled"] is False
    assert any(event["event_type"] == "intervention.recorded" and event["state"] == "AUTHORIZED" for event in events.events)
