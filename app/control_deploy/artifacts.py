"""Content-addressed bytes for the isolated engine.

This is the local double behind ArtifactPort. It is not the W03 package.
"""

import json
import os
from pathlib import Path

from app.control_deploy.contract_machine import digest_of, is_digest


class ImmutableConflict(Exception):
    def __init__(self, digest):
        super().__init__(digest)
        self.digest = digest


class FilesystemArtifacts:
    def __init__(self, root):
        self.root = Path(root)
        self.directory = self.root / "artifacts"
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, digest):
        if not is_digest(digest):
            raise ImmutableConflict(digest)
        return self.directory / digest[7:]

    def _bind_path(self, digest):
        return self._path(digest).with_suffix(".bind.json")

    def put(self, digest, body):
        if not isinstance(body, bytes):
            raise TypeError("artifact body must be bytes")
        if digest_of(body) != digest:
            raise ImmutableConflict(digest)
        path = self._path(digest)
        if path.exists():
            if path.read_bytes() != body:
                raise ImmutableConflict(digest)
            return
        temporary = path.with_suffix(".tmp")
        with temporary.open("wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def read(self, digest):
        path = self._path(digest)
        if not path.exists() or not path.is_file():
            return None
        return path.read_bytes()

    def bind(self, digest, candidate_id, service_id):
        self._path(digest)
        path = self._bind_path(digest)
        record = {
            "digest": digest,
            "candidate_id": candidate_id,
            "service_id": service_id,
        }
        if path.exists():
            current = json.loads(path.read_text(encoding="utf-8"))
            if current != record:
                raise ImmutableConflict(digest)
            return current
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
        return record
