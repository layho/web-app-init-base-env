"""Stable repository identity.

The same store and the same repository generation keep one repo_id across
process restarts. A new generation (a rebuilt repository) cannot adopt that
repo_id, and a tampered binding is not rewritten into a new one.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets

from .atomic import atomic_write
from .errors import ControlError

PRODUCTION_PROTOCOL_ENABLED = False


def bound_token_for_directory(path: Path) -> str:
    """Token changes when the directory inode changes, as a rebuilt checkout does."""
    info = path.stat()
    return f"{info.st_dev}:{info.st_ino}"


def _seal(repo_id: str, bound_token: str, anchor: str) -> str:
    material = f"{repo_id}\n{bound_token}\n{anchor}".encode()
    return hashlib.sha256(material).hexdigest()


def _reject(message: str) -> ControlError:
    return ControlError("CANDIDATE_STALE", 409, message)


class RepositoryIdentity:
    def __init__(self, repo_id: str, bound_token: str, anchor: str):
        self.repo_id = repo_id
        self.bound_token = bound_token
        self.anchor = anchor

    @classmethod
    def open(cls, root: Path, bound_token: str) -> "RepositoryIdentity":
        if not isinstance(bound_token, str) or not bound_token or "/" in bound_token or "\n" in bound_token:
            raise _reject("repository generation token is missing")
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        path = root / "repo-identity.json"
        if not path.exists():
            anchor = secrets.token_hex(32)
            repo_id = "repo_" + hashlib.sha256(anchor.encode()).hexdigest()[:32]
            record = {
                "schema_version": 2,
                "repo_id": repo_id,
                "bound_token": bound_token,
                "anchor": anchor,
                "seal": _seal(repo_id, bound_token, anchor),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "production_protocol_enabled": False,
            }
            atomic_write(path, json.dumps(record, sort_keys=True).encode())
            os.chmod(path, 0o600)
            return cls(repo_id, bound_token, anchor)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise _reject("repository identity file cannot be read") from exc
        repo_id = record.get("repo_id")
        anchor = record.get("anchor")
        stored = record.get("bound_token")
        if not all(isinstance(item, str) and item for item in (repo_id, anchor, stored)):
            raise _reject("repository identity file is incomplete")
        if record.get("seal") != _seal(repo_id, stored, anchor):
            raise _reject("repository identity seal does not match this store")
        if stored != bound_token:
            raise _reject("repository generation does not match the stored identity")
        if record.get("production_protocol_enabled") is True:
            raise ControlError("NOT_AUTHORIZED", 403, "production protocol flag is not accepted")
        return cls(repo_id, bound_token, anchor)

    def assert_same(self, repo_id: str) -> None:
        if repo_id != self.repo_id:
            raise _reject("repo_id does not match the bound repository identity")
