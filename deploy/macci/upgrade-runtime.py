#!/usr/bin/env python3
"""Update only existing reviewed MacCI runtime modules, without restart."""
import argparse
from contextlib import ExitStack
import fcntl
import os
from pathlib import Path
import re
import hashlib
import stat
import subprocess
import sys
from tempfile import NamedTemporaryFile
from datetime import datetime, timezone

SOURCE = Path(__file__).resolve().parent
ROOT = Path.home() / ".local/share/macmini-pr-tests"
NAMES = ("runner.py", "merge_preview.py")
LOCKS = ("worker.lock", "preview-merge.lock", "main-publish.lock", "receipt-delivery.lock")
MAX_MODULE_BYTES = 1024 * 1024


def owned_private(path, file=True):
    info = path.lstat()
    kind = stat.S_ISREG if file else stat.S_ISDIR
    return kind(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077


def replace_file(path, content):
    with NamedTemporaryFile(dir=path.parent, delete=False) as out:
        temp = Path(out.name)
        try:
            os.fchmod(out.fileno(), 0o700)
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
            out.close()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def read_candidate_modules(source, sha):
    """Read immutable Git blobs as data; never import or run candidate modules."""
    result = {}
    for name in NAMES:
        path = "deploy/macci/" + name
        entry = subprocess.check_output(
            ["git", "-C", str(source), "ls-tree", "--full-tree", sha, "--", path], timeout=30
        ).decode("ascii").rstrip("\n")
        if not re.fullmatch(r"100(?:644|755) blob [0-9a-f]{40}\t" + re.escape(path), entry):
            raise ValueError("Candidate module is not a regular Git blob: " + name)
        obj = entry.split()[2]
        size = int(subprocess.check_output(
            ["git", "-C", str(source), "cat-file", "-s", obj], timeout=30))
        if not 0 < size <= MAX_MODULE_BYTES:
            raise ValueError("Candidate module size rejected: " + name)
        data = subprocess.check_output(
            ["git", "-C", str(source), "cat-file", "blob", obj], timeout=30)
        if len(data) != size:
            raise ValueError("Candidate module size changed: " + name)
        # Compile for syntax only. No execution, imports, or bytecode files.
        compile(data, name, "exec", dont_inherit=True)
        result[name] = data
    return result


def verify_reviewed_digests(modules, runner_digest, merge_digest):
    expected = {"runner.py": runner_digest, "merge_preview.py": merge_digest}
    for name in NAMES:
        digest = expected[name]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Independent reviewed SHA-256 required for " + name)
        if hashlib.sha256(modules[name]).hexdigest() != digest:
            raise ValueError("Reviewed module digest mismatch: " + name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True, help="exact reviewed source commit")
    parser.add_argument("--runner-sha256", help="independently reviewed runner.py digest")
    parser.add_argument("--merge-sha256", help="independently reviewed merge_preview.py digest")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        raise SystemExit("Invalid SHA")
    actual = subprocess.check_output(["git","-C",str(SOURCE),"rev-parse","HEAD"], text=True).strip()
    if actual != args.sha:
        raise SystemExit("Checkout is not the exact reviewed SHA")
    changes = subprocess.check_output(["git","-C",str(SOURCE),"status","--porcelain","--","."],text=True).strip()
    if changes:
        raise SystemExit("MacCI source checkout contains changes; refuse upgrade")
    if not owned_private(ROOT, False):
        raise SystemExit("Existing private MacCI data directory required")
    for name in (*NAMES, "publish_main.py", "github-token"):
        if not owned_private(ROOT / name):
            raise SystemExit("Unsafe or missing existing file: " + name)
    desired = read_candidate_modules(SOURCE, args.sha)
    if args.check:
        print("Candidate module syntax checked only; no runtime changes or test approval")
        return 0
    # This explicit gate is not a substitute for independent MacCI evidence or authorization.
    # Only a locally trusted installer and independently reviewed digests may be used.
    verify_reviewed_digests(desired, args.runner_sha256, args.merge_sha256)
    if sys.platform != "darwin":
        raise SystemExit("Apply is restricted to existing macOS runtime")
    with ExitStack() as stack:
        for name in LOCKS:
            fd = os.open(ROOT/name, os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
            stream = stack.enter_context(os.fdopen(fd,"r+"))
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise SystemExit("Unsafe lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit("MacCI job active; no files changed; retry when idle")
        previous={name:(ROOT/name).read_bytes() for name in NAMES}
        if previous == desired:
            print("Runtime already matches reviewed source; no changes")
            return 0
        stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup=ROOT/("runtime-backup-"+stamp)
        backup.mkdir(mode=0o700)
        for name,content in previous.items():
            replace_file(backup/name, content)
        try:
            for name,content in desired.items():
                replace_file(ROOT/name, content)
        except OSError:
            for name,content in previous.items():
                replace_file(ROOT/name,content)
            raise
        print("Runtime updated to reviewed SHA",actual)
        print("Backup:",backup)
        print("Existing launchd services will use new scripts on their next invocation.")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
