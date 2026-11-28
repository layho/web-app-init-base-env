"""Isolated closed loop for one temporary directory.

The loop calls the delivery service, the scheduler, the deploy transaction
engine, lifecycle cleanup, and the acceptance projection. Merge, deploy, and
publish stay closed. Nothing here starts a host service or a container.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.control_board.projection import project
from app.control_delivery.ports import (
    FixtureAuth,
    FixtureDeploy,
    FixtureEvents,
    FixtureRemote,
    FixtureSchedule,
    ServerClock,
)
from app.control_delivery.service import DeliveryService
from app.control_joint.evidence import evidence_matrix
from app.control_joint.w05_issue import issue_w05_receipt
from app.control_joint.w07_driver import IsolatedDeploy
from app.control_joint.w09_driver import APP, SCOPES, _policy, _probe
from app.control_lifecycle.flags import HOST_MUTATION, REAL_INSTALL, REAL_UNINSTALL
from app.control_lifecycle.grant import W02AuthorizationPort
from app.control_lifecycle.host import SyntheticHost
from app.control_lifecycle.platform import DIGEST
from app.control_lifecycle.service import LifecycleService
from app.control_scheduler.scheduler import Scheduler


def run_isolated_loop(root: Path) -> dict:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    exercised: dict[str, str] = {}
    stages = {}
    stages["feedback"] = _feedback_through_projection(root, exercised)
    stages["schedule"] = _schedule(root, exercised)
    stages["deploy_transaction"] = _deploy(root, exercised)
    stages["cleanup"] = _cleanup(root, exercised)
    stages["issuance"] = _issue(root)
    matrix = evidence_matrix(exercised)
    return {
        "ok": all(item.get("ok") for item in stages.values()),
        "stages": stages,
        "exercised": exercised,
        "evidence": matrix,
        "production_enabled": False,
        "real_install": REAL_INSTALL,
        "real_uninstall": REAL_UNINSTALL,
        "host_mutation": HOST_MUTATION,
        "macci_receipt": False,
        "public_running_sha": None,
        "branch_protection_verified": False,
    }


def _feedback_through_projection(root: Path, exercised: dict) -> dict:
    service = DeliveryService(
        root / "w05",
        auth=FixtureAuth(),
        events=FixtureEvents(),
        schedule=FixtureSchedule(),
        deploy=FixtureDeploy(),
        remote=FixtureRemote(),
        clock=ServerClock(),
    )
    actor = {"kind": "test", "id": "fixture", "proof_ref": "test-proof://w11/loop"}
    scope = {"repo": "layho/web-app-init-base-env", "task_id": "tsk_w11"}
    submitted = service.submit_feedback(
        actor=actor,
        scope=scope,
        author_id="fixture",
        feedback_id="fb_loop",
        category="bug",
        body="isolated loop",
        idempotency_key="loop-submit",
        expected_revision=0,
    )
    if not submitted.ok:
        return {"ok": False, "stage": "feedback", "code": submitted.code}
    exercised["W05"] = "app.control_delivery.service.DeliveryService.submit_feedback"
    chain = {
        "chain_id": "ch_loop",
        "pr_number": 11,
        "required": True,
        "head": "a" * 40,
        "base": "b" * 40,
        "merge": "c" * 40,
        "tree": "d" * 40,
    }
    claimed = service.claim_task(
        actor=actor,
        scope=scope,
        task_id="tsk_w11",
        feedback_ids=["fb_loop"],
        chains=[chain],
        idempotency_key="loop-claim",
        expected_revision=0,
    )
    if not claimed.ok:
        return {"ok": False, "stage": "claim", "code": claimed.code}
    stored = service.store.read("chain", "tsk_w11__ch_loop")
    if not isinstance(stored, dict):
        return {"ok": False, "stage": "claim", "code": "EVIDENCE_MISSING"}
    bound = service.bind_test_evidence(
        actor=actor,
        scope=scope,
        task_id="tsk_w11",
        chain_id="ch_loop",
        head=stored["head"],
        base=stored["base"],
        merge=stored["merge"],
        tree=stored["tree"],
        passed=True,
        run_id="run_loop",
        idempotency_key="loop-test",
        expected_revision=stored["revision"],
    )
    if not bound.ok:
        return {"ok": False, "stage": "test", "code": bound.code}
    exercised["W05"] = "app.control_delivery.service.DeliveryService.bind_test_evidence"
    current = service.store.read("chain", "tsk_w11__ch_loop")
    service.deploy.observations[current["candidate_id"]] = {
        "guard": "exact_remote_observation",
        "exact": True,
        "status": "observed",
        "candidate_id": current["candidate_id"],
        "evidence_ref": "isolated://observe",
    }
    observed = service.apply_observation(
        actor=actor,
        scope=scope,
        task_id="tsk_w11",
        chain_id="ch_loop",
        idempotency_key="loop-observe",
        expected_revision=current["revision"],
    )
    if not observed.ok:
        return {"ok": False, "stage": "delivery", "code": observed.code}
    current = service.store.read("chain", "tsk_w11__ch_loop")
    service.deploy.observations[current["candidate_id"]] = {
        "guard": "deployment_committed",
        "exact": True,
        "status": "observed",
        "candidate_id": current["candidate_id"],
        "evidence_ref": "isolated://deploy-record",
    }
    delivered = service.apply_observation(
        actor=actor,
        scope=scope,
        task_id="tsk_w11",
        chain_id="ch_loop",
        idempotency_key="loop-deliver",
        expected_revision=current["revision"],
    )
    if not delivered.ok:
        return {"ok": False, "stage": "delivery", "code": delivered.code}
    projected = service.project(actor=actor, task_id="tsk_w11", scope=scope)
    if not projected.ok:
        return {"ok": False, "stage": "projection", "code": projected.code}
    board = project([], schedule={"test_double": True, "runtime_evidence": False})
    exercised["W10"] = "app.control_board.projection.project"
    return {
        "ok": True,
        "feedback": submitted.body.get("state"),
        "test_proof_class": bound.body.get("proof_class"),
        "delivery_state": delivered.body.get("state"),
        "projection_state": projected.body.get("state") or projected.state,
        "board_tasks": len(board.get("tasks") or {}),
        "macci_proof": False,
        "real_merge": False,
        "real_deploy": False,
        "runtime_acceptance": False,
    }


def _schedule(root: Path, exercised: dict) -> dict:
    schedule = root / "scheduler"
    schedule.mkdir()
    os.chmod(schedule, 0o700)
    scheduler = Scheduler(schedule, _Closed(), _Closed(), repo_id="layho/web-app-init-base-env")
    opened = scheduler.open_version("v060", baseline="a" * 40)
    task = scheduler.open_task(task_id="tsk_w11", version="v060", app_id="base", worktree="work/tree")
    exercised["W04"] = "app.control_scheduler.scheduler.Scheduler.open_task"
    return {"ok": True, "version": opened.get("state"), "task": task.get("task_id"), "production_enabled": False}


def _deploy(root: Path, exercised: dict) -> dict:
    driver = IsolatedDeploy(root / "deploy")
    try:
        frozen = driver.recover_unknown_switch()
    finally:
        driver.close()
    if frozen.get("state") != "SAFE_FROZEN":
        return {"ok": False, "stage": "deploy_transaction", "state": frozen.get("state"), "code": frozen.get("code")}
    exercised["W07"] = "app.control_deploy.engine.DeployEngine.plan"
    return {
        "ok": True,
        "state": frozen.get("state"),
        "host_mutation": False,
        "real_host": False,
        "switched_again": frozen.get("switched_again"),
    }


def _cleanup(root: Path, exercised: dict) -> dict:
    if REAL_UNINSTALL or REAL_INSTALL or HOST_MUTATION:
        return {"ok": False, "code": "EXECUTION_DENIED"}
    workspace = root / "w09"
    workspace.mkdir()
    os.chmod(workspace, 0o700)
    home = workspace / "home"
    inventory = workspace / "inventory"
    home.mkdir()
    inventory.mkdir()
    os.chmod(home, 0o700)
    os.chmod(inventory, 0o700)
    owned = inventory / "owned.txt"
    owned.write_text("synthetic", encoding="utf-8")
    os.chmod(owned, 0o600)
    service = LifecycleService(workspace, host=SyntheticHost(_probe()), authorizer=W02AuthorizationPort(_policy()))
    grant = service.issue_grant("installer", SCOPES, APP)
    payload = {
        "run_id": "run-loop",
        "app_id": APP,
        "repo": "acme/app",
        "domain": "app.example.com",
        "port": 18080,
        "track": "main",
        "title": "app",
        "foundation_digest": DIGEST,
        "parameters": {"install_deps": False, "github_https": False},
        "resources": [{"resource_id": "owned", "kind": "file", "owner": APP, "relative": "owned.txt"}],
        "resource_ids": ["owned"],
    }
    observed = service.call(grant, "resource.observe", payload, idempotency_key="loop-observe-file", expected_revision=0)
    if observed.code != "OK":
        return {"ok": False, "stage": "cleanup", "code": observed.code}
    cleaned = service.call(
        grant,
        "cleanup.synthetic",
        payload,
        idempotency_key="loop-cleanup",
        expected_revision=observed.revision,
    )
    if cleaned.code != "OK" or owned.exists():
        return {"ok": False, "stage": "cleanup", "code": cleaned.code, "remains": owned.exists()}
    exercised["W09"] = "app.control_lifecycle.service.LifecycleService.call"
    return {
        "ok": True,
        "code": cleaned.code,
        "removed": True,
        "real_uninstall": False,
        "host_mutation": False,
        "synthetic": True,
    }


def _issue(root: Path) -> dict:
    issued = issue_w05_receipt(
        root,
        operation_id="loop-op",
        expected_revision=1,
        candidate_digest="ab" * 32,
        repo_id="layho/web-app-init-base-env",
        app_id="base",
        result={"http": 200, "state": "ACCEPTING", "runtime_acceptance": False},
    )
    if issued.get("status") != "applied" or not issued.get("wrote"):
        return {"ok": False, "stage": "issuance", "status": issued.get("status")}
    from app.control_joint.receipts import query_operation_receipt

    arguments = dict(
        operation_id="loop-op",
        expected_revision=1,
        candidate_digest="ab" * 32,
        repo_id="layho/web-app-init-base-env",
        app_id="base",
    )
    found = query_operation_receipt(root, authority="W05", **arguments)
    w04_before = query_operation_receipt(root, authority="W04", **arguments)
    conflict = issue_w05_receipt(
        root,
        operation_id="loop-op",
        expected_revision=1,
        candidate_digest="ab" * 32,
        repo_id="layho/web-app-init-base-env",
        app_id="base",
        result={"http": 500, "state": "FAILED"},
    )
    w04_after = query_operation_receipt(root, authority="W04", **arguments)
    still = query_operation_receipt(root, authority="W05", **arguments)
    return {
        "ok": (
            found.get("status") == "applied"
            and conflict.get("status") == "conflict"
            and conflict.get("wrote") is False
            and still.get("status") == "applied"
            and w04_before.get("status") == w04_after.get("status")
        ),
        "queried": found.get("status"),
        "conflict": conflict.get("status"),
        "w04": w04_after.get("status"),
    }


class _Closed:
    def execute(self, operation, body, route_class="isolated"):
        del operation, body, route_class
        raise RuntimeError("closed")
