"""Report host capabilities this process can actually see.

A local unittest is not a MacCI receipt, a macOS host proof, or a public
running SHA. Missing tools stay BLOCKED. This probe does not start a
container or change a host account.
"""

from __future__ import annotations

import platform
import shutil


def host_capability() -> dict:
    system = platform.system()
    docker = shutil.which("docker")
    podman = shutil.which("podman")
    sandbox = shutil.which("sandbox-exec")
    missing = []
    if system != "Darwin":
        missing.append("macOS host")
    if not docker and not podman:
        missing.append("container runtime")
    if system == "Darwin" and not sandbox:
        missing.append("sandbox-exec")
    missing.append("MacCI receipt for this exact tree")
    missing.append("public running SHA")
    return {
        "status": "BLOCKED",
        "system": system,
        "docker": bool(docker),
        "podman": bool(podman),
        "sandbox_exec": bool(sandbox),
        "macci_receipt": False,
        "public_running_sha": None,
        "host_downgrade": False,
        "missing": missing,
        "side_effect": False,
    }
