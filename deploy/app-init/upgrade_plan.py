"""Deterministic, read-only three-way foundation upgrade planner.

Inputs are old/new *rendered* app templates and tracked app commit blobs.
Never execute a template, change a checkout, publish a ref or modify a database.
The next coordinator must verify the sources are accepted foundation commits.
"""
import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
from tempfile import TemporaryDirectory

MAX_FILE = 8 * 1024 * 1024
PROTECTED = {".app-init.json", "README.md", "app/project_info.json", "app/release_notes.json"}
ROOT_FILES = {".gitignore", "requirements.txt", "frontend/index.html", "frontend/package.json",
              "frontend/package-lock.json", "frontend/tsconfig.json", "frontend/vite.config.ts"}
APP_OWNED_PREFIXES = ("app_modules/", "frontend/src/app-modules/", "migrations/modules/")
MODULE_SEEDS = {"app_modules/__init__.py", "app_modules/registry.py"}
PREFIXES = ("app/", "frontend/src/", "scripts/", "deploy/macmini/", "migrations/", "tests/")


def app_owned(path):
    return isinstance(path, str) and path.casefold().startswith(APP_OWNED_PREFIXES)


def managed(path):
    p = PurePosixPath(path)
    if (not path or p.is_absolute() or ".." in p.parts or p.as_posix() != path
            or app_owned(path) or path in PROTECTED or any(x.startswith(".") for x in p.parts if path != ".gitignore")
            or any(x in {"__pycache__", "node_modules", "dist", "logs", "data", "secrets"} for x in p.parts)
            or p.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".p12"}):
        return False
    return path in ROOT_FILES or (path.startswith(PREFIXES) and p.suffix in {".py", ".ts", ".tsx", ".css", ".json", ".sql", ".sh", ".html", ".md"})


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


@dataclass(frozen=True)
class Decision:
    path: str
    action: str
    reason: str
    old: bytes | None
    new: bytes | None
    local: bytes | None
    result: bytes | None = None

    def summary(self):
        return {"path": self.path, "action": self.action, "reason": self.reason,
                "old_sha256": digest(self.old), "new_sha256": digest(self.new),
                "app_sha256": digest(self.local), "result_sha256": digest(self.result)}


def merge_text(old, new, local):
    if any(b"\0" in data for data in (old, new, local)):
        return None
    try:
        for data in (old, new, local):
            data.decode("utf-8")
    except UnicodeError:
        return None
    with TemporaryDirectory(prefix="foundation-merge-") as folder:
        paths = [Path(folder) / name for name in ("app", "old", "new")]
        for path, data in zip(paths, (local, old, new)):
            path.write_bytes(data)
        proc = subprocess.run(["git", "merge-file", "-p", *map(str, paths)],
                              capture_output=True, timeout=30)
        if proc.returncode != 0:
            return None
        return proc.stdout


# Only this already-templated host script may be resolved automatically.
# Runtime installers, service launchers other than manage.sh, and SQL migrations
# retain mandatory review; never auto-delete any host file.
SAFE_HOST_MERGE = frozenset({"deploy/macmini/manage.sh"})
HOST_IDENTITY_KEYS = ("LABEL_PREFIX", "PORT", "TRACK_BRANCH", "EXPECTED_ORIGIN")


def host_identity(content):
    """Protect the real deployed app's instance-specific service settings."""
    import re
    if content is None:
        return None
    lines = content.decode("utf-8", "strict").splitlines()
    values = {}
    for key in HOST_IDENTITY_KEYS:
        matches = [line for line in lines if re.fullmatch(key + r"=[^\r\n]+", line)]
        if len(matches) != 1:
            return None
        values[key] = matches[0].split("=", 1)[1].strip()
    return values


def resolve_host_script(path, old, new, current):
    """Only syntactically safe, identity-preserving, provable three-way changes.

    A conflict, local deletion, changed app identity, or unrecognized host
    script is never patched silently. Runs bash syntax checking only, not code.
    """
    if path not in SAFE_HOST_MERGE or old is None or new is None or current is None:
        return None
    if any(b"\0" in data for data in (old, new, current)):
        return None
    try:
        original = host_identity(current)
        if original is None:
            return None
        result = new if current == old else merge_text(old, new, current)
        if result is None or host_identity(result) != original:
            return None
        result.decode("utf-8", "strict")
    except (UnicodeError, ValueError):
        return None
    # -n validates syntax without running installer commands or making writes.
    try:
        check = subprocess.run(["bash", "-n"], input=result, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result if check.returncode == 0 else None


def plan(old, new, app):
    """Only paths owned by the rendered templates are eligible; preserve app-only files."""
    decisions = []
    for path in sorted(old.keys() | new.keys()):
        if not managed(path):
            continue
        before, after, current = old.get(path), new.get(path), app.get(path)
        if before == after or current == after:
            continue
        if path in SAFE_HOST_MERGE and before != after:
            result = resolve_host_script(path, before, after, current)
            if result is None:
                action, reason = "review", "host script requires review: unsafe merge or changed instance settings"
            else:
                action = "replace" if current == before else "merge"
                reason = "verified host script three-way merge, bash syntax and app identity"
        elif path.startswith(("deploy/macmini/", "migrations/")) or after is None:
            action, reason, result = "review", "host/migration/deletion requires explicit review", None
        elif current == before:
            action, reason, result = ("add" if before is None else "replace"), "app matches old template", after
        elif before is None or current is None:
            action, reason, result = "conflict", "overlapping add or local removal", None
        else:
            result = merge_text(before, after, current)
            if result is None:
                action, reason = "conflict", "overlapping or non-text edits"
            else:
                action, reason = "merge", "non-overlapping three-way merge"
        decisions.append(Decision(path, action, reason, before, after, current, result))
    return decisions


def module_seed_snapshot(root):
    """Read only exact starter files; never adopt business content as templates."""
    root = Path(root).resolve(strict=True)
    result = {}
    for relative in sorted(MODULE_SEEDS):
        path = root / relative
        if not path.exists() and not path.is_symlink():
            continue
        if path.parent.is_symlink() or path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE:
            raise ValueError("Unsafe application module seed: " + relative)
        result[relative] = path.read_bytes()
    return result


def module_seed_decisions(new_seeds, observed):
    """Seed missing files once; an existing exact path is always app-owned."""
    if not set(new_seeds).issubset(MODULE_SEEDS):
        raise ValueError("Unexpected application module seed")
    decisions = []
    for path, content in sorted(new_seeds.items()):
        if path in observed:
            if observed[path] not in {"100644", "100755"}:
                raise ValueError("Non-regular application module seed: " + path)
            continue
        if any(other.casefold() == path.casefold() or other.casefold().startswith(path.casefold()+"/") or path.casefold().startswith(other.casefold()+"/") for other in observed):
            raise ValueError("Application module seed path collision: " + path)
        decisions.append(Decision(path, "add", "initialize missing app-owned module seed", None, content, None, content))
    return decisions


def generated_snapshot(root):
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Template snapshot is not a directory")
    result = {}
    for folder, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"__pycache__", "node_modules", "dist", "logs", "data", "secrets"}]
        for name in names:
            path = Path(folder) / name
            relative = path.relative_to(root).as_posix()
            if not managed(relative):
                continue
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE:
                raise ValueError("Unsafe generated file: " + relative)
            result[relative] = path.read_bytes()
    return result


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], timeout=30)


def tracked_snapshot(root, ref, wanted):
    """Read exact committed regular blobs only, never working files or symlinks."""
    sha = git(root, "rev-parse", "--verify", ref + "^{commit}").decode().strip()
    entries = git(root, "ls-tree", "-rz", sha).split(b"\0")
    result = {}
    for entry in entries:
        if not entry:
            continue
        fields, name = entry.split(b"\t", 1)
        path = name.decode("utf-8", "surrogateescape")
        if path not in wanted or not managed(path):
            continue
        mode, kind, oid = fields.decode().split()
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ValueError("Non-regular app entry: " + path)
        size = int(git(root, "cat-file", "-s", oid))
        if size > MAX_FILE:
            raise ValueError("Oversized app file: " + path)
        result[path] = git(root, "cat-file", "blob", oid)
    return sha, result


def report(old, new, root, ref="HEAD"):
    sha, app = tracked_snapshot(root, ref, old.keys() | new.keys())
    decisions = plan(old, new, app)
    return {"schema": 1, "app_sha": sha, "read_only": True,
            "source_verification": "required before creating upgrade PR",
            "blocked": any(d.action in {"review", "conflict"} for d in decisions),
            "changes": [d.summary() for d in decisions]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-generated", required=True, type=Path)
    parser.add_argument("--new-generated", required=True, type=Path)
    parser.add_argument("--app", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(report(generated_snapshot(args.old_generated), generated_snapshot(args.new_generated), args.app), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
