"""Drive the W07 deploy machine inside an isolated directory.

``record`` plans a deployment, applies through verify, then feeds an unknown
switch result and reconciles it to ``SAFE_FROZEN``. It does not switch a host
service. A second record sees the frozen deployment and does not dispatch the
switch again.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from app.control_deploy.contract_machine import candidate_id, digest_of
from app.control_deploy.engine import DeployEngine, Request
from app.control_deploy.ports import Effect
from app.control_joint.w07_ports import OLD, OLD_BYTES, TOKEN, ClosedAuth, ClosedEvents, ClosedLease, ClosedService

HEAD = "2" * 40
BASE = "3" * 40
MERGE = "4" * 40
TREE = "5" * 40
NEW_BYTES = b"candidate-artifact-isolated"
SPEC_REPO = "layho/web-app-init-base-env"
TASK = "tsk_w11"


def _candidate():
    return {
        "repo_id": SPEC_REPO,
        "task_id": TASK,
        "head": HEAD,
        "base": BASE,
        "merge": MERGE,
        "tree": TREE,
    }


def _request(**overrides):
    cand = _candidate()
    body = NEW_BYTES
    previous = OLD_BYTES
    from app.control_deploy.contract_machine import load_contract

    _contract, _edges, spec = load_contract()
    request = Request(
        actor={"kind": "test", "id": "isolated", "proof_ref": "test-proof://w11/isolated"},
        principal_id="isolated",
        scope="deploy",
        expected_revision=overrides.pop("expected_revision", 0),
        idempotency_key=overrides.pop("idempotency_key", "idem-w11-plan"),
        run_id="run-isolated",
        lease_token=TOKEN,
        lease_generation=1,
        server_now=100,
        candidate=cand,
        candidate_id=candidate_id(spec, cand["repo_id"], cand["task_id"], cand["head"], cand["base"], cand["merge"], cand["tree"]),
        artifact_digest=digest_of(body),
        artifact_bytes=body,
        service_id="web",
        previous_healthy_sha=OLD,
        previous_artifact_digest=digest_of(previous),
        previous_artifact_bytes=previous,
        running_sha=OLD,
        deployment_id="dep_w11",
    )
    if overrides:
        request = replace(request, **overrides)
    return request


class IsolatedDeploy:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.service = ClosedService()
        self.lease = ClosedLease()
        self.auth = ClosedAuth()
        self.events = ClosedEvents()
        self.engine = self._open()
        self.revision = 0
        self.fence = None
        self.deployment_id = "dep_w11"

    def _open(self):
        return DeployEngine(
            self.root,
            service=self.service,
            lease=self.lease,
            auth=self.auth,
            events=self.events,
        )

    def close(self):
        self.engine.close()

    def reopen(self):
        self.engine.close()
        self.engine = self._open()
        return self.engine

    def call(self, method, key, **overrides):
        fields = {
            "idempotency_key": key,
            "expected_revision": self.revision,
            "fence_generation": self.fence,
            "deployment_id": self.deployment_id,
        }
        fields.update(overrides)
        result = getattr(self.engine, method)(_request(**fields))
        if result.deployment_id:
            self.deployment_id = result.deployment_id
        if result.revision is not None:
            self.revision = result.revision
            self.fence = result.fence_generation
        return result

    def recover_unknown_switch(self) -> dict:
        """Run plan → stage → verify, then an unknown switch, then freeze.

        Re-entry after ``SAFE_FROZEN`` does not call ``switch`` again.
        """

        store = self.engine.journal.replay()
        existing = store["deployments"].get(self.deployment_id)
        if existing and existing.get("state") == "SAFE_FROZEN":
            return {
                "state": "SAFE_FROZEN",
                "code": None,
                "switched_again": False,
                "switch_calls": self.service.calls.count("switch"),
                "host_mutation": False,
                "real_host": False,
            }
        planned = self.call("plan", "idem-w11-plan")
        if planned.state != "PLAN":
            return _failed(planned)
        for key, state in (("stage", "STAGE"), ("verify", "VERIFY")):
            stepped = self.call("apply", f"idem-w11-{key}")
            if stepped.state != state:
                return _failed(stepped)
        self.service.queue["switch"] = [Effect("unknown", detail="no receipt")]
        self.engine.halt_at = "after_result:switch"
        from app.control_deploy.engine import Interrupted

        try:
            self.call("apply", "idem-w11-switch")
        except Interrupted:
            pass
        calls_after_effect = self.service.calls.count("switch")
        self.reopen()
        resumed = self.call("apply", "idem-w11-switch")
        if resumed.state != "RECONCILE" or resumed.code != "SIDE_EFFECT_UNKNOWN":
            return _failed(resumed)
        if self.service.calls.count("switch") != calls_after_effect:
            return {"state": resumed.state, "code": "REPEATED_UNKNOWN", "host_mutation": False, "real_host": False}
        frozen = self.call("reconcile", "idem-w11-unknown", observation={"status": "unknown"})
        if frozen.state != "SAFE_FROZEN":
            return _failed(frozen)
        later = self.call("apply", "idem-w11-after-freeze")
        return {
            "state": later.state,
            "code": later.code,
            "switched_again": self.service.calls.count("switch") != calls_after_effect,
            "switch_calls": self.service.calls.count("switch"),
            "host_mutation": False,
            "real_host": self.service.real_host,
        }

    def status(self) -> dict:
        store = self.engine.journal.replay()
        deployment = store["deployments"].get("dep_w11")
        return {
            "state": None if deployment is None else deployment.get("state"),
            "host_mutation": False,
            "real_host": False,
            "present": deployment is not None,
        }


def _failed(result) -> dict:
    return {
        "state": result.state,
        "code": result.code,
        "host_mutation": False,
        "real_host": False,
        "switched_again": False,
    }


def public_line(result: dict, caller: str) -> bytes:
    body = {
        "command": "record",
        "caller": caller,
        "state": result.get("state"),
        "code": result.get("code"),
        "host_mutation": False,
        "real_host": False,
        "install": False,
        "service_switch": False,
    }
    return (json.dumps(body, sort_keys=True) + "\n").encode("utf-8")
