"""Read-only ports for W01, W02, W04, and W07.

Default ports are BLOCKED. They are not stores and they do not invent
runtime evidence. Fixture ports are test doubles: they answer only the
decisions or events the caller supplies, and every fixture result is
synthetic with ``runtime_evidence`` false.
"""

from datetime import datetime, timedelta, timezone


class ServerClock:
    """Injectable server clock. Caller timestamps cannot move it."""

    def __init__(self, now):
        if now.tzinfo is None:
            raise ValueError("server clock requires an aware datetime")
        self._now = now.astimezone(timezone.utc)

    def now(self):
        return self._now

    def advance(self, seconds):
        self._now += timedelta(seconds=seconds)
        return self._now


class SideEffectProbe:
    """Records mutate attempts. Diagnostic reads must leave ``calls`` empty."""

    def __init__(self):
        self.calls = []

    def deploy(self, *args, **kwargs):
        self.calls.append("deploy")
        raise AssertionError("deploy")

    def retry(self, *args, **kwargs):
        self.calls.append("retry")
        raise AssertionError("retry")

    def repair(self, *args, **kwargs):
        self.calls.append("repair")
        raise AssertionError("repair")

    def write_issue(self, *args, **kwargs):
        self.calls.append("write_issue")
        raise AssertionError("write_issue")

    def write_comment(self, *args, **kwargs):
        self.calls.append("write_comment")
        raise AssertionError("write_comment")


def _blocked(interface, reason):
    return {
        "status": "BLOCKED",
        "interface": "pending",
        "port": interface,
        "runtime_evidence": False,
        "synthetic": False,
        "evidence_state": "missing",
        "events": None,
        "state": None,
        "reason": reason,
    }


class BlockedAuthorizationPort:
    calls = ()

    def authenticate(self, principal):
        return {
            "allowed": False,
            "http": 403,
            "code": "BLOCKED",
            "interface": "pending",
            "evidence_state": "missing",
            "principal_id": None,
            "reason": "W02 authentication interface is not defined on this isolated tree.",
        }

    def authorize(self, principal_id, app_id, action):
        return {
            "allowed": False,
            "http": 403,
            "code": "BLOCKED",
            "interface": "pending",
            "evidence_state": "missing",
            "principal_id": principal_id,
            "app_id": app_id,
            "reason": "W02 authorization interface is not defined on this isolated tree.",
        }


class BlockedEventPort:
    def read(self, **kwargs):
        return _blocked(
            "w01",
            "W01 authoritative event interface is not defined on this isolated tree.",
        )


class BlockedSchedulePort:
    def read(self, **kwargs):
        return _blocked(
            "w04",
            "W04 scheduling interface is not defined on this isolated tree.",
        )


class BlockedDeploymentPort:
    def read(self, **kwargs):
        return _blocked(
            "w07",
            "W07 deployment event interface is not defined on this isolated tree.",
        )


class Grant:
    def __init__(self, principal_id, app_id, action, expires_at=None):
        self.principal_id = principal_id
        self.app_id = app_id
        self.action = action
        self.expires_at = expires_at


class FixtureAuthorizationPort:
    """Test double. Decisions live in the caller-supplied grant list only."""

    def __init__(self, grants, clock):
        self.grants = list(grants)
        self.clock = clock
        self.calls = []

    def authenticate(self, principal):
        self.calls.append("authenticate")
        if not isinstance(principal, dict):
            return self._deny("NOT_AUTHORIZED", "missing")
        if principal.get("kind") != "test":
            return self._deny("NOT_AUTHORIZED", "unknown")
        proof = principal.get("proof_ref")
        if not isinstance(proof, str) or not proof.startswith("test-proof://"):
            return self._deny("NOT_AUTHORIZED", "unknown")
        expires_at = _parse_time(principal.get("expires_at")) if principal.get("expires_at") else None
        if principal.get("expires_at") and expires_at is None:
            return self._deny("NOT_AUTHORIZED", "unknown")
        if expires_at is not None and expires_at <= self.clock.now():
            return self._deny("NOT_AUTHORIZED", "expired")
        principal_id = principal.get("id")
        if not isinstance(principal_id, str) or not principal_id:
            return self._deny("NOT_AUTHORIZED", "unknown")
        return {
            "allowed": True,
            "http": 200,
            "code": None,
            "interface": "test_double",
            "evidence_state": "present",
            "principal_id": principal_id,
            "runtime_evidence": False,
            "synthetic": True,
        }

    def authorize(self, principal_id, app_id, action):
        self.calls.append(("authorize", principal_id, app_id, action))
        now = self.clock.now()
        for grant in self.grants:
            if grant.principal_id != principal_id or grant.action != action:
                continue
            if grant.expires_at is not None and grant.expires_at <= now:
                return {
                    "allowed": False,
                    "http": 403,
                    "code": "POLICY_DENIED",
                    "interface": "test_double",
                    "evidence_state": "expired",
                    "principal_id": principal_id,
                    "app_id": app_id,
                    "runtime_evidence": False,
                    "synthetic": True,
                    "reason": "The diagnostic grant is expired.",
                }
            if grant.app_id != app_id:
                continue
            return {
                "allowed": True,
                "http": 200,
                "code": None,
                "interface": "test_double",
                "evidence_state": "present",
                "principal_id": principal_id,
                "app_id": app_id,
                "runtime_evidence": False,
                "synthetic": True,
            }
        return {
            "allowed": False,
            "http": 403,
            "code": "POLICY_DENIED",
            "interface": "test_double",
            "evidence_state": "missing",
            "principal_id": principal_id,
            "app_id": app_id,
            "runtime_evidence": False,
            "synthetic": True,
            "reason": "No diagnostic grant covers this application.",
        }

    def _deny(self, code, evidence_state):
        return {
            "allowed": False,
            "http": 403,
            "code": code,
            "interface": "test_double",
            "evidence_state": evidence_state,
            "principal_id": None,
            "runtime_evidence": False,
            "synthetic": True,
        }


class FixtureEventPort:
    def __init__(self, events):
        self.events = list(events)
        self.calls = 0

    def read(self, **kwargs):
        self.calls += 1
        app_id = kwargs.get("app_id")
        run_id = kwargs.get("run_id")
        matched = []
        for event in self.events:
            if event.get("app_id") != app_id:
                continue
            if run_id is not None and event.get("run_id") != run_id:
                continue
            matched.append(dict(event, synthetic=True, runtime_evidence=False))
        return {
            "status": "test_double",
            "interface": "w01",
            "runtime_evidence": False,
            "synthetic": True,
            "evidence_state": "present" if matched else "missing",
            "events": matched,
            "state": None,
            "reason": "Synthetic W01 double. Not runtime evidence.",
        }


class FixtureSchedulePort:
    def __init__(self, state="UNKNOWN", evidence_state="unknown"):
        self.state = state
        self.evidence_state = evidence_state
        self.calls = 0

    def read(self, **kwargs):
        self.calls += 1
        return {
            "status": "test_double",
            "interface": "w04",
            "runtime_evidence": False,
            "synthetic": True,
            "evidence_state": self.evidence_state,
            "events": None,
            "state": self.state,
            "reason": "Synthetic W04 double. Not runtime evidence.",
        }


class FixtureDeploymentPort:
    def __init__(self, state=None, evidence_state="missing"):
        self.state = state
        self.evidence_state = evidence_state
        self.calls = 0

    def read(self, **kwargs):
        self.calls += 1
        return {
            "status": "test_double",
            "interface": "w07",
            "runtime_evidence": False,
            "synthetic": True,
            "evidence_state": self.evidence_state,
            "events": None,
            "state": self.state,
            "reason": "Synthetic W07 double. Not runtime evidence.",
        }


def _parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)
