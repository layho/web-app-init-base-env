"""Fixture-only checks for an old install's files and a labeled rollback.

The inputs are temporary directories or in-memory file maps. Nothing here
starts a service, reads credentials, installs a runner, or emits a host receipt.
A passing result does not prove a macOS upgrade, process health, or database state.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

MAX_FILE = 8 * 1024 * 1024
STATEMENT = "夹具结果不是 macOS 实机回执、进程健康证明或发布授权。"
DOES_NOT_PROVE = (
    "macos-service-start",
    "process-health",
    "database-state",
    "production-receipt",
    "credential-isolation",
)
MAC_RECEIPT_FIELDS = (
    "old_sha",
    "candidate_sha",
    "host",
    "command",
    "exit_code",
    "health_check",
    "rolled_back_version",
    "file_digest",
)


def load_tree(root):
    """Read regular files. Symlinks and oversized files fail closed."""
    return _index(Path(root))


def record(files, label):
    """Bind a file map to a digest. The label must say this is a fixture."""
    if not isinstance(label, str) or "fixture" not in label:
        raise ValueError("record label must identify a fixture")
    normalized = _files(files)
    return {"label": label, "files": normalized, "digest": _digest(normalized)}


def plan_upgrade(old, updates, preserve):
    """Replace only non-preserved paths. Any preserved overlap leaves old unchanged."""
    current = _files(old)
    if not isinstance(updates, dict) or not isinstance(preserve, (set, frozenset, list, tuple)):
        return _result(False, current, ["invalid upgrade input"], "upgrade-refused")
    kept = set(preserve)
    rejected = []
    for path, content in updates.items():
        problem = _relative(path)
        if problem or not isinstance(content, bytes):
            rejected.append({"path": str(path), "reason": "unsafe-path"})
            continue
        if path in kept or _preserved_prefix(path, kept):
            rejected.append({"path": path, "reason": "business-file-preserved"})
    if rejected:
        return _result(False, current, rejected, "upgrade-refused")
    merged = dict(current)
    merged.update(updates)
    return _result(True, merged, [], "foundation-files-updated")


def note_start(outcome):
    """Record an injected outcome. This function does not launch a process."""
    if outcome not in {"fail", "ok"}:
        return {"running": False, "observed_process": False, "accepted": False, "reason": "unknown-outcome"}
    return {
        "running": False,
        "observed_process": False,
        "accepted": False,
        "injected_outcome": outcome,
        "reason": "start-failed" if outcome == "fail" else "start-not-observed",
    }


def rollback(recorded):
    """Restore the recorded fixture bytes when their digest still matches."""
    if not isinstance(recorded, dict) or recorded.get("digest") != _digest(_files(recorded.get("files", {}))):
        return _result(False, {}, ["rollback record digest mismatch"], "rollback-refused")
    if "fixture" not in str(recorded.get("label", "")):
        return _result(False, {}, ["rollback record is not a fixture"], "rollback-refused")
    restored = _files(recorded["files"])
    return _result(True, restored, [], "old-files-restored")


def materialize(files, root):
    """Write a file map into an empty directory without creating symlinks."""
    destination = Path(root)
    destination.mkdir()
    for path, content in sorted(_files(files).items()):
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            raise ValueError("materialize target already exists")
        target.write_bytes(content)
    return destination


def receipt_gap():
    """Name the fields a later Mac run must fill. Values stay empty."""
    return {
        "authoritative": False,
        "statement": STATEMENT,
        "fields": {name: None for name in MAC_RECEIPT_FIELDS},
        "does_not_prove": list(DOES_NOT_PROVE),
    }


def _result(ok, files, rejected, reason):
    normalized = _files(files) if ok else _files(files)
    return {
        "ok": ok,
        "authoritative": False,
        "statement": STATEMENT,
        "reason": reason,
        "files": normalized,
        "digest": _digest(normalized) if ok else None,
        "rejected": rejected,
        "does_not_prove": list(DOES_NOT_PROVE),
    }


def _files(files):
    if not isinstance(files, dict):
        raise ValueError("files must be a map")
    normalized = {}
    for path, content in files.items():
        if _relative(path) or not isinstance(content, bytes):
            raise ValueError("unsafe file map")
        normalized[path] = content
    return normalized


def _preserved_prefix(path, preserve):
    return any(path.startswith(item) for item in preserve if item.endswith("/"))


def _digest(files):
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[path])
        digest.update(b"\0")
    return digest.hexdigest()


def _relative(path):
    if not isinstance(path, str) or path == "" or path.startswith("/") or "\\" in path or "\x00" in path:
        return "unsafe-path"
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return "unsafe-path"
    return None


def _index(root):
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("snapshot is not a directory")
    result = {}
    for folder, dirs, names in os.walk(root, followlinks=False):
        kept = []
        for name in list(dirs):
            directory = Path(folder) / name
            if directory.is_symlink():
                raise ValueError("symlink in fixture: " + directory.relative_to(root).as_posix())
            kept.append(name)
        dirs[:] = kept
        for name in names:
            path = Path(folder) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise ValueError("symlink in fixture: " + relative)
            if _relative(relative) or not path.is_file():
                raise ValueError("unsafe fixture file: " + relative)
            if path.stat().st_size > MAX_FILE:
                raise ValueError("oversized fixture file: " + relative)
            result[relative] = path.read_bytes()
    return result
