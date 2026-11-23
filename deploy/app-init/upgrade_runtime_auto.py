"""Opt-in, fail-closed runtime updater for the Mac mini foundation scheduler.

Only an explicitly enabled, private scheduler installation may run this.
A candidate must be the exact official GitHub main SHA AND have a closed,
OWNER-accepted BASE-ACCEPTANCE issue. The existing trusted installer remains
the only mutator, including its regression tests, backup, and tick lock.
"""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.request import Request, build_opener, HTTPRedirectHandler

import upgrade_pr as upgrade
import upgrade_scheduler as scheduler

OFFICIAL = "git@github.com:layho/web-app-init-base-env.git"
API = "https://api.github.com/repos/layho/web-app-init-base-env/issues"
OPT_IN = scheduler.ROOT / "runtime-auto-update.enabled"
INSTALLED = scheduler.ROOT / "runtime-installed-sha"
PANEL = "layho/macmini-office-app-panel"
SHA = re.compile(r"[0-9a-f]{40}\Z")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def private_value(path):
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 256):
            raise ValueError("Unsafe runtime update control file")
        return path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return None


def candidate_accepted(sha):
    """The release was accepted, not merely pushed to the official branch."""
    if not SHA.fullmatch(sha):
        return False
    token = upgrade.read_token()
    for page in range(1, 11):
        req = Request(
            API + f"?state=closed&per_page=100&page={page}",
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "foundation-runtime-updater"},
        )
        with build_opener(NoRedirect()).open(req, timeout=20) as response:
            rows = json.load(response)
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("Invalid acceptance issue listing")
        for issue in rows:
            if (issue.get("title") != f"[BASE-ACCEPTANCE {sha}]"
                    or issue.get("state") != "closed"
                    or (issue.get("user") or {}).get("login") != "layho"
                    or (issue.get("closed_by") or {}).get("login") != "layho"
                    or issue.get("pull_request")):
                continue
            body = issue.get("body") or ""
            if not body.startswith("[BASE_ACCEPTANCE] "):
                continue
            try:
                data = json.loads(body[len("[BASE_ACCEPTANCE] "):])
            except ValueError:
                continue
            if (isinstance(data, dict) and data.get("schema") == 1
                    and data.get("candidate_sha") == sha
                    and data.get("tested_merge") == sha
                    and SHA.fullmatch(data.get("expected_main_sha") or "")
                    and SHA.fullmatch(data.get("test_head") or "")
                    and SHA.fullmatch(data.get("tree") or "")):
                return True
        if len(rows) < 100:
            return False
    return False


def run_once():
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    os.environ["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    os.environ["GH_PROMPT_DISABLED"] = "1"
    scheduler.private_root()
    if private_value(OPT_IN) != "enabled":
        return "disabled"
    current = private_value(INSTALLED)
    if current is not None and not SHA.fullmatch(current):
        raise ValueError("Invalid installed runtime SHA record")
    remote = upgrade.remote_ref(OFFICIAL, "refs/heads/main")
    if not remote or not SHA.fullmatch(remote):
        raise ValueError("No exact official main SHA")
    if current == remote:
        return "current"
    if not candidate_accepted(remote):
        return "unaccepted"
    # A separately checked-out immutable source is inspected by the existing
    # trusted installer. No remote code is executed before acceptance review.
    with TemporaryDirectory(prefix="foundation-runtime-") as folder:
        root = Path(folder) / "source"
        subprocess.run(["git", "clone", "--quiet", "--single-branch", "--branch", "main",
                        OFFICIAL, str(root)], check=True, timeout=120)
        observed = upgrade.line(root, "rev-parse", "HEAD")
        if observed != remote or upgrade.line(root, "status", "--porcelain", "--untracked-files=normal"):
            raise ValueError("Foundation main advanced during runtime check")
        if upgrade.remote_ref(OFFICIAL, "refs/heads/main") != remote:
            raise ValueError("Foundation changed; retry next tick")
        installer = root / "deploy/app-init/install-upgrade-scheduler.py"
        if not installer.is_file() or installer.is_symlink():
            raise ValueError("Reviewed installer unavailable")
        subprocess.run([sys.executable, "-B", str(installer),
                        "--sha", remote, "--check-repo", PANEL,
                        "--apply", "--upgrade"],
                       check=True, timeout=300, env={
                           **os.environ, "GIT_TERMINAL_PROMPT": "0",
                           "GH_PROMPT_DISABLED": "1",
                           "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
                       })
    return "upgraded"


def main():
    if sys.platform != "darwin":
        return 1
    try:
        state = run_once()
        if state == "upgraded":
            print("Official accepted foundation scheduler runtime upgraded")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError, TypeError):
        # Host scheduler continues its normal work; the updater will retry
        # without printing token, remote stderr or untrusted raw content.
        print("Runtime update unavailable; no override", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
