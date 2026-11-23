"""Independent host scheduler for approved downstream repositories.

Creates draft upgrade PRs. Only explicitly approved Panel upgrades may be
promoted after exact MacCI success and independent safety verification. Never
deploys or touches installed application files, domains, databases or services.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from tempfile import NamedTemporaryFile

import upgrade_pr as upgrade
import upgrade_auto_merge as auto_merge

ROOT = Path.home() / ".local/share/macmini-foundation-upgrades"
INTERVAL = 300
LABEL = "com.layho.foundation-upgrades"
MODES = {"observe", "create"}
# Blocked plans must be re-evaluated after runtime upgrades or changing
# merge rules, even if the GitHub SHAs remain unchanged. Never cache blocked.
TERMINAL = {"current", "closed_pr"}


def private_root():
    ROOT.mkdir(parents=True, mode=0o700, exist_ok=True)
    st = ROOT.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise ValueError("Unsafe scheduler directory")


def read_json(path, default):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return default
    with os.fdopen(fd, "r", encoding="utf-8") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077 or st.st_size > 2_000_000:
            raise ValueError("Unsafe scheduler state")
        return json.load(f)


def write_json(path, data):
    with NamedTemporaryFile(dir=ROOT, delete=False, mode="w", encoding="utf-8") as f:
        name = Path(f.name)
        try:
            os.fchmod(f.fileno(), 0o600)
            json.dump(data, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
            f.close()
            os.replace(name, path)
        finally:
            name.unlink(missing_ok=True)


@contextmanager
def lock(name):
    private_root()
    fd = os.open(ROOT / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "r+") as f:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError("Unsafe scheduler lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def repository_valid(repo):
    return isinstance(repo, str) and upgrade.REPO.fullmatch(repo) and repo.lower() != upgrade.FOUNDATION.lower()


def registry():
    data = read_json(ROOT / "registry.json", {"schema": 1, "apps": {}})
    if data.get("schema") != 1 or not isinstance(data.get("apps"), dict) or len(data["apps"]) > 200:
        raise ValueError("Invalid scheduler registry")
    for repo, entry in data["apps"].items():
        if (not repository_valid(repo) or not isinstance(entry, dict)
                or entry.get("mode") not in MODES
                or (entry.get("legacy_base") is not None and not upgrade.SHA.fullmatch(entry["legacy_base"]))):
            raise ValueError("Invalid registered app")
    return data


def register(repo, mode, legacy_base=None, if_absent=False):
    if not repository_valid(repo) or mode not in MODES:
        raise ValueError("Invalid app registration")
    if legacy_base is not None and not upgrade.SHA.fullmatch(legacy_base):
        raise ValueError("Invalid legacy base")
    with lock("registry.lock"):
        data = registry()
        if if_absent and repo in data["apps"]:
            return
        data["apps"][repo] = {"mode": mode, "legacy_base": legacy_base}
        write_json(ROOT / "registry.json", data)


def status_path(repo):
    return ROOT / ("status-" + hashlib.sha256(repo.encode()).hexdigest() + ".json")


def open_upgrades(repo):
    return [p for p in upgrade.pull_rows(repo) if p.get("headRefName", "").startswith("base-upgrade/")]


def safe_url(repo, value):
    if isinstance(value, str) and re.fullmatch(r"https://github\.com/" + re.escape(repo) + r"/pull/[1-9]\d*", value):
        return value
    return None


def record(repo, mode, target, app_sha, state, **extra):
    item = {"schema": 1, "repository": repo, "mode": mode, "target_sha": target,
            "app_sha": app_sha, "state": state, "checked_at": datetime.now(timezone.utc).isoformat(),
            **extra}
    write_json(status_path(repo), item)
    print(json.dumps(item, ensure_ascii=False), flush=True)
    return item


def tick_app(repo, entry, target, force=False):
    mode = entry["mode"]
    try:
        app_sha = upgrade.remote_ref("git@github.com:" + repo + ".git", "refs/heads/main")
        if not app_sha:
            return record(repo, mode, target, None, "no_main")
        if mode == "observe":
            return record(repo, mode, target, app_sha, "waiting_adoption")
        # Any outstanding upgrade, even for an older target, prevents stacking.
        pending = open_upgrades(repo)
        if pending:
            # Never silently select a random PR when several older upgrade
            # candidates are open; this needs a human/agent review.
            if len(pending) != 1:
                return record(repo, mode, target, app_sha, "upgrade_review_required",
                              reason="multiple_pending_upgrades", count=len(pending))
            row = pending[0]
            url = safe_url(repo, row.get("url"))
            branch = row.get("headRefName", "")
            match = re.fullmatch(r"base-upgrade/([0-9a-f]{40})", branch)
            if repo not in auto_merge.APPROVED_REPOS:
                return record(repo, mode, target, app_sha, "upgrade_pending", pr_url=url)
            if not match or not isinstance(row.get("number"), int):
                return record(repo, mode, target, app_sha, "upgrade_review_required",
                              pr_url=url, reason="invalid_upgrade_branch")
            pending_target = match.group(1)
            if not auto_merge.official_foundation_target(pending_target):
                return record(repo, mode, target, app_sha, "upgrade_review_required",
                              pr_url=url, reason="unknown_or_nonancestor_target")
            # Old source target must not block further versions: safely merge
            # the exact previously tested candidate and re-check new main on
            # the next scheduled tick before planning the latest version.
            if auto_merge.promote(repo, row, app_sha, pending_target):
                return record(repo, mode, target, app_sha, "upgrade_merged",
                              pr_url=url, merged_now=True, promoted_target_sha=pending_target)
            return record(repo, mode, target, app_sha, "upgrade_waiting_verification",
                          pr_url=url, pending_target_sha=pending_target)
        previous = read_json(status_path(repo), {})
        if (not force and previous.get("state") in TERMINAL and previous.get("app_sha") == app_sha
                and previous.get("target_sha") == target and previous.get("mode") == mode
                and previous.get("legacy_base") == entry.get("legacy_base")):
            extra = {k: previous[k] for k in ("pr_url", "review_count", "legacy_base") if k in previous}
            return record(repo, mode, target, app_sha, previous["state"], **extra)
        result = upgrade.coordinate(repo, entry.get("legacy_base"), True, expected_target=target,
                                    expected_app=app_sha)
        state = result["state"]
        if state == "existing_pr":
            pr = result["pr"]
            state = "upgrade_pending" if pr.get("state") == "OPEN" else "closed_pr"
            url = safe_url(repo, pr.get("url"))
        else:
            url = safe_url(repo, result.get("url"))
        if state not in {"current", "blocked", "draft_created", "upgrade_pending", "closed_pr"}:
            raise ValueError("Unexpected coordinator result")
        flagged = []
        for change in result.get("changes", []):
            if not isinstance(change, dict) or change.get("action") not in {"review", "conflict"}:
                continue
            path, action = change.get("path"), change["action"]
            # The planner only reports tracked code paths. Do not expose
            # file content, token paths or arbitrary untrusted text.
            if (isinstance(path, str) and upgrade.planner.managed(path)
                    and len(path) <= 180 and len(flagged) < 100):
                flagged.append({"path": path, "action": action,
                                "reason": str(change.get("reason", ""))[:120]})
        return record(repo, mode, target, app_sha, state, pr_url=url, review_count=len(flagged),
                      blocked_files=flagged if state == "blocked" else [],
                      legacy_base=entry.get("legacy_base"))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        # Never log raw subprocess stderr or credentials; retry transient errors.
        return record(repo, mode, target, locals().get("app_sha"), "error")


def tick(force=False):
    # Explicitly opted-in installations may refresh the independently copied
    # host runtime only from accepted official main. This runs BEFORE the
    # scheduler's tick lock because the trusted installer acquires that lock.
    # Its subprocess cannot circumvent the application PR safety gates.
    updater = ROOT / "runtime" / "upgrade_runtime_auto.py"
    if updater.is_file() and not updater.is_symlink():
        try:
            subprocess.run([sys.executable, "-B", str(updater)],
                           check=False, timeout=360,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            pass
    # LaunchAgent must never ask for credentials or access an interactive terminal.
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    os.environ["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    os.environ["GH_PROMPT_DISABLED"] = "1"
    with lock("tick.lock"):
        with lock("registry.lock"):
            apps = registry()["apps"]
        target = upgrade.remote_ref(upgrade.SOURCE_URL, "refs/heads/main")
        if not target:
            raise ValueError("Official foundation main unavailable")
        for repo, entry in sorted(apps.items()):
            tick_app(repo, entry, target, force)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("register")
    r.add_argument("--repo", required=True)
    r.add_argument("--mode", choices=sorted(MODES), default="observe")
    r.add_argument("--legacy-base")
    r.add_argument("--if-absent", action="store_true", help="Preserve an existing registration")
    t = sub.add_parser("tick")
    t.add_argument("--force", action="store_true")
    sub.add_parser("list")
    args = parser.parse_args()
    try:
        if args.command == "register":
            register(args.repo, args.mode, args.legacy_base, if_absent=args.if_absent)
        elif args.command == "tick":
            tick(args.force)
        else:
            private_root()
            print(json.dumps(registry(), ensure_ascii=False, indent=2))
    except BlockingIOError:
        print("Scheduler busy; next scheduled invocation will retry", flush=True)
        return 0 if args.command == "tick" else 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print("Scheduler stopped: " + type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
