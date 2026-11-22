#!/usr/bin/env python3
"""One-time installation of a reviewed scheduler, without root or new credentials."""
import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

# Check before importing modules that use Python 3.10 typing syntax.
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10+ required. Run deploy/app-init/install-upgrade-scheduler.sh to select a compatible installed interpreter.")

import upgrade_scheduler as scheduler
import upgrade_pr as upgrade

HERE = Path(__file__).resolve().parent
FILES = ("upgrade_scheduler.py", "upgrade_pr.py", "upgrade_plan.py")
PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def run(*args):
    subprocess.run(args, check=True, timeout=120)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True, help="exact accepted foundation main SHA")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
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
    if not shutil.which("gh"):
        raise SystemExit("Existing authenticated GitHub CLI required")
    run("gh", "auth", "status", "--hostname", "github.com")
    run(sys.executable, "-B", str(HERE.parents[1] / "tests/test_upgrade_scheduler.py"))
    domain = "gui/" + str(os.getuid())
    run("launchctl", "print", domain)
    if not args.apply:
        print("Preflight passed; --apply installs a separate user LaunchAgent")
        return 0
    scheduler.private_root()
    with scheduler.lock("tick.lock"):
        root = scheduler.ROOT
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
            if any(not (runtime/name).is_file() or (runtime/name).is_symlink() or (runtime/name).read_bytes() != (HERE/name).read_bytes() for name in FILES) or plistlib.loads(plist.read_bytes()) != job:
                raise SystemExit("Existing scheduler differs; do not overwrite running software")
            print("Existing scheduler matches; no files or jobs replaced")
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
    print("Scheduler installed; explicitly register an app or initialize a new app with app-init.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
