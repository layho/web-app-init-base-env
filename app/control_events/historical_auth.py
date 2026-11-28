"""Historical authorization and revocation.

The decision is the applied grant and revocation sequence. A writable
``authorized`` flag is not an allow. A signature that already belonged to
another revision, or to a revocation, is not a later grant.
"""

from __future__ import annotations


def historical_authorization(events: list[dict]) -> dict:
    """Return authorized, revoked, or unknown from the event sequence.

    ``authorized`` on an event is ignored. At one revision a revocation
    wins over a grant. A higher revision replaces a lower one. Reusing a
    signature from a revocation, or from a different revision, is unknown.
    """

    usable = []
    scopes = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("event_type")
        if kind not in {"authorization.granted", "authorization.revoked"}:
            continue
        if event.get("_authoritative") is not True or not isinstance(event.get("signature"), str) or not event["signature"]:
            return {"decision": "unknown", "reason": "unverified_event", "revision": None}
        scope = tuple(event.get(field) for field in ("repo_id", "app_id", "task_id", "candidate_id", "operation", "stream_id", "stream_epoch"))
        if not all(isinstance(value, str) and value for value in scope[:-1]) or type(scope[-1]) is not int:
            return {"decision": "unknown", "reason": "incomplete_scope", "revision": None}
        scopes.add(scope)
        revision = event.get("revision")
        seq = event.get("producer_seq")
        if type(revision) is not int or revision < 0 or type(seq) is not int or seq < 1:
            return {"decision": "unknown", "reason": "invalid_event_coordinates", "revision": None}
        usable.append(event)
    if not usable:
        return {"decision": "unknown", "reason": "no_authorization_event", "revision": None}
    if len(scopes) != 1:
        return {"decision": "unknown", "reason": "mixed_scope", "revision": None}
    ordered = sorted(usable, key=_rank)
    winner = ordered[-1]
    signature = winner.get("signature")
    if isinstance(signature, str) and signature:
        winner_revision = int(winner.get("revision") or 0)
        for event in ordered:
            if event is winner or event.get("signature") != signature:
                continue
            other = event.get("revision")
            other_revision = int(other) if type(other) is int and not isinstance(other, bool) else 0
            if event.get("event_type") == "authorization.revoked" or other_revision != winner_revision:
                return {
                    "decision": "unknown",
                    "reason": "stale_signature",
                    "revision": winner_revision,
                }
    decision = "revoked" if winner.get("event_type") == "authorization.revoked" else "authorized"
    return {
        "decision": decision,
        "reason": "event_sequence",
        "revision": int(winner.get("revision") or 0),
        "event_id": winner.get("event_id"),
    }


def _rank(event: dict) -> tuple:
    revision = event.get("revision")
    seq = event.get("producer_seq")
    # A revocation dominates every grant at the same revision.
    revocation = 1 if event.get("event_type") == "authorization.revoked" else 0
    return (int(revision), revocation, int(seq))
