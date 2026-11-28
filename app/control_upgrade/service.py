"""Upgrade checks and plans. Actions that would change an application stay denied."""

import hashlib
import json
from datetime import datetime

from app.control_upgrade.boundary import PRESERVE, allowlist_problem, case_collision, classify_path
from app.control_upgrade.candidate import candidate_id
from app.control_upgrade.conflicts import classify_text
from app.control_upgrade.flags import (
    ACCEPTANCE_ENABLED,
    MOUNTED_IN_APP_MAIN,
    PRODUCTION_DB_ACCESS,
    PRODUCTION_ENABLEMENT,
    PRODUCTION_VERIFICATION_ENABLED,
    REAL_DEPLOY,
    REAL_MERGE,
    REAL_UPGRADE,
)
from app.control_upgrade.store import Journal
from app.control_upgrade.versions import (
    VERSION_RE,
    cache_is_stale,
    identify_scheduler,
    sha_view,
    valid_sha,
)

SECRET_MARKERS = ("github_pat_", "ghp_", "BEGIN PRIVATE KEY", "AKIA", "postgres://")
READ_ACTION = "upgrade.read"
BLOCKING = {
    "app_data",
    "app_config",
    "app_custom",
    "path_rejected",
    "rename_or_symlink",
    "case_collision",
    "ambiguous_owner",
    "blanket_frontend_src_copy",
    "blanket_ownership",
    "migration_requires_review",
    "isolation_collision",
    "isolation_cross_app",
    "isolation_unproven",
    "build_failed_not_compatible",
    "build_evidence_missing",
    "macci_not_success",
    "version_mismatch",
    "ambiguous",
    "unknown_path",
    "previous_healthy_unproven",
    "server_manifest_missing",
    "manifest_candidate_mismatch",
    "manifest_source_missing",
    "evidence_unlabeled",
}

TUPLE_FIELDS = ("head", "base", "merge", "tree")
SOURCE_BY_KIND = {
    "core_session": "core_session",
    "app_service": "app_service",
    "agent": "agent",
    "macci": "macci",
    "publisher": "publisher",
    "host": "host_deployer",
    "hostops": "hostops_readonly",
    "user_acceptance": "user_acceptance",
    "candidate": "candidate",
    "policy_service": "policy_service",
}


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _usable(component):
    if component.get("sha") or component.get("running_sha"):
        return True
    return component.get("identified") is True or component.get("state") == "identified"


class UpgradeService:
    def __init__(self, root=None, *, clock, build, schedule, delivery, diagnostics, deploy, grants, principal=None, manifest=None):
        self.clock = clock
        self.build = build
        self.schedule = schedule
        self.delivery = delivery
        self.diagnostics = diagnostics
        self.deploy = deploy
        self.grants = list(grants or [])
        self.principal = principal
        self.manifest = manifest
        self.journal = Journal(root)
        self._sequence = 0

    def flags(self):
        return {
            "mounted": MOUNTED_IN_APP_MAIN,
            "production_enabled": PRODUCTION_ENABLEMENT,
            "production_verification_enabled": PRODUCTION_VERIFICATION_ENABLED,
            "production_db_access": PRODUCTION_DB_ACCESS,
            "real_merge": REAL_MERGE,
            "real_deploy": REAL_DEPLOY,
            "real_upgrade": REAL_UPGRADE,
            "acceptance_enabled": ACCEPTANCE_ENABLED,
            "upgrade_apply_enabled": False,
            "auto_resolve_ambiguous": False,
            "github_execution": False,
            "macci_proof": False,
            "joint_integration_ready": False,
            "overwrite_app_data": False,
            "overwrite_app_config": False,
            "overwrite_custom_code": False,
        }

    def enable_production(self):
        return self._disabled("production_enablement_disabled")

    def apply(self, request):
        return self._disabled("upgrade_action_disabled")

    def enroll(self, request):
        return self._disabled("scheduler_enrollment_disabled")

    def resolve(self, request):
        return self._disabled("ambiguous_conflict_not_resolved")

    def promote(self, request):
        return self._disabled("merge_disabled")

    def overwrite(self, request):
        return self._disabled("overwrite_disabled")

    def inspect(self, request):
        opened = self._open(request, READ_ACTION, write=False)
        if opened is not None:
            return opened
        diagnostic = self.diagnostics.read(app_id=request["app_id"])
        build = self.build.read(app_id=request["app_id"])
        claim = None
        if diagnostic.get("evidence_state") == "present" and isinstance(diagnostic.get("panel_pr_state"), str):
            claim = diagnostic["panel_pr_state"]
        paths = request.get("paths") if isinstance(request.get("paths"), list) else []
        return self._public({
            "http": 200,
            "code": None,
            "state": "INSPECTED",
            "message": "Read-only boundary inspection.",
            "panel_pr_state": "unproven",
            "diagnostic_claim": claim,
            "sandbox": False,
            "same_process_trusted_sdk": True,
            "modules_empty_is_acl_proof": False,
            "db_acl_effective": False,
            "isolation_proven": False,
            "port_claimed_db_acl": build.get("db_acl_effective") is True,
            "paths": [{"path": path, "classification": classify_path(path, [])} for path in paths],
            "journal_recorded": False,
        })

    def status(self, request):
        opened = self._open(request, READ_ACTION, write=False)
        if opened is not None:
            return opened
        fresh = self._fresh_components(request)
        cached = self.journal.get_cache(request["app_id"])
        components = {}
        for name, component in fresh.items():
            if _usable(component):
                chosen = dict(component)
                chosen["stale"] = False
            else:
                chosen = self._cached_component(cached, name)
                if chosen.get("source") != "cache":
                    chosen = dict(component)
                    chosen["stale"] = False
            components[name] = chosen
        repository_sha = components["repository"].get("sha")
        running_sha = components["running"].get("sha")
        components["running"]["matches_repository"] = bool(repository_sha and running_sha and repository_sha == running_sha)
        port_running = fresh["running"].get("sha")
        claim = request.get("running_sha") if "running_sha" in request else None
        return self._public({
            "http": 200,
            "code": None,
            "state": "STATUS",
            "message": "Component versions are separate observations.",
            "components": components,
            "caller_claim_ignored": "running_sha" in request and claim != port_running,
            "caller_declaration_ignored": request.get("declared") is not None,
            "collapsed": False,
            "journal_recorded": False,
        })

    def diagnose(self, request):
        opened = self._open(request, READ_ACTION, write=False)
        if opened is not None:
            return opened
        judged = self._judge(request)
        if judged.get("error") is not None:
            return judged["error"]
        body = dict(judged["body"])
        body["journal_recorded"] = False
        body["message"] = "Read-only diagnosis. Nothing was recorded."
        return self._public(body)

    def list_plans(self, request):
        opened = self._open(request, READ_ACTION, write=False)
        if opened is not None:
            return opened
        return {
            "app_id": request["app_id"],
            "plan_ids": [plan["plan_id"] for plan in self.journal.plans_for(request["app_id"])],
            "side_effect": False,
        }

    def plan(self, request):
        opened = self._open(request, "upgrade.plan", write=True)
        if opened is not None:
            return opened
        replay = self._replay(request, "plan")
        if replay is not None:
            return replay
        if request.get("expected_revision") != 1:
            return self._error(409, "CONFLICT_REVISION", "The expected revision does not match.")
        if request.get("install_kind") == "first_install" and request.get("previous_healthy_sha"):
            return self._error(409, "EVIDENCE_MISSING", "A first install cannot carry a previous healthy SHA.")
        judged = self._judge(request)
        if judged.get("error") is not None:
            error = judged["error"]
            if error.get("code") == "CANDIDATE_STALE":
                self._remember(request, "plan", None, {"state": None, "http": 409, "code": "CANDIDATE_STALE"})
            return error
        return self._store_plan(request, "plan", judged["body"], recovers_plan_id=None, previous_healthy_sha=None)

    def reconcile(self, request):
        opened = self._open(request, "upgrade.reconcile", write=True)
        if opened is not None:
            return opened
        replay = self._replay(request, "reconcile")
        if replay is not None:
            return replay
        plan = self._owned_plan(request)
        if isinstance(plan, dict) and plan.get("code"):
            return plan
        if request.get("expected_revision") != plan["revision"]:
            return self._error(409, "CONFLICT_REVISION", "The expected revision does not match.")
        report = self.deploy.read(app_id=request["app_id"], candidate_id=plan.get("candidate_id"))
        if report.get("status") == "BLOCKED":
            return self._error(409, "DEPENDENCY_UNSATISFIED", "The deploy port is not available on this isolated tree.")
        state, claims_commit = self._effect_state(plan, report)
        original = {"state": state, "plan_id": plan["plan_id"], "http": 200, "code": None}
        outcome = self._cas_reconcile(request, plan["plan_id"], state, claims_commit, original)
        replayed = self._cas_replay(outcome, request, "reconcile")
        if replayed is not None:
            return replayed
        return self._public({
            "http": 200,
            "code": None,
            "state": state,
            "message": "Reconcile classified the observation and did not commit an upgrade.",
            "plan_id": plan["plan_id"],
            "accepted_commit": False,
            "port_claims_commit": claims_commit,
            "journal_recorded": True,
            "candidate_id": plan.get("candidate_id"),
        })

    def recover(self, request):
        opened = self._open(request, "upgrade.recover", write=True)
        if opened is not None:
            return opened
        plan = self._owned_plan(request)
        if isinstance(plan, dict) and plan.get("code"):
            return plan
        if plan["state"] == "SAFE_FROZEN":
            return self._error(403, "POLICY_DENIED", "SAFE_FROZEN has no exit.", reason="safe_frozen_no_exit")
        if plan["state"] not in {"FAILED", "RECONCILE", "BLOCKED"}:
            return self._error(409, "EVIDENCE_MISSING", "This plan has no failed effect to recover.")
        replay = self._replay(request, "recover")
        if replay is not None:
            return replay
        if request.get("expected_revision") != plan["revision"]:
            return self._error(409, "CONFLICT_REVISION", "The expected revision does not match.")
        judged = self._judge(request)
        if judged.get("error") is not None:
            return judged["error"]
        report = self.deploy.read(app_id=request["app_id"], candidate_id=plan.get("candidate_id"))
        port_sha = report.get("previous_healthy_sha")
        if not valid_sha(port_sha):
            port_sha = None
        caller_ignored = "previous_healthy_sha" in request and request.get("previous_healthy_sha") != port_sha
        body = dict(judged["body"])
        reasons = list(body["reasons"])
        if port_sha is None:
            reasons.append("previous_healthy_unproven")
            body["state"] = "BLOCKED"
            body["compatible"] = False
        elif reasons_block(reasons):
            body["state"] = "BLOCKED"
        body["reasons"] = sorted(set(reasons))
        body["previous_healthy_sha"] = port_sha
        body["caller_sha_ignored"] = caller_ignored
        body["recovers_plan_id"] = plan["plan_id"]
        return self._store_plan(
            request,
            "recover",
            body,
            recovers_plan_id=plan["plan_id"],
            previous_healthy_sha=port_sha,
            caller_sha_ignored=caller_ignored,
            source_plan_id=plan["plan_id"],
            source_revision=plan["revision"],
        )

    def _fresh_components(self, request):
        app_id = request["app_id"]
        candidate = request.get("candidate") if isinstance(request.get("candidate"), dict) else {}
        computed = None
        if all(valid_sha(candidate.get(name)) for name in TUPLE_FIELDS):
            computed = candidate_id(
                request["scope"]["repo"],
                request["scope"]["task_id"],
                candidate["head"],
                candidate["base"],
                candidate["merge"],
                candidate["tree"],
            )
        self.build.read(app_id=app_id, candidate_id=computed)
        schedule = self.schedule.read(app_id=app_id, candidate_id=computed)
        delivery = self.delivery.read(app_id=app_id, candidate_id=computed)
        diagnostic = self.diagnostics.read(app_id=app_id, candidate_id=computed)
        runtime = self.deploy.read(app_id=app_id, candidate_id=computed)
        repository = self._repository_component()
        running = sha_view(None if runtime.get("evidence_state") != "present" or runtime.get("status") == "BLOCKED" else runtime.get("running_sha"))
        running["matches_repository"] = repository.get("sha") == running.get("sha") and running.get("sha") is not None
        scheduler = identify_scheduler(schedule)
        macci = sha_view(None if delivery.get("evidence_state") != "present" or delivery.get("status") == "BLOCKED" else delivery.get("macci_sha"))
        hostops_sha = None
        if diagnostic.get("evidence_state") == "present" and diagnostic.get("status") != "BLOCKED":
            hostops_sha = diagnostic.get("running_sha")
        hostops = sha_view(hostops_sha)
        launchd = sha_view(
            None if runtime.get("evidence_state") != "present" or runtime.get("status") == "BLOCKED" else runtime.get("launchd_sha"),
            key="running_sha",
        )
        if runtime.get("evidence_state") == "present":
            launchd["label"] = runtime.get("launchd_label")
        return {
            "repository": repository,
            "running": running,
            "scheduler": scheduler,
            "macci": macci,
            "hostops": hostops,
            "launchd": launchd,
        }

    def _repository_component(self):
        unknown = {"sha": None, "version": None, "state": "unknown", "source": "unknown", "stale": False, "observed_at": None}
        manifest = self.manifest
        if manifest is None or getattr(manifest, "server_controlled", False) is not True:
            return dict(unknown)
        version = getattr(manifest, "repository_version", None)
        sha = getattr(manifest, "repository_sha", None)
        if not isinstance(version, str) or VERSION_RE.fullmatch(version) is None or not valid_sha(sha):
            return dict(unknown)
        return {
            "sha": sha,
            "version": version,
            "state": "identified",
            "source": "server_manifest",
            "stale": False,
            "observed_at": getattr(manifest, "observed_at", None),
        }

    def _cached_component(self, cached, name):
        unknown = {"sha": None, "state": "unknown", "source": "unknown", "stale": False, "observed_at": None}
        if name == "launchd":
            unknown = {"running_sha": None, "state": "unknown", "source": "unknown", "stale": False, "observed_at": None}
        previous = None
        if isinstance(cached, dict):
            sources = cached.get("sources")
            if isinstance(sources, dict) and isinstance(sources.get(name), dict):
                previous = dict(sources[name])
        if previous is None or not _usable(previous):
            return unknown
        chosen = previous
        chosen["source"] = "cache"
        observed_at = chosen.get("observed_at")
        if not isinstance(observed_at, str):
            chosen["stale"] = True
            chosen["state"] = "unknown"
            chosen["observed_at"] = None
            return chosen
        try:
            observed = datetime.fromisoformat(observed_at)
            if observed.tzinfo is None:
                raise ValueError(observed_at)
            age = (self.clock.now() - observed).total_seconds()
        except ValueError:
            chosen["stale"] = True
            chosen["state"] = "unknown"
            return chosen
        chosen["observed_at"] = observed_at
        chosen["stale"] = cache_is_stale(age)
        return chosen

    def _judge(self, request):
        app_id = request["app_id"]
        bound = self._bound_candidate(request)
        if bound.get("error") is not None:
            return bound
        computed = bound["candidate_id"]
        tup = bound["tuple"]
        build = self.build.read(app_id=app_id, candidate_id=computed)
        delivery = self.delivery.read(app_id=app_id, candidate_id=computed)
        if build.get("status") == "BLOCKED" or delivery.get("status") == "BLOCKED":
            return {"error": self._error(409, "DEPENDENCY_UNSATISFIED", "A required port is not available on this isolated tree.")}
        if self._evidence_binding(build, tup, computed) == "stale" or self._evidence_binding(delivery, tup, computed) == "stale":
            return {"error": self._error(409, "CANDIDATE_STALE", "The candidate tuple does not match the recorded evidence.")}
        reasons = []
        decisions = []
        preserved = set()
        if request.get("copy_mode") == "blanket_frontend_src":
            reasons.append("blanket_frontend_src_copy")
        self._apply_manifest(app_id, tup, reasons, decisions, preserved)
        labeled = _labeled_double(build) and _labeled_double(delivery)
        if not labeled:
            reasons.append("evidence_unlabeled")
        else:
            build_problem = _build_problem(build, self._evidence_binding(build, tup, computed))
            if build_problem:
                reasons.append(build_problem)
            if valid_sha(build.get("target_sha")) and build.get("target_sha") != request.get("target_sha"):
                reasons.append("version_mismatch")
            if _macci_problem(delivery, self._evidence_binding(delivery, tup, computed)):
                reasons.append("macci_not_success")
        unique = sorted(set(reasons))
        state = "BLOCKED" if reasons_block(unique) else "PLANNED"
        body = {
            "http": 200,
            "code": None,
            "state": state,
            "message": "The plan is a check. It does not apply an upgrade.",
            "reasons": unique,
            "decisions": decisions,
            "preserved": sorted(preserved),
            "compatible": state == "PLANNED",
            "mergeable_is_compatibility": False,
            "candidate_id": computed,
            "evidence_reused": False,
            "isolation_proven": False,
            "test_double_not_runtime_proof": labeled,
            "journal_recorded": False,
        }
        return {"body": body}

    def _apply_manifest(self, app_id, tup, reasons, decisions, preserved):
        manifest = self.manifest
        if manifest is None or getattr(manifest, "server_controlled", False) is not True:
            reasons.append("server_manifest_missing")
            return
        source_id = getattr(manifest, "source_id", None)
        if not isinstance(source_id, str) or not source_id:
            reasons.append("manifest_source_missing")
        candidate = getattr(manifest, "candidate", None)
        if not isinstance(candidate, dict):
            reasons.append("manifest_candidate_mismatch")
            return
        manifest_tuple = tuple(candidate.get(name) for name in TUPLE_FIELDS)
        if manifest_tuple != tup:
            reasons.append("manifest_candidate_mismatch")
            return
        allowlist = list(getattr(manifest, "allowlist", []) or [])
        problem = allowlist_problem(allowlist)
        if problem:
            reasons.append(problem)
        changes = list(getattr(manifest, "changes", []) or [])
        observed = list(getattr(manifest, "observed_files", []) or [])
        path_list = [item.get("path") for item in changes if isinstance(item, dict)] + list(observed)
        if case_collision(path_list):
            reasons.append("case_collision")
        reasons.extend(isolation_reasons(getattr(manifest, "peers", None), app_id))
        for path in observed:
            kind = classify_path(path, allowlist)
            if kind in PRESERVE:
                preserved.add(path)
        for change in changes:
            if not isinstance(change, dict):
                reasons.append("path_rejected")
                continue
            path = change.get("path")
            if change.get("app_id") not in (None, app_id):
                reasons.append("isolation_cross_app")
            kind = classify_path(path, allowlist)
            if change.get("owner") == "app" and kind == "foundation":
                kind = "ambiguous_owner"
                reasons.append("ambiguous_owner")
            if change.get("op") in {"rename", "delete"} or change.get("symlink") is True:
                reasons.append("rename_or_symlink")
            if isinstance(path, str) and path.startswith("migrations/") and kind == "foundation":
                reasons.append("migration_requires_review")
            text_kind = None
            if kind == "foundation" and change.get("op") not in {"rename", "delete"} and change.get("symlink") is not True:
                text_kind = classify_text(change.get("old"), change.get("new"), change.get("local"))
            classification = text_kind if text_kind not in (None, "unchanged") else kind
            if classification == "ambiguous":
                reasons.append("ambiguous")
            elif classification in {"rejected", "unknown"}:
                reasons.append("path_rejected" if classification == "rejected" else "unknown_path")
            elif classification in PRESERVE:
                reasons.append(classification)
                if isinstance(path, str):
                    preserved.add(path)
            action = "review"
            if classification in PRESERVE:
                action = "preserve"
            elif classification in {"ambiguous", "ambiguous_owner", "rejected", "unknown"}:
                action = "conflict"
            if text_kind != "unchanged":
                decisions.append({
                    "path": path,
                    "classification": classification,
                    "action": action,
                    "auto_resolved": False,
                    "applied": False,
                    "result": None,
                })

    def _bound_candidate(self, request):
        candidate = request.get("candidate")
        if not isinstance(candidate, dict):
            return {"error": self._error(422, "SCHEMA_UNSUPPORTED", "The request was rejected.")}
        fields = [candidate.get(name) for name in ("head", "base", "merge", "tree")]
        if not all(valid_sha(item) for item in fields):
            return {"error": self._error(422, "SCHEMA_UNSUPPORTED", "The request was rejected.")}
        computed = candidate_id(request["scope"]["repo"], request["scope"]["task_id"], *fields)
        supplied = request.get("candidate_id")
        if supplied is not None and supplied != computed:
            return {"error": self._error(409, "CANDIDATE_STALE", "The candidate id does not match the tuple.")}
        if not valid_sha(request.get("target_sha")):
            return {"error": self._error(422, "SCHEMA_UNSUPPORTED", "The request was rejected.")}
        return {"candidate_id": computed, "tuple": tuple(fields)}

    def _evidence_binding(self, report, tup, computed):
        if report.get("evidence_state") != "present":
            return "missing"
        fields = tuple(report.get(name) for name in TUPLE_FIELDS)
        if not all(valid_sha(item) for item in fields) or not isinstance(report.get("candidate_id"), str) or not report.get("candidate_id"):
            return "unbound"
        if fields != tup or report.get("candidate_id") != computed:
            return "stale"
        return "bound"

    def _effect_state(self, plan, report):
        port_state = report.get("state")
        effect = report.get("effect")
        if port_state == "SAFE_FROZEN":
            return "SAFE_FROZEN", False
        if port_state == "COMMIT" or effect == "ok":
            return "RECONCILE", True
        if effect == "unknown" or port_state == "RECONCILE":
            return "RECONCILE", False
        if effect == "failed" or port_state == "FAILED":
            return "FAILED", False
        if effect == "not_occurred":
            return plan["state"], False
        return "RECONCILE", False

    def _store_plan(self, request, operation, body, *, recovers_plan_id, previous_healthy_sha, caller_sha_ignored=False, source_plan_id=None, source_revision=None):
        self._sequence += 1
        raw = f"{id(self)}:{operation}:{request['app_id']}:{self.clock.now().isoformat()}:{self._sequence}".encode("utf-8")
        plan_id = "upl_" + hashlib.sha256(raw).hexdigest()[:24]
        plan = {
            "plan_id": plan_id,
            "revision": 1,
            "state": body["state"],
            "repo": request["scope"]["repo"],
            "task_id": request["scope"]["task_id"],
            "app_id": request["app_id"],
            "candidate_id": body.get("candidate_id"),
            "reasons": list(body.get("reasons") or []),
            "decisions": list(body.get("decisions") or []),
            "preserved": list(body.get("preserved") or []),
            "write_set": [],
            "applicable": False,
            "upgrade_executed": False,
            "recovers_plan_id": recovers_plan_id,
            "previous_healthy_sha": previous_healthy_sha,
            "created_at": self.clock.now().isoformat(),
        }
        original = {"state": plan["state"], "plan_id": plan_id, "http": 200, "code": None}
        outcome = self._cas_insert(
            request,
            operation,
            plan,
            original,
            observations=None,
            source_plan_id=source_plan_id,
            source_revision=source_revision,
        )
        replayed = self._cas_replay(outcome, request, operation)
        if replayed is not None:
            return replayed
        published = dict(body)
        published.update({
            "plan_id": plan_id,
            "journal_recorded": True,
            "recovers_plan_id": recovers_plan_id,
            "previous_healthy_sha": previous_healthy_sha,
            "caller_sha_ignored": caller_sha_ignored,
            "invented_previous_healthy_sha": False,
        })
        return self._public(published)

    def _owned_plan(self, request):
        plan = self.journal.get_plan(request.get("plan_id"))
        scope = request.get("scope") if isinstance(request.get("scope"), dict) else {}
        if plan is None or plan.get("app_id") != request.get("app_id"):
            return self._error(409, "EVIDENCE_MISSING", "The plan is not in this application journal.")
        if plan.get("repo") != scope.get("repo") or plan.get("task_id") != scope.get("task_id"):
            return self._error(409, "EVIDENCE_MISSING", "The plan is not in this application journal.")
        if not self._principal_covers(plan.get("repo"), plan.get("task_id"), plan.get("app_id")):
            return self._error(403, "POLICY_DENIED", "The actor is not allowed to do this.")
        candidate = request.get("candidate") if isinstance(request.get("candidate"), dict) else None
        if isinstance(candidate, dict) and all(valid_sha(candidate.get(name)) for name in TUPLE_FIELDS):
            computed = candidate_id(scope["repo"], scope["task_id"], *(candidate[name] for name in TUPLE_FIELDS))
            if computed != plan.get("candidate_id"):
                return self._error(409, "EVIDENCE_MISSING", "The plan is not in this application journal.")
        supplied = request.get("candidate_id")
        if supplied is not None and supplied != plan.get("candidate_id"):
            return self._error(409, "EVIDENCE_MISSING", "The plan is not in this application journal.")
        return plan

    def _remember(self, request, operation, plan_id, original):
        key = self._idem_key(request, operation)
        record = {
            "hash": _payload_hash(request),
            "plan_id": plan_id,
            "operation": operation,
            "original": original,
        }

        def mutate(state):
            state["idempotency"][key] = record
            return True, None

        self.journal.persist(mutate)

    def _cas_insert(self, request, operation, plan, original, *, observations, source_plan_id, source_revision):
        key = self._idem_key(request, operation)
        payload = _payload_hash(request)

        def mutate(state):
            existing = state["idempotency"].get(key)
            if existing is not None:
                return False, {"replay": existing, "mismatch": existing.get("hash") != payload}
            if source_plan_id is not None:
                source = state["plans"].get(source_plan_id)
                if source is None:
                    return False, {"missing": True}
                if source.get("state") == "SAFE_FROZEN":
                    return False, {"frozen": True}
                if source.get("revision") != source_revision:
                    return False, {"conflict": True}
            state["plans"][plan["plan_id"]] = plan
            state["idempotency"][key] = {
                "hash": payload,
                "plan_id": plan["plan_id"],
                "operation": operation,
                "original": original,
            }
            if observations is not None:
                state["cache"][request["app_id"]] = observations
            return True, {"applied": True}

        return self.journal.persist(mutate)

    def _cas_reconcile(self, request, plan_id, state_name, claims_commit, original):
        key = self._idem_key(request, "reconcile")
        payload = _payload_hash(request)
        expected = request.get("expected_revision")

        def mutate(state):
            existing = state["idempotency"].get(key)
            if existing is not None:
                return False, {"replay": existing, "mismatch": existing.get("hash") != payload}
            current = state["plans"].get(plan_id)
            if current is None:
                return False, {"missing": True}
            if current.get("state") == "SAFE_FROZEN":
                return False, {"frozen": True}
            if current.get("revision") != expected:
                return False, {"conflict": True, "revision": current.get("revision")}
            updated = dict(current)
            updated["state"] = state_name
            updated["revision"] = expected + 1
            updated["accepted_commit"] = False
            updated["port_claims_commit"] = claims_commit
            state["plans"][plan_id] = updated
            state["idempotency"][key] = {
                "hash": payload,
                "plan_id": plan_id,
                "operation": "reconcile",
                "original": original,
            }
            return True, {"applied": True}

        return self.journal.persist(mutate)

    def _cas_replay(self, outcome, request, operation):
        if not isinstance(outcome, dict):
            return self._error(409, "EVIDENCE_MISSING", "The plan is not in this application journal.")
        if outcome.get("applied") is True:
            return None
        if outcome.get("mismatch") is True:
            return self._error(409, "IDEMPOTENCY_PAYLOAD_MISMATCH", "The idempotency key was reused with a different payload.")
        if outcome.get("replay") is not None:
            return self._replay(request, operation)
        if outcome.get("frozen") is True:
            return self._error(403, "POLICY_DENIED", "SAFE_FROZEN cannot be rewritten.", reason="safe_frozen_no_rewrite")
        if outcome.get("conflict") is True:
            return self._error(409, "CONFLICT_REVISION", "The expected revision does not match.")
        return self._error(409, "EVIDENCE_MISSING", "The plan is not in this application journal.")

    def _replay(self, request, operation):
        record = self.journal.get_idem(self._idem_key(request, operation))
        if record is None:
            return None
        if record.get("hash") != _payload_hash(request):
            return self._error(409, "IDEMPOTENCY_PAYLOAD_MISMATCH", "The idempotency key was reused with a different payload.")
        current = record.get("original", {}).get("state")
        plan = self.journal.get_plan(record.get("plan_id")) if record.get("plan_id") else None
        if plan is not None:
            current = plan["state"]
        body = {
            "http": 200,
            "code": "IDEMPOTENCY_REPLAY",
            "state": current,
            "message": "Replayed the original result.",
            "replayed": True,
            "original_result": record.get("original"),
            "current_state": current,
            "journal_recorded": False,
        }
        if record.get("plan_id"):
            body["plan_id"] = record["plan_id"]
        return self._public(body)

    def _open(self, request, action, *, write):
        schema_error = self._schema(request, write=write)
        if schema_error is not None:
            return schema_error
        if not _actor_ok(request.get("actor")):
            return self._error(403, "NOT_AUTHORIZED", "The actor is not accepted.")
        principal = self.principal
        if principal is None or getattr(principal, "server_assigned", False) is not True:
            return self._error(403, "NOT_AUTHORIZED", "The actor is not accepted.")
        if not self._granted(principal, request, action):
            return self._error(403, "POLICY_DENIED", "The actor is not allowed to do this.")
        if request.get("production_database") is True or request.get("database_url"):
            return self._error(403, "PRODUCTION_DB_FORBIDDEN", "Production database access is forbidden.")
        return None

    def _schema(self, request, *, write):
        if not isinstance(request, dict) or request.get("schema_version") != 2 or "received_at" in request:
            return self._error(422, "SCHEMA_UNSUPPORTED", "The request was rejected.")
        for text in _strings(request):
            if any(marker in text for marker in SECRET_MARKERS):
                return self._error(422, "SCHEMA_UNSUPPORTED", "The request was rejected.")
        actor = request.get("actor")
        scope = request.get("scope")
        if not isinstance(actor, dict) or not isinstance(scope, dict):
            return self._error(422, "SCHEMA_UNSUPPORTED", "The request was rejected.")
        if request.get("app_id") != scope.get("app_id"):
            return self._error(422, "SCHEMA_UNSUPPORTED", "The request was rejected.")
        if not all(isinstance(scope.get(name), str) and scope.get(name) for name in ("repo", "task_id", "app_id")):
            return self._error(422, "SCHEMA_UNSUPPORTED", "The request was rejected.")
        if write:
            if not isinstance(request.get("idempotency_key"), str) or not request["idempotency_key"]:
                return self._error(422, "SCHEMA_UNSUPPORTED", "The request was rejected.")
            if type(request.get("expected_revision")) is not int:
                return self._error(422, "SCHEMA_UNSUPPORTED", "The request was rejected.")
        return None

    def _granted(self, principal, request, action):
        scope = request["scope"]
        if not self._principal_covers(scope.get("repo"), scope.get("task_id"), scope.get("app_id"), principal=principal):
            return False
        return any(
            grant.principal_id == principal.principal_id
            and grant.action == action
            and grant.app_id == scope.get("app_id")
            and getattr(grant, "repo", None) == scope.get("repo")
            and getattr(grant, "task_id", None) == scope.get("task_id")
            for grant in self.grants
        )

    def _principal_covers(self, repo, task_id, app_id, principal=None):
        principal = self.principal if principal is None else principal
        if principal is None or getattr(principal, "server_assigned", False) is not True:
            return False
        if getattr(principal, "trusted_source", None) != SOURCE_BY_KIND.get(getattr(principal, "kind", None)):
            return False
        if getattr(principal, "repo_id", None) != repo:
            return False
        apps = getattr(principal, "app_ids", None)
        if not apps:
            single = getattr(principal, "app_id", None)
            apps = () if not single else (single,)
        if app_id not in set(apps):
            return False
        tasks = getattr(principal, "tasks", ()) or ()
        return task_id in set(tasks)

    def _idem_key(self, request, operation):
        return "|".join([
            self.principal.principal_id,
            request["scope"]["repo"],
            request["scope"]["task_id"],
            request["app_id"],
            operation,
            request["idempotency_key"],
        ])

    def _disabled(self, reason):
        return self._public({
            "http": 403,
            "code": "POLICY_DENIED",
            "state": None,
            "message": "The upgrade action is disabled.",
            "reason": reason,
            "enrolled": False,
            "journal_recorded": False,
        })

    def _error(self, http, code, message, *, reason=None):
        body = {"http": http, "code": code, "state": None, "message": message, "journal_recorded": False}
        if reason is not None:
            body["reason"] = reason
        return self._public(body)

    def _public(self, body):
        published = {
            "schema_version": 2,
            "side_effect": False,
            "upgrade_executed": False,
            "applied": False,
            "applicable": False,
            "write_set": [],
            "auto_resolved": False,
            "runtime_proof": False,
            "production_enabled": False,
            "production_verification_enabled": False,
            "mounted": False,
            "real_merge": False,
            "real_deploy": False,
            "real_upgrade": False,
            "acceptance_enabled": False,
            "production_db_access": False,
            "lease_token": None,
            "fetched": False,
            "network_under_lock": False,
            "invented_previous_healthy_sha": False,
            "enrolled": False,
        }
        published.update(body)
        published["schema_version"] = 2
        published["side_effect"] = False
        published["upgrade_executed"] = False
        published["applied"] = False
        published["applicable"] = False
        published["write_set"] = []
        published["auto_resolved"] = False
        published["runtime_proof"] = False
        published["production_enabled"] = False
        published["production_verification_enabled"] = False
        published["mounted"] = False
        published["real_merge"] = False
        published["real_deploy"] = False
        published["real_upgrade"] = False
        published["acceptance_enabled"] = False
        published["production_db_access"] = False
        published["lease_token"] = None
        published["fetched"] = False
        published["network_under_lock"] = False
        published["invented_previous_healthy_sha"] = False
        published["enrolled"] = False
        return published


def reasons_block(reasons):
    return any(reason in BLOCKING for reason in reasons)


def _labeled_double(report):
    return (
        isinstance(report, dict)
        and report.get("not_runtime_proof") is True
        and report.get("test_double") is True
        and report.get("proof_label") == "test_double_not_runtime_proof"
        and report.get("runtime_proof") is not True
        and report.get("macci_proof") is not True
    )


def _build_problem(build, binding):
    if build.get("evidence_state") != "present" or binding != "bound":
        return "build_evidence_missing"
    exit_code = build.get("exit_code")
    if build.get("frontend_failed") is True or type(exit_code) is not int or exit_code != 0:
        return "build_failed_not_compatible"
    return None


def _macci_problem(delivery, binding):
    if binding != "bound" or delivery.get("macci_state") != "success" or not valid_sha(delivery.get("macci_sha")):
        return "macci_not_success"
    return None


def isolation_reasons(peers, app_id):
    if not isinstance(peers, list):
        return ["isolation_unproven"]
    reasons = []
    roots = {}
    prefixes = {}
    saw_self = False
    for peer in peers:
        if not isinstance(peer, dict):
            reasons.append("isolation_unproven")
            continue
        other = peer.get("app_id")
        root = peer.get("data_root")
        prefix = peer.get("table_prefix")
        if not isinstance(other, str) or not isinstance(root, str) or not isinstance(prefix, str):
            reasons.append("isolation_unproven")
            continue
        if other == app_id:
            saw_self = True
        if root in roots and roots[root] != other:
            reasons.append("isolation_collision")
        if prefix in prefixes and prefixes[prefix] != other:
            reasons.append("isolation_collision")
        roots[root] = other
        prefixes[prefix] = other
        if prefix != f"mod_{other}__":
            reasons.append("isolation_collision")
    if not saw_self:
        reasons.append("isolation_unproven")
    return reasons


def _actor_ok(actor):
    if not isinstance(actor, dict) or actor.get("kind") != "test":
        return False
    proof = actor.get("proof_ref")
    return isinstance(proof, str) and proof.startswith("test-proof://") and isinstance(actor.get("id"), str) and bool(actor["id"])


def _payload_hash(request):
    body = {key: value for key, value in request.items() if key != "actor"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
