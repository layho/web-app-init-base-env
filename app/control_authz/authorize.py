"""Default-deny binding of repo, app, producer, stream, operation, and scope."""

from __future__ import annotations

from .errors import AuthzError
from .principal import Principal, ResourceBinding

MATRIX = {
    "diagnostic.read": frozenset({"core_session", "app_service", "agent", "macci", "publisher", "host", "hostops", "user_acceptance"}),
    "feedback.write": frozenset({"core_session", "agent"}),
    "pull.read": frozenset({"app_service", "host"}),
    "run.claim": frozenset({"agent"}),
    "run.renew": frozenset({"agent"}),
    "run.start": frozenset({"agent"}),
    "run.complete": frozenset({"agent"}),
    "run.release": frozenset({"agent"}),
    "run.heartbeat": frozenset({"agent"}),
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
    "session.revoke": frozenset({"core_session", "host", "agent"}),
    "session.check": frozenset({"core_session", "agent", "host"}),
    "limit.consume": frozenset({"core_session", "agent", "app_service", "host"}),
    "github.effect": frozenset({"host", "agent", "macci", "publisher"}),
    "branch.read": frozenset({"host", "hostops", "core_session"}),
    "host.privilege": frozenset({"host"}),
    "policy.change": frozenset({"host"}),
    "db.migrate": frozenset({"host"}),
    "signer.revoke": frozenset({"host"}),
}

REQUIRED_SCOPE = {
    "feedback.write": "feedback",
    "pull.read": "pull",
    "preview.merge": "merge",
    "release.publish": "publish",
    "diagnostic.read": "read",
}


def _deny(reason: str, detail: str) -> AuthzError:
    return AuthzError("NOT_AUTHORIZED", 403, detail, reason=reason)


def binding_for(principal: Principal, operation: str, body: dict) -> ResourceBinding:
    producer = body["producer_id"] or principal.producer_id
    stream = body["stream_id"] or principal.stream_id
    target_app = body["target_app_id"] or body["app_id"]
    return ResourceBinding(
        repo_id=body["repo_id"],
        app_id=body["app_id"],
        producer_id=producer,
        stream_id=stream,
        operation=operation,
        scope=body["credential_scope"],
        task_id=body["task_id"],
        target_app_id=target_app,
    )


def authorize(principal: Principal, operation: str, resource_binding: ResourceBinding) -> None:
    if not isinstance(principal, Principal) or not isinstance(resource_binding, ResourceBinding):
        raise _deny("default_deny", "authorization requires a server principal")
    if not principal.principal_id or not principal.kind:
        raise _deny("empty_authorizer", "empty authorizer is denied")
    if resource_binding.operation != operation or not operation:
        raise _deny("default_deny", "operation is not allowed")
    allowed = MATRIX.get(operation)
    if allowed is None:
        raise _deny("default_deny", "operation is not allowed")
    if principal.kind not in allowed:
        if not (operation == "release.publish" and principal.kind == "candidate"):
            raise _deny("role", "role is not allowed")
    if not principal.repo_id or principal.repo_id != resource_binding.repo_id:
        raise _deny("cross_repo", "repository is outside the server binding")
    granted_tasks = {item[5:] for item in principal.scopes if item.startswith("task:") and len(item) > 5}
    if not resource_binding.task_id or resource_binding.task_id not in granted_tasks:
        raise _deny("cross_task", "task is outside the server binding")
    requested_app = resource_binding.target_app_id or resource_binding.app_id
    if principal.app_id:
        if requested_app and requested_app != principal.app_id:
            raise _deny("cross_app", "service identity cannot cross apps")
    elif requested_app:
        raise _deny("cross_app", "principal has no app binding for that app")
    if resource_binding.producer_id != principal.producer_id:
        raise _deny("cross_producer", "producer is outside the server binding")
    if resource_binding.stream_id != principal.stream_id:
        raise _deny("cross_stream", "stream is outside the server binding")
    required = REQUIRED_SCOPE.get(operation)
    if required is not None and (resource_binding.scope != required or required not in principal.scopes):
        raise _deny("cross_scope", "operation scope is not interchangeable")
