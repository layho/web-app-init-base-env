"""Delivery journal on a caller-supplied directory.

This is the W05 protocol journal. It is not an authentication store and it
is not the W01 event authority. Callers pass a temporary directory in tests.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - this package is exercised on Linux
    fcntl = None

_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,200}")


class Journal:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.root / ".journal.lock"

    def _path(self, kind, key):
        if not isinstance(kind, str) or not _KEY.fullmatch(kind):
            raise ValueError("unsafe kind")
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise ValueError("unsafe key")
        path = (self.root / kind / (key + ".json")).resolve()
        if path.parent != (self.root / kind).resolve():
            raise ValueError("path escape")
        return path

    def _locked(self):
        return _Lock(self._lock_path)

    def read(self, kind, key):
        path = self._path(kind, key)
        with self._locked():
            if not path.exists():
                return None
            return json.loads(path.read_text(encoding="utf-8"))

    def write(self, kind, key, doc):
        path = self._path(kind, key)
        payload = json.dumps(doc, sort_keys=True, indent=2) + "\n"
        with self._locked():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, path)

    def list(self, kind):
        folder = self.root / kind
        with self._locked():
            if not folder.is_dir():
                return []
            docs = []
            for path in sorted(folder.glob("*.json")):
                docs.append(json.loads(path.read_text(encoding="utf-8")))
            return docs

    def append_trace(self, entry):
        path = self.root / "trace.jsonl"
        line = json.dumps(entry, sort_keys=True) + "\n"
        with self._locked():
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def traces(self):
        path = self.root / "trace.jsonl"
        with self._locked():
            if not path.exists():
                return []
            rows = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if line:
                    rows.append(json.loads(line))
            return rows


class _Lock:
    def __init__(self, path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.handle = self.path.open("a+")
        if fcntl is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handle is not None:
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
        return False
