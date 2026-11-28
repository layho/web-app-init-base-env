"""Drive the W09 lifecycle machine inside an isolated directory.

The synthetic host is a test double. Entry planning and recovery run through
``LifecycleService``. No installer command is executed.
"""

from __future__ import annotations

import json
import os

from app.control_diagnostics.safe_io import UnsafePath, read_bounded
from app.control_lifecycle.grant import ServerPolicy, W02AuthorizationPort
from app.control_lifecycle.host import SyntheticHost
from app.control_lifecycle.platform import DIGEST, REQUIRED_DISK_BYTES
from app.control_lifecycle.service import REQUIRED_SCOPE, LifecycleService
from app.control_joint.safe_root import BoundRoot

APP = "acme--app"
REPO = "acme/app"
PORT = 18080
DOMAIN = "app.example.com"
SCOPES = frozenset(REQUIRED_SCOPE.values())


def _probe():
    return {
        "os": "linux",
        "version": "24.04",
        "arch": "x86_64",
        "distro": "ubuntu",
        "package_manager": "apt",
        "service_manager": "systemd",
        "uid": 1000,
        "euid_is_root": False,
        "tty": False,
        "disk_free_bytes": REQUIRED_DISK_BYTES,
        "dependencies": {
            "git": True,
            "node": True,
            "npm": True,
            "initdb": True,
            "postgres": True,
            "pg_isready": True,
        },
        "ports": {PORT: None},
        "identity": {"principal": "installer", "app_id": APP},
        "permissions": {"uid": 1000, "mode": 0o700, "owner_matches": True},
        "credentials": {
            "cloudflare": {"present": True, "fingerprint": "sha256:" + ("ab" * 32), "scope": "cloudflare.map"},
            "github_feedback": {"present": True, "fingerprint": "sha256:" + ("cd" * 32), "scope": "feedback"},
        },
        "expected_foundation_digest": DIGEST,
        "cloudflare": {
            "tunnel_alias": "tunnel.cfargotunnel.com",
            "dns": [],
            "ingress": [{"hostname": None, "path": None, "service": "http_status:404"}],
        },
        "service": {"active": False, "unit_digest": None, "scope_level": "user", "generation": 0},
        "cluster": {"present": False, "owner": None},
        "health": {"ok": True, "running_sha": "a" * 40},
    }


def _policy():
    return ServerPolicy(
        principal="installer",
        app_id=APP,
        repo=REPO,
        host_id="synthetic-unverified-host",
        scopes=SCOPES,
        run_scope=f"runs:{APP}",
        resource_scope=f"inventory:{APP}",
        enabled=True,
    )


def _payload():
    return {
        "run_id": "run-1",
        "app_id": APP,
        "repo": REPO,
        "domain": DOMAIN,
        "port": PORT,
        "track": "main",
        "title": "app",
        "foundation_digest": DIGEST,
        "parameters": {"install_deps": False, "github_https": False},
    }


def drive(root, command: str) -> dict:
    bound = BoundRoot.open(root)
    home = inventory = None
    try:
        home = bound.mkdir("home")
        inventory = bound.mkdir("inventory")
        faults = set()
        if os.environ.get("V060_JOINT_FAULT") == "recovery_fails":
            faults.add("recovery_fails")
        service = LifecycleService(root, host=SyntheticHost(_probe(), faults), authorizer=W02AuthorizationPort(_policy()))
        grant = service.issue_grant("installer", SCOPES, APP)
        if command == "status":
            body = service.inspect(grant)
            return {"ok": True, "code": "OK", "revision": body.get("revision"), "install": False, "service_switch": False}
        planned = service.call(grant, "entry.plan", _payload(), idempotency_key="isolated-entry-plan", expected_revision=0)
        if command == "plan":
            _write_plan(bound, planned)
            return _view(planned)
        recovered = service.call(
            grant,
            "recovery.plan",
            _payload(),
            idempotency_key="isolated-recovery-plan",
            expected_revision=planned.revision,
        )
        if "recovery_fails" in faults:
            frozen = service.call(
                grant,
                "recovery.apply_synthetic",
                _payload(),
                idempotency_key="isolated-recovery-apply",
                expected_revision=recovered.revision,
            )
            _write_plan(bound, frozen)
            return _view(frozen)
        _write_plan(bound, recovered)
        return _view(recovered)
    finally:
        if home is not None:
            os.close(home)
        if inventory is not None:
            os.close(inventory)
        bound.close()


def _view(result) -> dict:
    return {
        "ok": bool(result.ok) and result.code == "OK" and not result.host_side_effect and result.executed_commands == (),
        "code": result.code,
        "revision": result.revision,
        "install": False,
        "service_switch": False,
        "host_side_effect": bool(result.host_side_effect),
    }


def _write_plan(bound: BoundRoot, result) -> None:
    document = {
        "command": "plan",
        "install": False,
        "uninstall": False,
        "privilege": False,
        "service_switch": False,
        "host_side_effect": False,
        "code": result.code,
        "ok": bool(result.ok),
        "revision": result.revision,
        "test_double": True,
        "executed_commands": [],
        "root": None,
    }
    bound.write_new("lifecycle-plan.json", json.dumps(document, sort_keys=True).encode("utf-8"))


def read_status(root) -> bytes | None:
    bound = BoundRoot.open(root)
    try:
        try:
            return read_bounded(bound.fd, "lifecycle-plan.json", 262144)
        except FileNotFoundError:
            return None
    finally:
        bound.close()
