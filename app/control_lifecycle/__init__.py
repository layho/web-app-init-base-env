"""Isolated V0.6.0 W09 lifecycle package.

Plans and diagnoses install, service, and cleanup contracts for Darwin
launchd and Linux systemd. It does not mount ``app.control_routes_w09``,
does not edit shared installers, and does not perform a real install,
uninstall, privilege change, or host command. Synthetic host results are
test doubles and are not MacCI or production proof.
"""

from app.control_lifecycle.fdpath import fd_namespace, w06_proc_self_fd_compatible
from app.control_lifecycle.flags import CAPABILITIES
from app.control_lifecycle.grant import Grant
from app.control_lifecycle.host import SyntheticHost
from app.control_lifecycle.platform import DIGEST, REQUIRED_DISK_BYTES, logical_install_path, service_unit_digest
from app.control_lifecycle.service import LifecycleService

__all__ = [
    "CAPABILITIES",
    "DIGEST",
    "Grant",
    "LifecycleService",
    "REQUIRED_DISK_BYTES",
    "SyntheticHost",
    "fd_namespace",
    "logical_install_path",
    "service_unit_digest",
    "w06_proc_self_fd_compatible",
]
