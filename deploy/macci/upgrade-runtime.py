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

INSTALL_ROOT = Path("/usr/local")
TRUSTED_INSTALLER = INSTALL_ROOT / "libexec/macmini-pr-tests/runtime-installer.py"
TRUSTED_INSTALLER_PIN = INSTALL_ROOT / "etc/macmini-pr-tests/runtime-installer.sha256"
SOURCE = Path.home() / "web-app-init-base-env-preview"
ROOT = Path.home() / ".local/share/macmini-pr-tests"
NAMES = ("runner.py", "merge_preview.py")
LOCKS = ("worker.lock", "preview-merge.lock", "main-publish.lock", "receipt-delivery.lock")
MAX_MODULE_BYTES = 1024 * 1024


def verify_trusted_installer(installer_path=None, pin_path=None, install_root=None, trusted_uid=0,
                             executed_path=None):
    """Require a root-owned, nonwritable installer and separately pinned digest."""
    installer_path = Path(installer_path or TRUSTED_INSTALLER)
    pin_path = Path(pin_path or TRUSTED_INSTALLER_PIN)
    install_root = Path(install_root or INSTALL_ROOT)
    executed_path = Path(executed_path or __file__)
    if executed_path.absolute() != installer_path.absolute():
        raise SystemExit("Refusing to run installer from a candidate checkout")
    if installer_path.absolute() != TRUSTED_INSTALLER.absolute() and install_root == INSTALL_ROOT:
        raise SystemExit("Refusing installer outside the fixed trusted path")
    for path in (installer_path, pin_path):
        try:
            relative = path.relative_to(install_root)
        except ValueError:
            raise SystemExit("Trusted installer path escapes its installation root")
        directory = install_root
        chain = [directory]
        for part in relative.parts[:-1]:
            directory = directory / part
            chain.append(directory)
        for directory in chain:
            try:
                info = directory.lstat()
            except OSError:
                raise SystemExit("Trusted installer path is unavailable")
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != trusted_uid
                    or info.st_mode & 0o022):
                raise SystemExit("Unsafe trusted installer directory")
    try:
        script_info = installer_path.lstat()
        pin_info = pin_path.lstat()
    except OSError:
        raise SystemExit("Trusted installer or digest pin is unavailable")
    if (not stat.S_ISREG(script_info.st_mode) or script_info.st_uid != trusted_uid
            or script_info.st_mode & 0o222 or script_info.st_size>2*1024*1024
            or not stat.S_ISREG(pin_info.st_mode)
            or pin_info.st_uid != trusted_uid or pin_info.st_mode & 0o222
            or pin_info.st_size > 128):
        raise SystemExit("Trusted installer and digest pin must be root-owned and read-only")
    try:
        fd = os.open(pin_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != trusted_uid
                    or opened.st_mode & 0o222 or opened.st_size > 128):
                raise SystemExit("Unsafe trusted installer digest pin")
            pin = stream.read(129).decode("ascii").strip()
        fd = os.open(installer_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != trusted_uid
                    or opened.st_mode & 0o222):
                raise SystemExit("Unsafe trusted installer file")
            digest = hashlib.sha256(stream.read()).hexdigest()
    except (OSError, UnicodeError):
        raise SystemExit("Could not verify trusted installer")
    if not re.fullmatch(r"[0-9a-f]{64}", pin) or digest != pin:
        raise SystemExit("Trusted installer digest does not match root-owned pin")
    return True


def git(source, *args):
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.check_output(
        ["git", "--no-replace-objects", "-C", str(source), *args],
        env=env, timeout=30,
    )


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
        entry = git(source, "ls-tree", "--full-tree", sha, "--", path).decode("ascii").rstrip("\n")
        if not re.fullmatch(r"100(?:644|755) blob [0-9a-f]{40}\t" + re.escape(path), entry):
            raise ValueError("Candidate module is not a regular Git blob: " + name)
        obj = entry.split()[2]
        size = int(git(source, "cat-file", "-s", obj))
        if not 0 < size <= MAX_MODULE_BYTES:
            raise ValueError("Candidate module size rejected: " + name)
        data = git(source, "cat-file", "blob", obj)
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
    verify_trusted_installer()
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
    source_info = SOURCE.lstat()
    if not stat.S_ISDIR(source_info.st_mode) or source_info.st_uid != os.getuid() or source_info.st_mode & 0o022:
        raise SystemExit("Unsafe MacCI source checkout")
    actual = git(SOURCE,"rev-parse","HEAD").decode("ascii").strip()
    if actual != args.sha:
        raise SystemExit("Checkout is not the exact reviewed SHA")
    changes = git(SOURCE,"status","--porcelain","--untracked-files=all","--ignored=no","--",".").decode("utf-8").strip()
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
