"""Server-assigned principal. Every field is filled by the registry, never by the request."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    principal_id: str
    kind: str
    repo_id: str
    app_id: str
    producer_id: str
    stream_id: str
    scopes: frozenset[str]
    trusted_source: str

    def __post_init__(self) -> None:
        for name in ("principal_id", "kind", "repo_id", "app_id", "producer_id", "stream_id", "trusted_source"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(name)
        object.__setattr__(self, "scopes", frozenset(self.scopes))
