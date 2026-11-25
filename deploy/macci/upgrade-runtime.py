#!/usr/bin/env python3
"""Update only existing reviewed MacCI runtime modules, without restart."""
import argparse
import plistlib
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


def recovery_plist():
    return plistlib.dumps({
        "Label":"com.layho.macci-runtime-recovery",
        "ProgramArguments":[sys.executable,"-B",str(ROOT/"runtime-recovery.py")],
        "RunAtLoad":True,"StartInterval":60,
        "StandardOutPath":str(ROOT/"runtime-recovery.log"),
        "StandardErrorPath":str(ROOT/"runtime-recovery.err"),
        "EnvironmentVariables":{"PATH":"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        "WorkingDirectory":str(ROOT),
    })

def install_service():
    label="com.layho.macci-runtime-recovery"
    folder=Path.home()/"Library/LaunchAgents"
    if not folder.is_dir() or folder.is_symlink() or folder.stat().st_uid!=os.getuid():
        raise ValueError("Existing user LaunchAgents directory required")
    path=folder/(label+".plist")
    content=recovery_plist()
    if path.exists() or path.is_symlink():
        if not owned_private(path) or path.read_bytes()!=content:
            raise ValueError("Existing recovery service differs")
    else:
        replace_file(path,content)
    domain=f"gui/{os.getuid()}"
    if subprocess.run(["launchctl","print",domain+"/"+label],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
        subprocess.run(["launchctl","bootstrap",domain,str(path)],check=True)
    subprocess.run(["launchctl","print",domain+"/"+label],stdout=subprocess.DEVNULL,check=True)

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
    for name in (*NAMES, "publish_main.py"):
        if not owned_private(ROOT / name):
            raise SystemExit("Unsafe or missing existing file: " + name)
    subprocess.run([sys.executable,"-B","-m","unittest","-v","test_runner","test_merge_preview","test_publish_main","test_runtime_recovery"],
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
        anchors=(("runtime-installer.py",source/"upgrade-runtime.py"),("runtime-recovery.py",source/"runtime_recovery.py"))
        if args.enable_recovery:
            for name,origin in anchors:
                if (ROOT/name).exists() or (ROOT/name).is_symlink():
                    if not owned_private(ROOT/name) or (ROOT/name).read_bytes()!=origin.read_bytes():
                        raise SystemExit("Existing recovery trust anchor differs; refuse replacement")
        previous={name:(ROOT/name).read_bytes() for name in NAMES}
        desired={name:(source/name).read_bytes() for name in NAMES}
        if previous == desired:
            print("Runtime already matches reviewed source")
        stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup=ROOT/("runtime-backup-"+stamp)
        backup.mkdir(mode=0o700)
        for name,content in previous.items():
            replace_file(backup/name, content)
        old_marker=(ROOT/"runtime-installed-sha")
        prior_marker=old_marker.read_bytes() if old_marker.exists() and owned_private(old_marker) else None
        try:
            for name,content in desired.items():
                replace_file(ROOT/name,content)
            if args.enable_recovery:
                for name,origin in anchors:
                    if not (ROOT/name).exists():
                        replace_file(ROOT/name,origin.read_bytes())
                # Enable only after launchd registration succeeds.
                install_service()
            replace_file(old_marker,(actual+"\n").encode())
            if args.enable_recovery:
                replace_file(ROOT/"runtime-recovery.enabled",b"enabled\n")
        except (OSError,ValueError,subprocess.SubprocessError):
            for name,content in previous.items():
                replace_file(ROOT/name,content)
            if prior_marker is None:
                old_marker.unlink(missing_ok=True)
            else:
                replace_file(old_marker,prior_marker)
            raise
        print("Runtime updated to reviewed SHA",actual)
        print("Backup:",backup)
        print("Existing launchd services will use new scripts on their next invocation.")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
