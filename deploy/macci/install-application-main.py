#!/usr/bin/env python3
"""Reviewed, opt-in installer for the application main-only MacCI coordinator.

No PR, repository, token, or arbitrary command may be supplied by the web UI.
Staging does not register or run launchd. Enabling is a distinct host action.
"""
import argparse
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
from tempfile import NamedTemporaryFile

SOURCE = Path(__file__).resolve().parent
ROOT = Path.home() / ".local/share/macmini-pr-tests"
LABEL = "com.layho.macci-app-main-panel"
PLIST = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
REPO = "layho/macmini-office-app-panel"
MODULES = ("merge_application.py",)
DEPENDENCIES = ("merge_preview.py", "runner.py", "github-token")
SHA = re.compile(r"[0-9a-f]{40}\Z")


def private_file(path):
    try:
        info = path.lstat()
        return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and not stat.S_IMODE(info.st_mode) & 0o077)
    except OSError:
        return False


def private_dir(path):
    try:
        info = path.lstat()
        return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                and not stat.S_IMODE(info.st_mode) & 0o077)
    except OSError:
        return False


def checked_checkout(sha):
    if not SHA.fullmatch(sha):
        raise ValueError("Expected a 40-character accepted Git SHA")
    commands = (
        ("rev-parse", "HEAD"),
        ("rev-parse", "--abbrev-ref", "HEAD"),
        ("status", "--porcelain", "--untracked-files=no"),
    )
    results = [subprocess.check_output(
        ["git", "-C", str(SOURCE), *args], text=True).strip() for args in commands]
    if results != [sha, "main", ""]:
        raise ValueError("Require an unchanged, accepted main checkout at the exact SHA")


def preflight(sha, run_tests=True):
    if sys.platform != "darwin":
        raise ValueError("Mac mini macOS required")
    checked_checkout(sha)
    if not private_dir(ROOT) or any(not private_file(ROOT / f) for f in DEPENDENCIES):
        raise ValueError("Existing private MacCI installation or token is unsafe")
    for name in MODULES:
        if not (SOURCE / name).is_file() or (SOURCE / name).is_symlink():
            raise ValueError("Reviewed coordinator module missing")
    if run_tests:
        subprocess.run([sys.executable, "-B", "-m", "unittest", "-v",
                        "test_merge_application", "test_install_application_main"],
                       cwd=SOURCE, check=True)


def atomic_install(path, content):
    with NamedTemporaryFile(dir=path.parent, prefix=".app-main-stage-", delete=False) as out:
        temporary = Path(out.name)
        try:
            os.fchmod(out.fileno(), 0o700)
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
            out.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def stage(sha):
    preflight(sha)
    destination = ROOT / MODULES[0]
    if destination.exists():
        if not private_file(destination):
            raise ValueError("Refuse to replace an unsafe existing coordinator")
        if destination.read_bytes() != (SOURCE / MODULES[0]).read_bytes():
            raise ValueError("Existing coordinator differs; refuse overwrite without review")
        return "Already staged"
    atomic_install(destination, (SOURCE / MODULES[0]).read_bytes())
    return "Staged without launchd registration; no app PR merged"


def job(python=sys.executable):
    return {
        "Label": LABEL,
        "ProgramArguments": [python, str(ROOT / MODULES[0]), "--repo", REPO],
        "WorkingDirectory": str(ROOT),
        "StartInterval": 60,
        "RunAtLoad": False,
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "StandardOutPath": str(ROOT / "app-main-panel.log"),
        "StandardErrorPath": str(ROOT / "app-main-panel.stderr.log"),
    }


def enable(sha):
    preflight(sha)
    if not private_file(ROOT / MODULES[0]):
        raise ValueError("Stage the coordinator before enabling")
    if (ROOT / MODULES[0]).read_bytes() != (SOURCE / MODULES[0]).read_bytes():
        raise ValueError("Staged coordinator differs from reviewed accepted SHA")
    if PLIST.exists():
        raise ValueError("Existing LaunchAgent present; refuse to overwrite or double-install")
    domain = f"gui/{os.getuid()}"
    subprocess.run(["/bin/launchctl", "print", domain], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    PLIST.parent.mkdir(exist_ok=True)
    if PLIST.is_symlink():
        raise ValueError("LaunchAgents path is a symlink")
    data = plistlib.dumps(job())
    with NamedTemporaryFile(dir=PLIST.parent, prefix=".app-main-plist-", delete=False) as out:
        temporary = Path(out.name)
        try:
            os.fchmod(out.fileno(), 0o600)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
            out.close()
            if PLIST.exists():
                raise ValueError("Agent appeared concurrently; refuse overwrite")
            # Do not overwrite an existing agent: exclusive hard-link creation.
            os.link(temporary, PLIST)
        finally:
            temporary.unlink(missing_ok=True)
    # A syntax failure means launchd was never invoked: remove only our own
    # newly-created inode. Do not remove a concurrently replaced plist.
    created = PLIST.lstat()
    try:
        subprocess.run(["/usr/bin/plutil", "-lint", str(PLIST)], check=True)
    except (OSError, subprocess.CalledProcessError):
        try:
            current = PLIST.lstat()
            if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                PLIST.unlink()
        except FileNotFoundError:
            pass
        raise
    # Bootstrap may have created a job even if the command reports failure.
    # Keep its plist for inspection rather than leaving an untracked loaded
    # service or deleting another process's configuration.
    subprocess.run(["/bin/launchctl", "bootstrap", domain, str(PLIST)], check=True)
    return "Installed; host app main coordinator now scheduled"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--stage", action="store_true")
    mode.add_argument("--enable", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    if args.check:
        preflight(args.sha)
        print("Preflight and application merge tests passed; no services changed")
    elif args.stage:
        print(stage(args.sha))
    else:
        print(enable(args.sha))


if __name__ == "__main__":
    main()
