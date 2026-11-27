"""Explicit ports for neighboring packages.

The isolated engine talks to W01 (authoritative deployment facts), W02
(authentication and leases), and W03 (immutable artifacts) only through
these interfaces. Nothing here copies those packages.
"""

from typing import Protocol


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


class EventPort(Protocol):
    def append_deployment(self, fact):
        """Accept one deployment fact. The caller must not set received_at."""


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
        ...
