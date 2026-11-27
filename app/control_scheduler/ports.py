"""Ports for the two interfaces this package is allowed to call.

W02 ``ControlIdentityService`` is the only authentication and lease surface:

- ``execute(operation, body, route_class=...)``
- ``trusted_fence(run_id, fenced=..., effect_known=...)``
- ``trusted_classify(run_id, fenced=..., classification=...)``
- ``register_candidate(repo=..., task=..., head=..., base=..., merge=..., tree=...)``

W01 ``Engine`` is the only event surface:

- ``append(body, crash=None)``
- ``query(request=..., stream_id=..., stream_epoch=..., after_seq=..., task_id=..., limit=...)``

This package does not import ``app.control_identity`` or ``app.control_events``.
Those modules are not in this isolated tree. A joint tree of W01
``33e65dc0925f03e60518a5c91a0e2254fd0fd12f`` and W02
``5dd0eb38e175a8a905836fbdbac5633ff9eb19ae`` has to supply the real objects.
Until then, callers pass test doubles. Doubles do not execute GitHub writes
and do not produce MacCI receipts.
"""

from __future__ import annotations

from typing import Protocol


class AuthResult(Protocol):
    status: int
    body: dict


class AuthPort(Protocol):
    def execute(self, operation: str, body: dict, *, route_class: str) -> AuthResult:
        """Match ``ControlIdentityService.execute``."""

    def trusted_fence(self, run_id: str, *, fenced: bool, effect_known: bool) -> dict:
        """Match ``ControlIdentityService.trusted_fence``. Client role is not an input."""

    def trusted_classify(self, run_id: str, *, fenced: bool, classification: str) -> dict:
        """Match ``ControlIdentityService.trusted_classify``."""

    def register_candidate(self, *, repo: str, task: str, head: str, base: str, merge: str, tree: str) -> str:
        """Match ``ControlIdentityService.register_candidate``."""


class EventPort(Protocol):
    def append(self, body: dict, *, crash: str | None = None) -> dict:
        """Match ``Engine.append``. The port owns sequence assignment and durability."""

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
        """Match ``Engine.query``. Reads have no side effect."""
