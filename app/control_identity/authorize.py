"""Default-deny operation matrix. Unlisted operations and roles fail closed."""

from __future__ import annotations

from .errors import ControlError

MATRIX = {
    "diagnostic.read": frozenset({"core_session", "app_service", "agent", "macci", "publisher", "host", "hostops", "user_acceptance"}),
    "feedback.write": frozenset({"core_session", "agent"}),
    "pull.read": frozenset({"app_service", "host"}),
    "run.claim": frozenset({"agent"}),
    "run.renew": frozenset({"agent"}),
    "run.start": frozenset({"agent"}),
    "run.complete": frozenset({"agent"}),
    "run.release": frozenset({"agent"}),
    "run.reconcile": frozenset({"agent", "host", "macci", "publisher", "user_acceptance", "candidate"}),
    "ci.status.write": frozenset({"macci"}),
    "preview.merge": frozenset({"host"}),
    "deploy.apply": frozenset({"host"}),
    "deploy.switch": frozenset({"host"}),
    "release.accept": frozenset({"user_acceptance"}),
    "release.reject": frozenset({"user_acceptance"}),
    "release.publish": frozenset({"publisher"}),
    "proxy.rotate": frozenset({"host"}),
    "proxy.authorize": frozenset({"host", "agent", "app_service"}),
    "policy.evaluate": frozenset({"core_session", "app_service", "agent", "macci", "publisher", "host", "hostops", "user_acceptance", "candidate"}),
    "session.issue": frozenset({"core_session", "agent", "host"}),
    "session.revoke": frozenset({"core_session", "host"}),
    "session.check": frozenset({"core_session", "agent", "host"}),
    "limit.consume": frozenset({"core_session", "agent", "app_service", "host"}),
    "github.effect": frozenset({"host", "agent", "macci", "publisher"}),
    "branch.read": frozenset({"host", "hostops", "core_session"}),
    "host.privilege": frozenset({"host"}),
    "policy.change": frozenset({"host"}),
    "db.migrate": frozenset({"host"}),
    "signer.revoke": frozenset({"host"}),
}


def authorize(principal: dict, operation: str, body: dict) -> None:
    allowed = MATRIX.get(operation)
    if allowed is None:
        raise ControlError("NOT_AUTHORIZED", 403, "operation is not allowed", reason="default_deny")
    kind = principal["kind"]
    if kind not in allowed:
        if operation == "release.publish" and kind == "candidate":
            return
        raise ControlError("NOT_AUTHORIZED", 403, "role is not allowed", reason="role")
    if operation == "diagnostic.read" and kind == "app_service":
        if str(body.get("target_app_id") or "") != str(principal.get("app_id") or ""):
            raise ControlError("NOT_AUTHORIZED", 403, "service identity cannot cross apps", reason="cross_app")
