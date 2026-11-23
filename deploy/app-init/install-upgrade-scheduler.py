#!/usr/bin/env python3
"""One-time installation of a reviewed scheduler, without root or new credentials."""
import argparse
import getpass
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import stat
from datetime import datetime, timezone
from tempfile import NamedTemporaryFile

# Check before importing modules that use Python 3.10 typing syntax.
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10+ required. Run deploy/app-init/install-upgrade-scheduler.sh to select a compatible installed interpreter.")

import upgrade_scheduler as scheduler
import upgrade_pr as upgrade

HERE = Path(__file__).resolve().parent
FILES = ("upgrade_scheduler.py", "upgrade_pr.py", "upgrade_plan.py", "upgrade_auto_merge.py", "upgrade_runtime_auto.py")
PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def run(*args):
    subprocess.run(args, check=True, timeout=120)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True, help="exact accepted foundation main SHA")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--upgrade", action="store_true", help="Safely replace an existing reviewed scheduler runtime; requires --apply")
    parser.add_argument("--check-repo", required=True, help="downstream repo for API/SSH preflight")
    parser.add_argument("--enable-auto-runtime", action="store_true", help="Explicitly opt in to future accepted-main host updates (requires --apply)")
    args = parser.parse_args()
    os.umask(0o077)
    if args.upgrade and not args.apply:
        raise SystemExit("--upgrade requires --apply")
    if args.enable_auto_runtime and not args.apply:
        raise SystemExit("--enable-auto-runtime requires --apply")
    if not upgrade.SHA.fullmatch(args.sha):
        raise SystemExit("Invalid SHA")
    if upgrade.line(HERE, "rev-parse", "HEAD") != args.sha or upgrade.line(HERE, "status", "--porcelain", "--untracked-files=normal"):
        raise SystemExit("Use a clean checkout at the exact reviewed SHA")
    if upgrade.remote_ref(upgrade.SOURCE_URL, "refs/heads/main") != args.sha:
        raise SystemExit("Only current official main may install the scheduler")
    if sys.platform != "darwin" or os.getuid() == 0:
        raise SystemExit("Run as the existing Mac administrator user, without sudo")
    os.environ["PATH"] = PATH
    os.environ["GH_PROMPT_DISABLED"] = "1"
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    if not scheduler.repository_valid(args.check_repo):
        raise SystemExit("Invalid downstream repository")
    os.environ["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    try:
        if not upgrade.remote_ref("git@github.com:" + args.check_repo + ".git", "refs/heads/main"):
            raise ValueError("No downstream main")
        print("SSH repository access passed", flush=True)
        try:
            token = upgrade.read_token()
            existing_token = True
        except FileNotFoundError:
            token = getpass.getpass("Paste dedicated GitHub PR token (hidden): ").strip()
            existing_token = False
        if not upgrade.valid_token(token):
            raise ValueError("Invalid token format")
        identity = upgrade.github("/user", token=token)
        if not isinstance(identity, dict) or not identity.get("login"):
            raise ValueError("GitHub identity unavailable")
        upgrade.pull_rows(args.check_repo, token=token)
        print("GitHub REST token and downstream PR read access passed (PR write is verified by actual creation)", flush=True)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit("Preflight failed: " + (str(exc) if type(exc) is ValueError else type(exc).__name__)) from None
    run(sys.executable, "-B", str(HERE.parents[1] / "tests/test_upgrade_scheduler.py"))
    run(sys.executable, "-B", str(HERE.parents[1] / "tests/test_upgrade_auto_merge.py"))
    domain = "gui/" + str(os.getuid())
    run("launchctl", "print", domain)
    if not args.apply:
        print("Preflight passed; --apply installs a separate user LaunchAgent")
        return 0
    scheduler.private_root()
    with scheduler.lock("tick.lock"):
        root = scheduler.ROOT
        if not existing_token:
            fd = os.open(upgrade.TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(token + "\n")
                f.flush()
                os.fsync(f.fileno())
        del token
        runtime = root / "runtime"
        plist = Path.home() / "Library/LaunchAgents" / (scheduler.LABEL + ".plist")
        job = {"Label": scheduler.LABEL, "ProgramArguments": [sys.executable, str(runtime/"upgrade_scheduler.py"), "tick"],
               "WorkingDirectory": str(runtime), "StartInterval": scheduler.INTERVAL, "RunAtLoad": True,
               "EnvironmentVariables": {"HOME": str(Path.home()), "PATH": PATH, "GH_PROMPT_DISABLED": "1",
                                        "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"},
               "StandardOutPath": str(root/"scheduler.log"), "StandardErrorPath": str(root/"scheduler.stderr.log")}
        if runtime.exists() or plist.exists():
            if runtime.is_symlink() or plist.is_symlink() or not runtime.is_dir() or not plist.is_file():
                raise SystemExit("Partial or unsafe installation; inspect existing state")
            if plistlib.loads(plist.read_bytes()) != job:
                raise SystemExit("LaunchAgent configuration differs; never overwrite")
            known = ("upgrade_scheduler.py", "upgrade_pr.py", "upgrade_plan.py")
            originals = {}
            for name in known:
                candidate = runtime/name
                info = candidate.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise SystemExit("Unsafe existing runtime: " + name)
                originals[name] = candidate.read_bytes()
            for name in FILES:
                candidate = runtime/name
                if candidate.exists() or candidate.is_symlink():
                    info = candidate.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                        raise SystemExit("Unsafe runtime file: " + name)
            desired = {name: (HERE/name).read_bytes() for name in FILES}
            if all((runtime/name).is_file() and (runtime/name).read_bytes() == content for name,content in desired.items()):
                print("Existing scheduler matches; no files or jobs replaced")
            elif not args.upgrade:
                raise SystemExit("Existing runtime differs; run --apply --upgrade after review")
            else:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                backup = root / ("scheduler-backup-" + stamp)
                backup.mkdir(mode=0o700)
                for name, content in originals.items():
                    old = backup / name
                    old.write_bytes(content)
                    old.chmod(0o600)
                added = [name for name in FILES if not (runtime/name).exists()]
                def atomic_write(name, content):
                    with NamedTemporaryFile(dir=runtime, delete=False) as tmp:
                        temp = Path(tmp.name)
                        try:
                            os.fchmod(tmp.fileno(), 0o600)
                            tmp.write(content)
                            tmp.flush()
                            os.fsync(tmp.fileno())
                            tmp.close()
                            os.replace(temp, runtime/name)
                        finally:
                            temp.unlink(missing_ok=True)
                try:
                    for name, content in desired.items():
                        atomic_write(name, content)
                except OSError:
                    for name, content in originals.items():
                        atomic_write(name, content)
                    for name in added:
                        (runtime/name).unlink(missing_ok=True)
                    raise
                print("Scheduler runtime upgraded; existing LaunchAgent, token and registry preserved")
                print("Previous runtime backup:", backup)
        else:
            runtime.mkdir(mode=0o700)
            for name in FILES:
                shutil.copyfile(HERE/name, runtime/name)
                (runtime/name).chmod(0o600)
            plist.parent.mkdir(parents=True, exist_ok=True)
            with plist.open("xb") as f:
                plistlib.dump(job, f)
            plist.chmod(0o600)
            run("plutil", "-lint", str(plist))
        probe = subprocess.run(["launchctl","print",domain+"/"+scheduler.LABEL],capture_output=True,timeout=15)
        if probe.returncode != 0:
            run("launchctl","bootstrap",domain,str(plist))
    # Record the exact accepted main that supplied this installed host runtime.
    # Never modify app registry, GitHub token, or its LaunchAgent here.
    def atomic_control(path, data):
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise SystemExit("Unsafe existing runtime update control file")
        with NamedTemporaryFile(dir=root, delete=False) as tmp:
            temporary = Path(tmp.name)
            try:
                os.fchmod(tmp.fileno(), 0o600)
                tmp.write(data.encode("ascii"))
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp.close()
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
    atomic_control(root / "runtime-installed-sha", args.sha + "\n")
    if args.enable_auto_runtime:
        atomic_control(root / "runtime-auto-update.enabled", "enabled\n")
        print("Accepted-main runtime auto-upgrade explicitly enabled")
    print("Scheduler installed; explicitly register an app or initialize a new app with app-init.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
