"""Platform file-descriptor namespaces.

W06's isolated diagnostics resolve containment through the hardcoded Linux
path ``/proc/self/fd``. That commit is not in this tree. Darwin's namespace
is ``/dev/fd``. A Linux resolution is not proof of a Darwin host, and neither
resolution is MacCI.
"""

W06_FD_PREFIX = "/proc/self/fd"


def fd_namespace(adapter_id):
    if adapter_id == "linux-systemd":
        return "/proc/self/fd"
    if adapter_id == "darwin-launchd":
        return "/dev/fd"
    raise ValueError(adapter_id)


def w06_proc_self_fd_compatible(adapter_id):
    return fd_namespace(adapter_id) == W06_FD_PREFIX


def containment_holds(directory, located):
    base = directory.rstrip("/")
    prefix = base + "/"
    return located.startswith(prefix)
