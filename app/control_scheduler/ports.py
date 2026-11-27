"""Ports for the two interfaces this package is allowed to call.

W02 ``ControlIdentityService`` is the authentication and lease surface.
The scheduler normalizes a secret-free request first. The adapter's
``sign`` runs only after that document is complete, and then ``execute``
sees the signed request. ``sign`` must not change business fields.

- ``sign(operation, normalized)``
- ``execute(operation, body, route_class=...)``
- ``trusted_fence(run_id, fenced=..., effect_known=...)``
- ``trusted_classify(run_id, fenced=..., classification=...)``
- ``register_candidate(repo=..., task=..., head=..., base=..., merge=..., tree=...)``
- ``verified_record(kind, **binding)`` reads a server acceptance, test, publish, or intervention object
- ``observe_ancestry(main_sha=..., preview_sha=..., observed_tree=..., candidate_tree=...)``
- ``bind_subject(subject_id, ...)`` gives the adapter the scheduler's own subject state before authorization
- ``reject_acceptance(...)`` records a user rejection of one exact candidate

W01 ``Engine`` is the event surface. The scheduler persists an event intent,
then the adapter signs that finished document and ``append`` delivers it.
``query`` pages from a caller-supplied cursor. This package does not assign
W01's storage, and it does not mark delivery complete until ``query``
returns the same event identity.

- ``sign(normalized)``
- ``append(body, crash=None)``
- ``query(request=..., stream_id=..., stream_epoch=..., after_seq=..., task_id=..., limit=...)``

This package does not import ``app.control_identity`` or ``app.control_events``.
Those modules are not in this isolated tree. A joint tree of W01
``33e65dc0925f03e60518a5c91a0e2254fd0fd12f`` and W02
``5dd0eb38e175a8a905836fbdbac5633ff9eb19ae`` has to supply the real objects.
Until then, callers pass test doubles. A double is not a signature oracle
for an unfinished request. Doubles do not execute GitHub writes and do not
produce MacCI receipts. Passing these tests does not verify that joint tree.
"""

from __future__ import annotations

from typing import Protocol


class AuthResult(Protocol):
    status: int
    body: dict


class AuthPort(Protocol):
    def sign(self, operation: str, normalized: dict) -> dict:
        """Attach a fresh proof to a finished request. Do not change business fields."""

    def execute(self, operation: str, body: dict, *, route_class: str) -> AuthResult:
        """Match ``ControlIdentityService.execute``. ``body`` is already signed."""

    def trusted_fence(self, run_id: str, *, fenced: bool, effect_known: bool) -> dict:
        """Match ``ControlIdentityService.trusted_fence``. Client role is not an input."""

    def trusted_classify(self, run_id: str, *, fenced: bool, classification: str) -> dict:
        """Match ``ControlIdentityService.trusted_classify``."""

    def register_candidate(self, *, repo: str, task: str, head: str, base: str, merge: str, tree: str) -> str:
        """Match ``ControlIdentityService.register_candidate``."""

    def verified_record(self, kind: str, **binding) -> dict:
        """Return one server-side evidence object bound to an exact subject."""

    def observe_ancestry(self, *, main_sha: str, preview_sha: str, observed_tree: str, candidate_tree: str) -> dict:
        """Return the server ancestry observation. A caller boolean is not this method."""

    def bind_subject(self, subject_id: str, **state) -> None:
        """Remember the scheduler's subject state for the next authorization."""

    def reject_acceptance(self, **binding) -> dict:
        """Record that the independent user signer rejected this exact candidate."""


class EventPort(Protocol):
    def sign(self, normalized: dict) -> dict:
        """Attach a fresh proof to a finished event document. Do not change its identity."""

    def append(self, body: dict, *, crash: str | None = None) -> dict:
        """Match ``Engine.append``. The port owns sequence checks and durability."""

    def query(
        self,
        *,
        request: dict,
        stream_id: str,
        stream_epoch: int,
        after_seq: int = 0,
        task_id: str | None = None,
        limit: int = 100,
    ) -> dict:
        """Match ``Engine.query``. Reads have no side effect. Honor ``after_seq`` and ``limit``."""
