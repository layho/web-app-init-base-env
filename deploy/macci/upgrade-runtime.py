#!/usr/bin/env python3
"""Update only existing reviewed MacCI runtime modules, without restart."""
import argparse
from contextlib import ExitStack
import fcntl
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from tempfile import NamedTemporaryFile
from datetime import datetime, timezone

SOURCE = Path(__file__).resolve().parent
ROOT = Path.home() / ".local/share/macmini-pr-tests"
NAMES = ("runner.py", "merge_preview.py")
LOCKS = ("worker.lock", "preview-merge.lock", "main-publish.lock")


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--enable-recovery", action="store_true")
    parser.add_argument("--sha", required=True, help="exact reviewed source commit")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    source=args.source.resolve()
    os.umask(0o077)
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        raise SystemExit("Invalid SHA")
    actual = subprocess.check_output(["git","-C",str(source),"rev-parse","HEAD"], text=True).strip()
    if actual != args.sha:
        raise SystemExit("Checkout is not the exact reviewed SHA")
    changes = subprocess.check_output(["git","-C",str(source),"status","--porcelain","--","."],text=True).strip()
    if changes:
        raise SystemExit("MacCI source checkout contains changes; refuse upgrade")
    if not owned_private(ROOT, False):
        raise SystemExit("Existing private MacCI data directory required")
    for name in (*NAMES, "publish_main.py", "github-token"):
        if not owned_private(ROOT / name):
            raise SystemExit("Unsafe or missing existing file: " + name)
    subprocess.run([sys.executable,"-B","-m","unittest","-v","test_runner","test_merge_preview","test_publish_main"],
                   cwd=source,check=True)
    if args.check:
        print("Host source tests passed; no runtime changes")
        return 0
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
        desired={name:(source/name).read_bytes() for name in NAMES}
        if previous == desired:
            print("Runtime already matches reviewed source")
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
        if args.enable_recovery:
            # Explicit one-time enablement. Never auto-replace the trust anchor.
            for name,origin in (("runtime-installer.py",source/"upgrade-runtime.py"),("runtime-recovery.py",source/"runtime_recovery.py")):
                if (ROOT/name).exists():
                    if not owned_private(ROOT/name) or (ROOT/name).read_bytes()!=origin.read_bytes():
                        raise SystemExit("Existing recovery trust anchor differs; refuse replacement")
                else:
                    replace_file(ROOT/name,origin.read_bytes())
            replace_file(ROOT/"runtime-recovery.enabled",b"enabled\n")
        replace_file(ROOT/"runtime-installed-sha",(actual+"\n").encode())
        print("Runtime updated to reviewed SHA",actual)
        print("Backup:",backup)
        print("Existing launchd services will use new scripts on their next invocation.")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
