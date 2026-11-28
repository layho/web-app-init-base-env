"""Frozen-contract paths for the isolated control routes.

Release paths carry the version segment from the contract examples.
Heartbeat is ``runs/{run_id}:heartbeat``.
"""

from __future__ import annotations

import re

_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _segment(value: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        return ""
    return value


def route_path_for(operation: str, body: dict) -> str:
    task_id = _segment(str(body.get("task_id") or ""))
    run_id = _segment(str(body.get("run_id") or ""))
    version = str(body.get("release_version") or "")
    paths = {
        "policy.evaluate": "/api/v1/control/policy:evaluate",
        "run.claim": f"/api/v1/control/tasks/{task_id}:claim" if task_id else "",
        "run.renew": f"/api/v1/control/tasks/{task_id}:renew" if task_id else "",
        "run.start": f"/api/v1/control/runs/{run_id}:start" if run_id else "",
        "run.complete": f"/api/v1/control/runs/{run_id}:complete" if run_id else "",
        "run.release": f"/api/v1/control/runs/{run_id}:release" if run_id else "",
        "run.heartbeat": f"/api/v1/control/runs/{run_id}:heartbeat" if run_id else "",
        "run.reconcile": f"/api/v1/control/runs/{run_id}:reconcile" if run_id else "",
        "proxy.authorize": "/api/v1/control/identity/proxy:authorize",
        "proxy.rotate": "/api/v1/control/identity/proxy:rotate",
        "ci.status.write": "/api/v1/control/ci:status",
        "release.accept": f"/api/v1/control/releases/{version}:accept" if _VERSION.fullmatch(version) else "",
        "release.reject": f"/api/v1/control/releases/{version}:reject" if _VERSION.fullmatch(version) else "",
        "release.publish": f"/api/v1/control/releases/{version}:publish" if _VERSION.fullmatch(version) else "",
        "feedback.write": "/api/v1/control/feedback:write",
        "pull.read": "/api/v1/control/pull:read",
        "preview.merge": "/api/v1/control/preview:merge",
        "diagnostic.read": "/api/v1/control/diagnostics:read",
        "session.issue": "/api/v1/control/sessions:issue",
        "session.revoke": "/api/v1/control/sessions:revoke",
        "session.check": "/api/v1/control/sessions:check",
        "limit.consume": "/api/v1/control/limits:consume",
        "github.effect": "/api/v1/control/github-effects:submit",
        "host.privilege": "/api/v1/control/host:privilege",
        "signer.revoke": "/api/v1/control/identity/signers:revoke",
        "branch.read": "/api/v1/control/branch-protection",
    }
    return paths.get(operation, "")
