"""Result values for the isolated delivery loop.

Codes that appear in the W00 contract keep that contract's HTTP meaning.
Delivery-local reasons travel in the body and do not add a second policy store.
"""

from __future__ import annotations


class Result:
    def __init__(
        self,
        *,
        ok,
        http,
        code,
        state,
        changed,
        body=None,
        blocked=False,
        pending=False,
        replayed=False,
    ):
        self.ok = bool(ok)
        self.http = int(http)
        self.code = code
        self.state = state
        self.changed = bool(changed)
        self.body = dict(body or {})
        self.blocked = bool(blocked)
        self.pending = bool(pending)
        self.replayed = bool(replayed)

    def as_dict(self):
        return {
            "ok": self.ok,
            "http": self.http,
            "code": self.code,
            "state": self.state,
            "changed": self.changed,
            "body": self.body,
            "blocked": self.blocked,
            "pending": self.pending,
            "replayed": self.replayed,
        }
