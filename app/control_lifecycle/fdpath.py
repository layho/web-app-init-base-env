"""Platform file-descriptor namespaces.

W06's isolated diagnostics resolve containment through the hardcoded Linux
path ``/proc/self/fd``. That commit is not in this tree. Darwin's namespace
is ``/dev/fd``. A Linux resolution is not proof of a Darwin host, and neither
resolution is MacCI.

``containment_holds`` is a string-prefix check for the synthetic Darwin
fixture. A ``/dev/fd/<n>`` string table is not safe path resolution, on
Darwin or anywhere else. Real W06 adaptation and two-platform host
verification remain outside this package.
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


def string_map_is_safe_path_resolution(adapter_id, mapping):
    """Return whether a /dev/fd string map may resolve a path.

    It never may. The mapping is ignored on purpose.
    """

    del adapter_id, mapping
    return False
