"""Darwin launchd and Linux systemd adaptation contracts.

Suggested pilot cells stay unverified. This module does not run launchctl,
systemctl, brew, or apt, and it does not treat a plan as MacCI.
"""

import hashlib
import json
import re
from pathlib import Path

HELP = (
    "Isolated lifecycle plan. Darwin uses launchd. Linux uses systemd. "
    "Real install, uninstall, privilege escalation, and host commands stay denied. "
    "A synthetic test double is not host capability proof."
)

DIGEST = "sha256:" + ("11" * 32)
REQUIRED_DISK_BYTES = 1024 ** 3
REPO_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
DOMAIN_PATTERN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")
SHA_PATTERN = re.compile(r"[a-f0-9]{40}\Z")
FINGERPRINT_PATTERN = re.compile(r"sha256:[a-f0-9]{64}\Z")
USER_DATA = (".env", "data", ".postgres")

ADAPTERS = {
    "darwin-launchd": {
        "service_tool": "launchctl",
        "package_manager": "brew",
        "fd_namespace": "/dev/fd",
        "socket_root": "/private/tmp",
        "unit_kind": "plist",
        "service_manager": "launchd",
        "unit_dir_user": "~/Library/LaunchAgents",
        "unit_dir_system": "/Library/LaunchDaemons",
    },
    "linux-systemd": {
        "service_tool": "systemctl",
        "package_manager": "apt",
        "fd_namespace": "/proc/self/fd",
        "socket_root": "/run/user",
        "unit_kind": "systemd-unit",
        "service_manager": "systemd",
        "unit_dir_user": "~/.config/systemd/user",
        "unit_dir_system": "/etc/systemd/system",
    },
}

PROBE_FIELDS = ("os", "version", "arch", "distro", "package_manager", "service_manager")
DEPENDENCIES = ("git", "node", "npm", "initdb", "postgres", "pg_isready")


def slug_for(repo):
    return repo.lower().replace("/", "--")


def label_for(repo):
    return "com.appinit." + hashlib.sha256(repo.lower().encode("utf-8")).hexdigest()[:12]


def logical_install_path(home, repo):
    return Path(home) / "Projects" / "app-init" / slug_for(repo)


def legacy_install_path(home, repo):
    return Path(home) / "Applications" / "app-init" / slug_for(repo)


def service_unit_digest(adapter_id, repo, port, scope_level):
    body = {
        "adapter_id": adapter_id,
        "autostart": True,
        "label": label_for(repo),
        "port": port,
        "repo": repo.lower(),
        "scope_level": scope_level,
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def classify(probe):
    """Return (adapter_id, supported_unverified) or (None, False) when refused."""
    if any(not probe.get(field) for field in PROBE_FIELDS):
        return None, False
    os_name = probe["os"]
    version = probe["version"]
    arch = probe["arch"]
    distro = probe["distro"]
    packages = probe["package_manager"]
    manager = probe["service_manager"]
    if arch not in {"x86_64", "arm64"}:
        return None, False
    if os_name == "darwin" and distro == "macos" and packages == "brew" and manager == "launchd":
        return "darwin-launchd", True
    linux = os_name == "linux" and packages == "apt" and manager == "systemd"
    ubuntu = distro == "ubuntu" and version == "24.04"
    debian = distro == "debian" and version == "13"
    if linux and (ubuntu or debian):
        return "linux-systemd", True
    return None, False
