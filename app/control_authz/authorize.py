"""Default-deny binding of repo, app, producer, stream, operation, and scope.

``MATRIX`` is the role map. ``REQUIRED_SCOPES`` classifies every operation.
A missing or empty entry does not authorize an arbitrary scope. The presented
scope must be one of the classified scopes and the caller must hold it.
Conjunction operations still require the whole set. ``policy.evaluate`` and
``proxy.authorize`` present one classified scope; the target action or the
proxy check is separate. ``app_id`` and ``target_app_id`` are checked separately.
"""

from __future__ import annotations

from .errors import AuthzError
from .principal import Principal, ResourceBinding

_DIAGNOSTIC = frozenset({
    "core_session", "app_service", "agent", "macci", "publisher", "host", "hostops", "user_acceptance",
})
_EVENT = frozenset({*_DIAGNOSTIC, "policy_service"})
_REPORT_SUBMIT = frozenset({"agent", "macci", "publisher", "user_acceptance"})
_REPORT_READ = frozenset({*_DIAGNOSTIC, "policy_service"})

MATRIX = {
    "diagnostic.read": _DIAGNOSTIC,
    "diagnostic.cursor": _DIAGNOSTIC,
    "event.append": _EVENT,
    "event.read": _EVENT,
    "report.submit": _REPORT_SUBMIT,
    "report.read": _REPORT_READ,
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

_RUN = frozenset({"run"})
_CI = frozenset({"ci"})
_ACCEPT = frozenset({"accept"})
_DEPLOY = frozenset({"deploy"})
_SESSION = frozenset({"session"})

# These operations present one member of a classified set. The caller must
# hold that member. They do not have to hold every other member.
_ALTERNATIVE_SCOPE_OPERATIONS = frozenset({"proxy.authorize", "policy.evaluate"})

_OPERATION_SCOPES = {
    "feedback.write": frozenset({"feedback"}),
    "pull.read": frozenset({"pull"}),
    "preview.merge": frozenset({"merge"}),
    "release.publish": frozenset({"publish"}),
    "diagnostic.read": frozenset({"read"}),
    "diagnostic.cursor": frozenset({"read", "diagnostic.cursor"}),
    "event.append": frozenset({"event.append"}),
    "event.read": frozenset({"event.read"}),
    "report.submit": frozenset({"report.submit"}),
    "report.read": frozenset({"report.read"}),
    "run.claim": _RUN,
    "run.renew": _RUN,
    "run.start": _RUN,
    "run.complete": _RUN,
    "run.release": _RUN,
    "run.heartbeat": _RUN,
    "run.reconcile": _RUN,
    "ci.status.write": _CI,
    "deploy.apply": _DEPLOY,
    "deploy.switch": _DEPLOY,
    "release.accept": _ACCEPT,
    "release.reject": _ACCEPT,
    "proxy.rotate": frozenset({"rotate"}),
    "proxy.authorize": frozenset({"feedback", "pull", "merge", "publish", "read"}),
    "session.issue": _SESSION,
    "session.revoke": _SESSION,
    "session.check": _SESSION,
    "limit.consume": frozenset({"limit"}),
    "github.effect": frozenset({"run", "github.effect"}),
    "branch.read": frozenset({"branch.read"}),
    "host.privilege": frozenset({"host.privilege"}),
    "policy.change": frozenset({"policy.change"}),
    "db.migrate": frozenset({"db.migrate"}),
    "signer.revoke": frozenset({"signer.revoke"}),
}

REQUIRED_SCOPES = {
    **_OPERATION_SCOPES,
    "policy.evaluate": frozenset().union(*_OPERATION_SCOPES.values()),
}


def _deny(reason: str, detail: str) -> AuthzError:
    return AuthzError("NOT_AUTHORIZED", 403, detail, reason=reason)


def _scopes_hold(operation: str, presented: str, held: set[str]) -> bool:
    """True only for a classified scope the caller holds.

    A missing entry, an empty entry, or a scope that is not classified for
    the operation is a denial. Conjunction operations require every member.
    Alternative operations require the one presented member.
    """

    required = REQUIRED_SCOPES.get(operation)
    if not required or presented not in required or presented not in held:
        return False
    if operation in _ALTERNATIVE_SCOPE_OPERATIONS:
        return True
    return required <= held


def binding_for(principal: Principal, operation: str, body: dict) -> ResourceBinding:
    producer = body["producer_id"] or principal.producer_id
    stream = body["stream_id"] or principal.stream_id
    return ResourceBinding(
        repo_id=body["repo_id"],
        app_id=body["app_id"],
        producer_id=producer,
        stream_id=stream,
        operation=operation,
        scope=body["credential_scope"],
        task_id=body["task_id"],
        target_app_id=body["target_app_id"],
    )


def _bound_app(principal: Principal, named: str) -> None:
    if named == "":
        return
    if principal.app_id:
        if named != principal.app_id:
            raise _deny("cross_app", "service identity cannot cross apps")
        return
    raise _deny("cross_app", "principal has no app binding for that app")


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
    _bound_app(principal, resource_binding.app_id)
    _bound_app(principal, resource_binding.target_app_id)
    if resource_binding.producer_id != principal.producer_id:
        raise _deny("cross_producer", "producer is outside the server binding")
    if resource_binding.stream_id != principal.stream_id:
        raise _deny("cross_stream", "stream is outside the server binding")
    if not _scopes_hold(operation, resource_binding.scope, set(principal.scopes)):
        raise _deny("cross_scope", "operation scope is not interchangeable")
