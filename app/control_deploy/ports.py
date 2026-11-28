"""Explicit ports for neighboring packages.

The isolated engine talks to W01 (authoritative deployment facts), W02
(authentication and leases), and W03 (immutable artifacts) only through
these interfaces. Nothing here copies those packages.
"""

import hashlib
import json
from typing import Protocol


class NotOccurred:
    """Trusted proof that one operation id was never dispatched.

    A missing lookup is not this proof. Only an authority that can attest
    the id was never started may return it, and the id has to match exactly.
    """

    def __init__(self, operation_id):
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("not_occurred proof requires an operation id")
        self.operation_id = operation_id
        self.status = "not_occurred"


def fact_digest(fact):
    """Digest of one deployment fact, ignoring a server receipt timestamp."""

    body = {key: value for key, value in fact.items() if key != "received_at"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class EventRejected(Exception):
    """W01 refused a deployment fact. The fact must not be rewritten into a new event."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Effect:
    """Result of one synthetic external call.

    status is ``ok``, ``failed``, or ``unknown``. ``unknown`` must be
    reconciled. It is not success and it is not a signal to retry.
    """

    def __init__(
        self,
        status,
        classification="none",
        detail="",
        local=None,
        public=None,
        resources_removed=None,
    ):
        if status not in {"ok", "failed", "unknown"}:
            raise ValueError("effect status must be ok, failed, or unknown")
        self.status = status
        self.classification = classification
        self.detail = detail
        self.local = local
        self.public = public
        self.resources_removed = resources_removed

    def as_pending(self, action):
        return {
            "action": action,
            "phase": "unknown" if self.status == "unknown" else "result",
            "status": self.status,
            "classification": self.classification,
            "detail": self.detail,
            "local": self.local,
            "public": self.public,
            "resources_removed": self.resources_removed,
        }


class LeasePort(Protocol):
    def check(
        self,
        *,
        token,
        principal_id,
        run_id,
        scope,
        generation,
        server_now,
    ):
        """Return True only for the current opaque token binding.

        A generation number by itself is not a token. A rejected check must
        not be reported by mutating the deployment into a new state.
        """


class AuthzPort(Protocol):
    def authenticate(self, actor):
        """Identity check. False stops the request before authorization."""

    def authorize(self, actor, scope, operation, candidate_id):
        """Authorization check. Runs only after authenticate succeeds."""

    def high_risk_allowed(self, actor, operation):
        """Dedicated authorization for high-risk enablement. Ordinary recovery does not call this."""

    def recovery_authorized(self, actor, deployment_id):
        """Preauthorized ordinary recovery of a frozen deployment."""

    def resume_authorized(self, actor, app_id, deployment_id):
        """Trusted background resume for one app and one deployment.

        Ordinary authentication does not grant this. A false result skips
        resume. The engine still limits the object to that app, host,
        service, and deployment.
        """


class EventPort(Protocol):
    def append_deployment(self, fact):
        """Accept one deployment fact into the W01 stream.

        The caller must not set received_at. A repeat of the same event_id
        and payload returns the original receipt. A different payload, a
        sequence gap, or a sequence conflict is rejected. This port is the
        only authority for accepted facts. A rejection or an unknown result
        is not an accepted publication.
        """

    def reconcile_deployment(self, fact):
        """Read-only proof for one fact that may already have been submitted.

        ``accepted`` requires the authoritative receipt, the full payload
        digest, and the same identity. ``not_accepted`` requires proof that
        the fact is absent and that repeating that exact payload is
        idempotent. Any other result is unknown and must not be appended.
        """


class ArtifactPort(Protocol):
    def put(self, digest, body):
        """Store immutable bytes. Different bytes for the same digest are rejected."""

    def read(self, digest):
        """Return stored bytes, or None when the digest is absent."""

    def bind(self, digest, candidate_id, service_id):
        """Bind a digest to one candidate and one service. The binding does not change."""


class ServicePort(Protocol):
    """Synthetic service. Implementations must not switch a real host service."""

    real_host: bool

    def stage(self, spec):
        ...

    def verify(self, spec):
        ...

    def switch(self, spec):
        ...

    def health(self, spec):
        ...

    def commit(self, spec):
        ...

    def rollback(self, spec):
        ...

    def observe(self, spec):
        """Read-only probe. Must not repeat stage, switch, rollback, or commit."""

    def operation_result(self, operation_id):
        """Read-only lookup for one stable operation id.

        Return the Effect already produced for that id. Return NotOccurred
        for that exact id only when this authority can prove the id was
        never dispatched. None, a missing row, or a corrupt row is not
        proof that the effect did not happen, and must not be followed by
        another call.
        """
