"""Phase-1 isolated build construction for W03 items A04, A05, and A06.

This module prepares a candidate build in a caller-supplied temporary
directory. It is not wired into ``deploy/macci/runner.py`` or
``deploy/macmini/manage.sh``. Those host paths stay unchanged until W07.

The W03 runtime target is macOS. The existing MacCI container boundary is
the ``docker run`` invocation in ``deploy/macci/runner.py`` (``--network
none``, ``--cap-drop ALL``, ``no-new-privileges``, ``--pids-limit 256``,
``--memory 3g``, ``--cpus 2``). This module maps that boundary and does not
edit ``runner.py``. On a host that cannot exercise it, the MacCI runtime is
``BLOCKED`` with a precise reason. Linux ``unshare`` is only an optional
adapter. It is not MacCI evidence and it is not a reason to run an
untrusted candidate on the host. When neither adapter can isolate, the
result is ``BLOCKED`` and the candidate is not started.

The optional adapter applies process, CPU, address-space, file-size, and
output limits itself. A timeout is not finished until the isolated process
tree has been confirmed dead. Output is read with a byte cap. Unbounded stdout capture is not used.
Build products are copied to
``.w03-artifacts`` and hashed again from that copy. A digest of a tree that
was then deleted is not the success path.

``host_acl_effective`` and ``db_acl_effective`` stay false. A probe on this
machine is not MacCI evidence and is not an A04 or A05 real-machine
measurement. ``production_verification_enabled`` stays false.

W02 alignment is pending. The public entry is ``app/control_authz``.
``app.auth.authorize_request`` is not that entry. This module does not
implement a second authenticator. ``Actor`` is a tests-only double.
The self-reported executor label ``Grok`` is not a trusted execution
identity.
"""

from __future__ import annotations

import copy
import ctypes
import errno
import hashlib
import json
import os
import re
import resource
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

SCHEMA_VERSION = 3
BASELINE_SHA = "b2011968a9c14845b89fe6d15dda4a595699e9ec"
W02_BASELINE_HEAD = "5dd0eb38e175a8a905836fbdbac5633ff9eb19ae"
W02_ALIGNMENT_STATUS = "pending_final_review"
W02_PUBLIC_AUTH = "app/control_authz"
EXECUTOR = "Grok"
BINDING_VERSION = "w03-binding-v2"
RUNTIME_TARGET = "macos"
# Observed in deploy/macci/runner.py test_candidate. This module does not edit that file.
MACCI_CONTAINER_BOUNDARY = {
    "source": "deploy/macci/runner.py test_candidate",
    "network": "none",
    "cap_drop": "ALL",
    "security_opt": "no-new-privileges",
    "pids_limit": "256",
    "memory": "3g",
    "cpus": "2",
    "run_timeout_seconds": 1200,
}
# The optional Linux adapter enforces these itself. They are not a MacCI receipt.
# nproc is an rlimit, not the MacCI cgroup pids limit. CPU is seconds, not --cpus.
LIMIT_NPROC = 16
LIMIT_CPU_SECONDS = 2
LIMIT_AS_BYTES = 128 * 1024 * 1024
LIMIT_FSIZE_BYTES = 256 * 1024
LIMIT_OUTPUT_BYTES = 32 * 1024
SYNTHETIC_CREDENTIAL_NAME = "W03_SYNTHETIC_MARK"
SYNTHETIC_CREDENTIAL_VALUE = "synthetic-w03-credential"
EVIDENCE_REF = "test-proof://w03/isolated-build"
FIXED_PATH = "/usr/bin:/bin"
FIXED_LANG = "C.UTF-8"
DEFAULT_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "PYTHONDONTWRITEBYTECODE", "TMPDIR")
FIXED_ENV_KEYS = frozenset(DEFAULT_ENV_ALLOWLIST)
DATABASE_MODES = frozenset({"denied", "mock", "temporary"})
ARTIFACT_MODES = frozenset({0o644, 0o755})
MAX_ARTIFACTS = 16
MAX_ARTIFACT_BYTES = 64 * 1024
MAX_PLAN_STEPS = 16
MAX_SCRIPT_CHARS = 2000
HOLD_TIMEOUT_SECONDS = 10
SANDBOX_TIMEOUT_SECONDS = 10
_CONTRACT_CODES = frozenset({
    "NOT_AUTHORIZED", "POLICY_DENIED", "PRODUCTION_DB_FORBIDDEN",
    "EVIDENCE_MISSING", "CONFLICT_REVISION", "RESERVATION_CONFLICT",
    "IDEMPOTENCY_PAYLOAD_MISMATCH", "SCHEMA_UNSUPPORTED", "SIDE_EFFECT_UNKNOWN",
    "EXECUTION_FAILED",
})
_MS_RDONLY = 1
_MS_NOSUID = 2
_MS_NODEV = 4
_MS_NOEXEC = 8
_MS_BIND = 4096
_MS_REC = 16384
_MS_PRIVATE = 1 << 18
_MS_REMOUNT = 32

# Names observed in the frozen baseline. Values are never read or stored.
CREDENTIAL_ENV_NAMES = frozenset({
    "APP_GITHUB_TOKEN",
    "APP_GITHUB_PR_TOKEN",
    "APP_GITHUB_FEEDBACK_TOKEN",
    "APP_API_TOKEN",
    "APP_ADMIN_PASSWORD",
    "APP_ADMIN_PASSWORD_HASH",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "SSH_AUTH_SOCK",
    "NPM_TOKEN",
    "NODE_AUTH_TOKEN",
    "DOCKER_AUTH_CONFIG",
})
CREDENTIAL_NAME_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "PRIVATE_KEY")
HIDDEN_OR_CREDENTIAL_PARTS = frozenset({
    ".env", ".npmrc", ".netrc", ".ssh", ".aws", ".gnupg", ".npm", ".docker",
    "github-token", "id_rsa", "id_ed25519",
})
FORBIDDEN_PARENT_PARTS = frozenset({
    ".ssh", ".aws", ".gnupg", ".npm", ".docker", "macmini-github",
})
TMP_ROOTS = frozenset({"/tmp", "/private/tmp", "/var/tmp", "/private"})

DETAIL_CODES = frozenset({
    "NONE",
    "HOST_TOKEN_INHERITANCE_DENIED",
    "ENV_NOT_ALLOWLISTED",
    "ENV_VALUES_REJECTED",
    "CREDENTIAL_ARTIFACT_DENIED",
    "CROSS_APP_DENIED",
    "SCRIPT_EXECUTION_DENIED",
    "LIFECYCLE_SCRIPT_DENIED",
    "WORKSPACE_ESCAPE_DENIED",
    "DATABASE_DENIED",
    "DIGEST_MISMATCH",
    "PERMISSION_DENIED",
    "NETWORK_DENIED",
    "CLEANUP_FAILED",
    "CONCURRENCY_CONFLICT",
    "HOLD_TIMEOUT",
    "ISOLATION_BLOCKED",
    "UNKNOWN_RESULT",
    "SCRIPT_FAILED",
    "RESOURCE_LIMIT",
    "ARTIFACT_NOT_VERIFIED",
    "INPUT_MISSING",
    "WORKSPACE_UNAVAILABLE",
    "SCHEMA_INVALID",
    "IDENTITY_REJECTED",
    "REVISION_CONFLICT",
})

W02_ALIGNMENT_PENDING = (
    "Replace the tests-only Actor double with the W02 app/control_identity principal: generation, scope, and no stored secret.",
    "Call the single public auth entry app/control_authz before idempotency. app.auth.authorize_request is not that entry. Do not add a second authentication implementation.",
    "The self-reported executor label Grok is not a trusted execution identity. Test doubles stay tests-only.",
    "Binding a new proof to app/control_authz or a real token is production enablement and stays unauthorized.",
    "Bind a MacCI signer that is distinct from the shared Owner login.",
    "Keep production_verification_enabled false. A test-proof:// string is not a production credential.",
    "Full integration remains blocked on the W02 identity-boundary final review.",
)
OWNERSHIP_EXCEPTIONS = (
    {
        "path": "docs/v060/02-architecture-contract.md",
        "wording": "六行依次为 → 七行依次为",
        "reason": (
            "The candidate encoding was already seven lines. The count word was corrected "
            "under a coordinated ownership exception. Shared-document edits are not expanded "
            "and tests/fixtures/v060/w00/contract.json is not edited."
        ),
        "coordinated": True,
        "shared_edits_expanded": False,
        "contract_json": "tests/fixtures/v060/w00/contract.json",
        "contract_json_edited": False,
    },
)
SAME_UID_THREAT_BOUNDARY = (
    "Same-UID threat boundary: mode 0700, an exclusive mkdir, and O_NOFOLLOW opens from a directory "
    "fd do not confine another process running as this uid. That peer can replace a directory entry "
    "before it is opened. lstat-then-open is not used. Untrusted candidate text runs only inside the "
    "isolation adapter. This does not claim host_acl_effective."
)

# Source review of the frozen ancestor. Not a live host or database measurement.
BASELINE_OBSERVATIONS = {
    "A04": {
        "source": "deploy/app-init/app_init.py install_job",
        "service_runs_as_installer_uid": True,
        "service_home_is_installer_home": True,
        "same_uid_directory_mode_is_isolation": False,
        "production_service_switched": False,
        "runtime_measured": False,
    },
    "A05": {
        "source": "deploy/app-init/app_init.py deploy; app/module_sdk.py",
        "initdb_auth": "trust",
        "role": "app_owner",
        "socket_parent": "/private/tmp",
        "same_uid_trust_is_inter_app_acl": False,
        "production_database_contacted": False,
        "runtime_measured": False,
    },
    "A06": {
        "source": "deploy/macmini/manage.sh test_app/build/update; deploy/macci/runner.py",
        "host_pytest_uses_env_dash_i": True,
        "host_pytest_keeps_home": True,
        "host_build_follows_load_env": True,
        "host_npm_ignore_scripts": False,
        "docker_npm_ignore_scripts": True,
        "docker_network_none": True,
        "leak_measured_on_host": False,
        "manage_sh_modified_by_w03": False,
        "runner_modified_by_w03": False,
        "runtime_measured": False,
    },
}

REAL_MACHINE_EVIDENCE = {
    "A04": (
        "Read-only launchd plist UserName and whether HOME is the installer home, with every secret value omitted.",
        "Process uid of the installed service compared with the installer uid, without switching the service.",
        "A second process with the same uid is denied the first app credential file by a control other than mode bits (separate uid, sandbox, or MAC). Do not print the credential.",
        "A recorded statement that the production service was not restarted or replaced for this check.",
    ),
    "A05": (
        "initdb auth method and role for each app cluster, showing trust plus a shared app_owner role is not an inter-app ACL.",
        "Socket directory uid and mode, plus a connect result from a different uid. A same-uid connect is not ACL proof.",
        "PostgreSQL ACL or distinct roles for app A reading app B, exercised on a temporary cluster only.",
        "A recorded statement that no production socket was opened and no migration ran.",
    ),
    "A06": (
        "Host pytest environ names only, showing whether HOME and token names are present. Omit values.",
        "Host build environ names after load_env, token names only. Omit values.",
        "Whether the host npm invocation passed --ignore-scripts and whether lifecycle scripts ran.",
        "Confirmation that W03 did not change manage.sh or runner.py. W07 must apply this contract on the host.",
        "A real-host measurement that a candidate lifecycle script cannot read the shared credential path. This simulation is not that measurement.",
    ),
}

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_APP_ID = re.compile(r"[a-z][a-z0-9_]{1,39}\Z")
_RUN_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_IDEM = re.compile(r"idem-[a-z0-9][a-z0-9-]{0,80}\Z")
_ACTOR_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,40}\Z")
_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,80}\Z")
_REPO_ID = re.compile(r"[A-Za-z0-9_.-]{1,40}(?:/[A-Za-z0-9_.-]{1,80})?\Z")
_PROOF = re.compile(r"test-proof://[A-Za-z0-9][A-Za-z0-9_./-]{0,80}\Z")
_REL_PART = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,79}\Z")
class IsolationError(Exception):
    """Bounded failure. ``str`` is the detail code and never a secret value."""

    def __init__(self, code: str, detail: str, http: int, phase: str):
        if detail not in DETAIL_CODES or code not in _CONTRACT_CODES:
            detail = "SCHEMA_INVALID"
            code = "SCHEMA_UNSUPPORTED"
            http = 422
            phase = "validate"
        self.code = code
        self.detail = detail
        self.http = http
        self.phase = phase
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class Actor:
    """Tests-only double. It is not the W02 principal and not an execution identity.

    W02_ALIGNMENT: swap this double for the reviewed ``app/control_identity``
    principal and authorize through ``app/control_authz``. Do not treat ``id``,
    the self-reported executor label, or a shared Owner login as a MacCI signer.
    """

    kind: str
    id: str
    proof_ref: str
    app_id: str


@dataclass(frozen=True, slots=True)
class Candidate:
    repo_id: str
    task_id: str
    app_id: str
    head: str
    base: str
    merge: str
    tree: str


@dataclass(frozen=True, slots=True)
class InputArtifact:
    relative_path: str
    content: bytes
    mode: int = 0o644


@dataclass(frozen=True, slots=True)
class BuildStep:
    op: str
    relative_path: str
    content: bytes = b""
    target_app_id: str = ""


@dataclass(frozen=True, slots=True)
class PermissionProfile:
    read_workspace: bool = True
    write_output: bool = True
    network: bool = False
    follow_symlinks: bool = False
    host_home_access: bool = False
    foreign_app_access: bool = False
    lifecycle_scripts: bool = False
    database_mode: str = "denied"


@dataclass(frozen=True, slots=True)
class BuildRequest:
    """Inputs for one isolated construction.

    ``env_allowlist`` selects fixed assignments. ``env_values`` is rejected
    when non-empty so a caller cannot supply a host token. ``declared_digest``
    must equal :func:`artifact_digest`. ``database_target`` is caller-supplied
    and is never opened.
    """

    actor: Actor
    candidate: Candidate
    artifacts: tuple[InputArtifact, ...]
    declared_digest: str
    env_allowlist: tuple[str, ...]
    permissions: PermissionProfile
    database_mode: str
    run_id: str
    idempotency_key: str
    expected_revision: int = 1
    env_values: tuple[tuple[str, str], ...] = ()
    script_text: str | None = None
    plan: tuple[BuildStep, ...] = ()
    database_target: str = ""
    production_verification_requested: bool = False


class MockDatabase:
    """In-memory per-app double. It is not a security boundary.

    It has no socket and no production role. A cross-app denial here is an
    in-process check only. Cross-app isolation for untrusted code is the
    sandbox probe, or ``BLOCKED`` when that probe cannot run.
    """

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()

    def put(self, app_id: str, key: str, value: str) -> None:
        with self._lock:
            self._rows.setdefault(app_id, {})[key] = value

    def get(self, requester_app_id: str, owner_app_id: str, key: str) -> str | None:
        if requester_app_id != owner_app_id:
            raise IsolationError("POLICY_DENIED", "CROSS_APP_DENIED", 403, "authorize")
        with self._lock:
            return self._rows.get(owner_app_id, {}).get(key)


class IsolationSession:
    """In-process view only. Lookup does not execute and does not touch the store.

    A second process has an empty session. Durable replay, crash recovery, and
    cleanup failure live in :class:`DurableStore`.
    """

    def __init__(self, revision: int = 1) -> None:
        self.revision = revision
        self.mock_db = MockDatabase()
        self.live_database_connects = 0
        self._idem: dict[tuple[str, ...], dict[str, object]] = {}
        self._lock = threading.Lock()

    def lookup(self, key: tuple[str, ...], payload_hash: str) -> tuple[str, dict | None]:
        """Read this process's memory. Do not execute and do not write the store."""
        with self._lock:
            stored = self._idem.get(key)
            if stored is None:
                return "miss", None
            if stored["payload_hash"] != payload_hash:
                receipt = stored["receipt"]
                assert isinstance(receipt, dict)
                return "mismatch", {
                    "original_result": receipt["state"],
                    "current_state": receipt["state"],
                }
            return "replay", copy.deepcopy(stored["receipt"])

    def remember(self, key: tuple[str, ...], payload_hash: str, receipt: Mapping[str, object]) -> None:
        """Cache a result this process already committed to the durable store."""
        with self._lock:
            current = self._idem.get(key)
            if current is None:
                self._idem[key] = {
                    "payload_hash": payload_hash,
                    "receipt": copy.deepcopy(dict(receipt)),
                }
                return
            if current["payload_hash"] != payload_hash:
                return


@dataclass(frozen=True, slots=True)
class ClaimDecision:
    kind: str
    receipt: dict | None = None
    info: dict | None = None
    claim_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class IsolationCapability:
    status: str
    backend: str
    reason: str
    evidence: dict


@dataclass(frozen=True, slots=True)
class ExecResult:
    started: bool
    exit_code: int | None
    blocked: bool
    reason: str
    tree_terminated: bool = False


def candidate_id(candidate: Candidate) -> str:
    """Frozen encoding: seven lines, no trailing newline, then ``cand_`` + sha256."""
    _require_candidate_shape(candidate)
    text = "\n".join((
        "v1",
        candidate.repo_id,
        candidate.task_id,
        candidate.head,
        candidate.base,
        candidate.merge,
        candidate.tree,
    ))
    if text.endswith("\n"):
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    return "cand_" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def artifact_digest(artifacts: tuple[InputArtifact, ...]) -> str:
    manifest = _canonical_manifest(artifacts)
    return "sha256:" + hashlib.sha256(manifest).hexdigest()


def build_environment(workspace: Path, allowlist: tuple[str, ...]) -> dict[str, str]:
    """Fixed assignments for allowlisted names. Caller values are not accepted."""
    _validate_allowlist(allowlist)
    workspace = _directory(workspace)
    assignments = {
        "PATH": FIXED_PATH,
        "HOME": str(workspace),
        "LANG": FIXED_LANG,
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(workspace / "tmp"),
    }
    return {key: assignments[key] for key in allowlist}


def classify_database_target(target: str) -> None:
    """Classify a caller-supplied database target and refuse it.

    This function does not open a socket and does not import a database driver
    beyond the temporary sqlite path used separately for mode ``temporary``.
    """
    if not isinstance(target, str) or not target:
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    lowered = target.lower()
    production_markers = ("postgres", "app_owner", "/private/tmp", "host=", "trust")
    if any(marker in lowered for marker in production_markers):
        raise IsolationError("PRODUCTION_DB_FORBIDDEN", "DATABASE_DENIED", 403, "authorize")
    raise IsolationError("POLICY_DENIED", "DATABASE_DENIED", 403, "authorize")


def resolve_contained(workspace: Path, relative: str) -> Path:
    """Reject a symlink by opening each component with ``O_NOFOLLOW``.

    The returned path is not a capability. Callers that write or read must
    use the directory fd of a workspace this process created. A same-UID peer
    is outside this check; see ``SAME_UID_THREAT_BOUNDARY``.
    """
    relative = _pure_relative(relative)
    root = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _walk_no_follow(root, relative, allow_missing_final=True)
    finally:
        os.close(root)
    return workspace.joinpath(*relative.split("/"))


def create_temporary_database(workspace: Path, app_id: str, *, root_fd: int | None = None) -> Path:
    """Create a sqlite file inside ``workspace`` through a directory fd.

    This is not a host cluster. The connection uses the open fd, not a later
    path open.
    """
    if not _APP_ID.fullmatch(app_id):
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    owns = root_fd is None
    if root_fd is None:
        root_fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    row = None
    try:
        db_fd = _ensure_child_dir(root_fd, "db")
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW
            fd = os.open("app.sqlite", flags, 0o600, dir_fd=db_fd)
            try:
                os.fchmod(fd, 0o600)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                    raise IsolationError("POLICY_DENIED", "DATABASE_DENIED", 403, "execute")
                connection = sqlite3.connect(f"file:/proc/self/fd/{fd}?mode=rw", uri=True)
                try:
                    connection.execute("create table probe(app_id text primary key)")
                    connection.execute("insert into probe(app_id) values (?)", (app_id,))
                    connection.commit()
                    row = connection.execute("select app_id from probe").fetchone()
                finally:
                    connection.close()
            finally:
                os.close(fd)
        finally:
            os.close(db_fd)
    finally:
        if owns and root_fd is not None:
            os.close(root_fd)
    if row != (app_id,):
        raise IsolationError("EVIDENCE_MISSING", "DATABASE_DENIED", 409, "execute")
    return workspace / "db" / "app.sqlite"


class DurableStore:
    """Filesystem claims keyed by the business key, not by app or run.

    A missing result is an unknown outcome. A live pid is a conflict. The
    same key with another run replays only after a result file is durable.
    """

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
        self.root = root

    def begin(self, business_key: tuple[str, ...], payload_hash: str, run_id: str) -> ClaimDecision:
        name = _claim_name(business_key)
        root_fd = self._root_fd()
        try:
            try:
                os.mkdir(name, 0o700, dir_fd=root_fd)
                created = True
            except FileExistsError:
                created = False
            claim_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                os.fchmod(claim_fd, 0o700)
                if created:
                    _write_new_file(claim_fd, "claim.json", {
                        "v": 1,
                        "state": "started",
                        "payload_hash": payload_hash,
                        "run_id": run_id,
                        "pid": os.getpid(),
                        "executor": EXECUTOR,
                    })
                    return ClaimDecision("fresh", claim_dir=self.root / name)
                return self._read_existing(claim_fd, payload_hash)
            finally:
                os.close(claim_fd)
        finally:
            os.close(root_fd)

    def commit(self, decision: ClaimDecision, receipt: Mapping[str, object], cleanup: Mapping[str, object] | None) -> None:
        if decision.kind != "fresh" or decision.claim_dir is None:
            raise IsolationError("SIDE_EFFECT_UNKNOWN", "UNKNOWN_RESULT", 409, "reserve")
        claim_fd = os.open(decision.claim_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if _fd_exists(claim_fd, "result.json"):
                return
            if cleanup is not None:
                _replace_file(claim_fd, "cleanup.json", dict(cleanup))
            _replace_file(claim_fd, "result.json", dict(receipt))
            os.fsync(claim_fd)
        finally:
            os.close(claim_fd)

    def abort(self, decision: ClaimDecision) -> None:
        if decision.kind != "fresh" or decision.claim_dir is None:
            return
        try:
            claim_fd = os.open(decision.claim_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            return
        try:
            if _fd_exists(claim_fd, "result.json"):
                return
            claim = _read_json_fd(claim_fd, "claim.json")
            if not isinstance(claim, dict) or claim.get("pid") != os.getpid():
                return
            for name in ("claim.json", "cleanup.json", "result.tmp", "cleanup.tmp"):
                try:
                    os.unlink(name, dir_fd=claim_fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(claim_fd)
        try:
            os.rmdir(decision.claim_dir)
        except OSError:
            pass

    def cleanup_record(self, business_key: tuple[str, ...]) -> dict | None:
        name = _claim_name(business_key)
        try:
            root_fd = self._root_fd()
        except IsolationError:
            return None
        try:
            try:
                claim_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            except OSError:
                return None
            try:
                data = _read_json_fd(claim_fd, "cleanup.json")
            finally:
                os.close(claim_fd)
        finally:
            os.close(root_fd)
        return data if isinstance(data, dict) else None

    def _root_fd(self) -> int:
        self.root.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        os.fchmod(fd, 0o700)
        return fd

    def _read_existing(self, claim_fd: int, payload_hash: str) -> ClaimDecision:
        claim = _read_json_fd(claim_fd, "claim.json")
        result = _read_json_fd(claim_fd, "result.json")
        if not isinstance(claim, dict):
            return ClaimDecision("unknown")
        if not isinstance(result, dict):
            pid = claim.get("pid")
            if isinstance(pid, int) and _pid_alive(pid):
                return ClaimDecision("conflict")
            return ClaimDecision("unknown")
        if claim.get("payload_hash") != payload_hash:
            state = result.get("state")
            return ClaimDecision("mismatch", info={"original_result": state, "current_state": state})
        return ClaimDecision("replay", receipt=result)


def leave_unfinished_claim(parent: Path, request: BuildRequest) -> None:
    """Record a started claim and no result, as a crash before commit would."""
    _check_parent(parent)
    DurableStore(Path(parent) / ".w03-claims").begin(
        _business_key(request), _payload_hash(request), request.run_id,
    )


def business_key_for(request: BuildRequest) -> tuple[str, ...]:
    return _business_key(request)


def bind_execution_digest(
    *,
    output_digest: str,
    input_digest: str,
    candidate: Candidate,
    executor: str,
    exit_code: int | None,
    probe_evidence: Mapping[str, object],
) -> dict:
    """Bind the output digest to the candidate 4-tuple, exit, and probe.

    ``executor`` is a self-reported label. The preimage records that the
    label is not a trusted execution identity. A matching label does not
    authorize the run and does not stand in for ``app/control_authz``.
    """
    _require_candidate_shape(candidate)
    if executor != EXECUTOR or not output_digest.startswith("sha256:") or not input_digest.startswith("sha256:"):
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    if exit_code is not None and (type(exit_code) is not int or not -128 <= exit_code <= 255):
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    canonical = _probe_canonical(probe_evidence)
    exit_line = "-" if exit_code is None else str(exit_code)
    preimage = "\n".join((
        BINDING_VERSION,
        input_digest,
        output_digest,
        candidate.head,
        candidate.base,
        candidate.merge,
        candidate.tree,
        "self-reported:" + executor,
        "trusted_execution_identity:false",
        exit_line,
        canonical,
    ))
    digest = "sha256:" + hashlib.sha256(preimage.encode("utf-8")).hexdigest()
    return {
        "version": BINDING_VERSION,
        "digest": digest,
        "input_digest": input_digest,
        "output_digest": output_digest,
        "candidate_tuple": {
            "head": candidate.head,
            "base": candidate.base,
            "merge": candidate.merge,
            "tree": candidate.tree,
        },
        "executor": executor,
        "self_reported_executor": executor,
        "trusted_execution_identity": False,
        "execution_identity": None,
        "self_reported_executor_is_identity": False,
        "exit_code": exit_code,
        "probe_evidence_sha256": "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def probe_isolation() -> IsolationCapability:
    """Run a trusted probe inside the sandbox. Never run candidate text.

    A missing sandbox, or a probe that cannot show the five denials, is
    ``BLOCKED``. This does not set a host or database ACL, and it is not
    MacCI evidence.
    """
    try:
        return _probe_isolation()
    except Exception:
        return _blocked("probe failed closed")


def macci_container_argv(job: str, image: str) -> list[str]:
    """Argv mapped from the existing MacCI ``docker run``. It is not launched here."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", job):
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,80}", image):
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    return [
        "docker", "run", "--name", job,
        "--network", MACCI_CONTAINER_BOUNDARY["network"],
        "--cap-drop", MACCI_CONTAINER_BOUNDARY["cap_drop"],
        "--security-opt", MACCI_CONTAINER_BOUNDARY["security_opt"],
        "--pids-limit", MACCI_CONTAINER_BOUNDARY["pids_limit"],
        "--memory", MACCI_CONTAINER_BOUNDARY["memory"],
        "--cpus", MACCI_CONTAINER_BOUNDARY["cpus"],
        image,
    ]


def assess_macci_runtime() -> dict:
    """Map the MacCI container boundary and mark what this host cannot exercise.

    This does not start Docker, does not edit ``runner.py``, and does not
    downgrade an untrusted candidate onto the host. ``macci_evidence`` stays
    ``pending``. A04 and A05 real-machine isolation stay unverified.
    """
    gaps = []
    missing = [
        "macci_docker_engine",
        "container_output_cap",
        "container_disk_quota",
        "container_timeout_tree_reap",
        "a04_real_mac_host_isolation",
        "a05_real_database_role_isolation",
    ]
    if sys.platform != "darwin":
        gaps.append(f"runtime target is macOS; this host is {sys.platform}")
        missing.insert(0, "macos_host")
    else:
        gaps.append("this process is not the MacCI host and will not launch a container")
        missing.insert(0, "macci_launch_not_performed")
    if shutil.which("docker") is None:
        gaps.append("the MacCI docker engine is not available on this host")
    else:
        gaps.append("docker is installed but this module will not launch a container or edit runner.py")
    gaps.append(
        "the existing docker run has no output byte cap, no disk quota, and no confirmation "
        "that the process tree is dead after timeout"
    )
    gaps.append("A04 real Mac host isolation and A05 real database-role isolation were not measured")
    return {
        "status": "BLOCKED",
        "runtime_target": RUNTIME_TARGET,
        "reason": "; ".join(gaps),
        "boundary": dict(MACCI_CONTAINER_BOUNDARY),
        "missing_capabilities": missing,
        "host_downgrade": False,
        "macci_evidence": "pending",
        "optional_adapter": "linux-unshare" if _linux_unshare_adapter_available() else "none",
    }


def _linux_unshare_adapter_available() -> bool:
    return sys.platform.startswith("linux") and _unshare_argv() is not None and _trusted_shell_files() is not None


def _select_adapter() -> str | None:
    """Return the optional Linux adapter, or None. Never return a host path."""
    if _linux_unshare_adapter_available():
        return "linux-unshare"
    return None


def _resource_limit_record(timeout: float | None = None) -> dict:
    return {
        "nproc": LIMIT_NPROC,
        "cpu_seconds": LIMIT_CPU_SECONDS,
        "address_space_bytes": LIMIT_AS_BYTES,
        "file_size_bytes": LIMIT_FSIZE_BYTES,
        "output_bytes": LIMIT_OUTPUT_BYTES,
        "timeout_seconds": SANDBOX_TIMEOUT_SECONDS if timeout is None else timeout,
        "enforced_by": "linux-unshare-rlimit",
        "macci_cgroup_limits": "BLOCKED",
    }


def _runtime_evidence(adapter: str) -> dict:
    assessment = assess_macci_runtime()
    return {
        "runtime_target": RUNTIME_TARGET,
        "macci_runtime": assessment["status"],
        "macci_runtime_reason": assessment["reason"],
        "macci_missing_capabilities": list(assessment["missing_capabilities"]),
        "macci_boundary": assessment["boundary"],
        "optional_adapter": adapter,
        "host_downgrade": False,
        "capture_output_unbounded": False,
        "trusted_execution_identity": False,
        "self_reported_executor_is_identity": False,
        "a04_a05_real_machine_status": "pending",
        "production_verification_enabled": False,
        "macci_evidence": "pending",
        "host_acl_effective": False,
        "db_acl_effective": False,
    }


def _apply_candidate_limits(nproc: int, cpu: int, address: int, fsize: int) -> None:
    pairs = (
        (resource.RLIMIT_NPROC, nproc),
        (resource.RLIMIT_CPU, cpu),
        (resource.RLIMIT_AS, address),
        (resource.RLIMIT_FSIZE, fsize),
        (resource.RLIMIT_CORE, 0),
    )
    for which, value in pairs:
        resource.setrlimit(which, (value, value))
        soft, hard = resource.getrlimit(which)
        if soft != value or hard != value:
            raise OSError(errno.EPERM, "limit did not stick")


def _read_exec_limits() -> tuple[int, int, int, int] | None:
    specs = (
        ("W03_LIMIT_NPROC", LIMIT_NPROC, 4, 64),
        ("W03_LIMIT_CPU", LIMIT_CPU_SECONDS, 1, 30),
        ("W03_LIMIT_AS", LIMIT_AS_BYTES, 32 * 1024 * 1024, 256 * 1024 * 1024),
        ("W03_LIMIT_FSIZE", LIMIT_FSIZE_BYTES, 4096, 1024 * 1024),
    )
    values = []
    for name, default, low, high in specs:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            values.append(default)
            continue
        if not raw.isdigit():
            return None
        number = int(raw)
        if not low <= number <= high:
            return None
        values.append(number)
    return values[0], values[1], values[2], values[3]


def _descendant_pids(pid: int) -> list[int]:
    found: list[int] = []
    stack = [pid]
    seen = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        path = f"/proc/{current}/task/{current}/children"
        try:
            with open(path, "r", encoding="ascii") as handle:
                raw = handle.read().split()
        except OSError:
            continue
        for item in raw:
            if item.isdigit():
                child = int(item)
                found.append(child)
                stack.append(child)
    if found and found[0] == pid:
        return found[1:]
    return [item for item in found if item != pid]


def _pid_namespace(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/ns/pid")
    except OSError:
        return None


def _processes_in_namespace(namespace: str) -> list[int]:
    found = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return [-1]
    for name in names:
        if not name.isdigit():
            continue
        try:
            if os.readlink(f"/proc/{name}/ns/pid") == namespace:
                found.append(int(name))
        except OSError:
            continue
    return found


def _reap_process_tree(proc: subprocess.Popen) -> bool:
    """SIGKILL the session and confirm the isolated pid namespace is empty.

    ``unshare --kill-child`` is not treated as proof. A timeout that only
    returns from ``wait`` is not complete reaping.
    """
    pid = proc.pid
    descendants = _descendant_pids(pid)
    namespaces = []
    for item in descendants:
        namespace = _pid_namespace(item)
        if namespace and namespace not in namespaces:
            namespaces.append(namespace)
    for target in (pid, *descendants):
        try:
            os.kill(target, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        except OSError:
            pass
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return False
    for _ in range(10):
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        for item in descendants:
            if _pid_alive(item):
                try:
                    os.kill(item, signal.SIGKILL)
                except OSError:
                    pass
        leftovers = []
        for namespace in namespaces:
            found = _processes_in_namespace(namespace)
            if any(item < 0 for item in found):
                return False
            leftovers.extend(found)
            for leftover in found:
                try:
                    os.kill(leftover, signal.SIGKILL)
                except OSError:
                    pass
        if not _pid_alive(pid) and not any(_pid_alive(item) for item in descendants) and not leftovers:
            return True
        time.sleep(0.02)
    return False


def _run_isolated(
    argv: list[str],
    env: dict[str, str],
    cwd: str,
    timeout: float,
    *,
    hold_stdin: bool = False,
    confirm_tree: Callable[[], bool] | None = None,
) -> dict:
    """Run an isolated argv with a bounded pipe and a confirmed reap.

    Stdout and stderr are discarded after they are counted. They are not
    stored and they are not collected with ``capture_output``.
    """
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if hold_stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    state = {"total": 0}
    lock = threading.Lock()
    exceeded = threading.Event()

    def consume(pipe) -> None:
        try:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                with lock:
                    state["total"] += len(chunk)
                    if state["total"] > LIMIT_OUTPUT_BYTES:
                        exceeded.set()
                        break
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    threads = (
        threading.Thread(target=consume, args=(proc.stdout,), daemon=True),
        threading.Thread(target=consume, args=(proc.stderr,), daemon=True),
    )
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None:
            if exceeded.is_set() or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        timed_out = proc.poll() is None and not exceeded.is_set()
        output_exceeded = exceeded.is_set()
        if proc.poll() is None:
            tree_terminated = _reap_process_tree(proc)
        else:
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                tree_terminated = False
            else:
                tree_terminated = not _pid_alive(proc.pid)
        if confirm_tree is not None:
            tree_terminated = bool(confirm_tree())
        return {
            "returncode": proc.returncode,
            "timed_out": timed_out,
            "output_exceeded": output_exceeded,
            "tree_terminated": tree_terminated,
        }
    finally:
        if hold_stdin and proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        if proc.poll() is None:
            _reap_process_tree(proc)
        for thread in threads:
            thread.join(timeout=1)


def _run_capped(argv: list[str], *, timeout: float, env: dict[str, str], cap: int = 65536) -> subprocess.CompletedProcess:
    """Read a trusted helper's output only up to ``cap``. Do not use ``capture_output``."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _reap_process_tree(proc)
        raise
    if len(out) > cap or len(err) > cap:
        _reap_process_tree(proc)
        raise OSError(errno.EFBIG, "helper output exceeded the cap")
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def execute_isolated(
    ws_fd: int,
    workspace: Path,
    script: str,
    *,
    timeout: float | None = None,
    hold_stdin: bool = False,
    force_limit_failure: bool = False,
    confirm_tree: Callable[[], bool] | None = None,
) -> ExecResult:
    """Execute candidate text only inside the optional Linux adapter.

    The MacCI container path stays ``BLOCKED`` on a host that cannot exercise
    it. There is no host-shell fallback. Output is capped. After a timeout
    or an output overrun the isolated process tree must be confirmed dead,
    or the result stays unconfirmed.
    """
    if assess_macci_runtime()["host_downgrade"]:
        return ExecResult(False, None, True, "host execution refused", False)
    if _select_adapter() != "linux-unshare":
        return ExecResult(False, None, True, "no isolation adapter; host execution refused", False)
    libs = _trusted_shell_files()
    argv = _unshare_argv()
    if not libs or argv is None:
        return ExecResult(False, None, True, "sandbox unavailable", False)
    if script.encode("utf-8") and any(script in part for part in argv):
        return ExecResult(False, None, True, "candidate text leaked into argv", False)
    try:
        _write_exclusive_at(ws_fd, "candidate.sh", script.encode("utf-8"), 0o600)
    except IsolationError:
        return ExecResult(False, None, True, "candidate file was not created", False)
    env = _sandbox_env(workspace, "exec", libs, None)
    if force_limit_failure:
        env["W03_FORCE_LIMIT_FAILURE"] = "1"
    limit = SANDBOX_TIMEOUT_SECONDS if timeout is None else timeout
    if type(limit) not in {int, float} or not 0.2 <= float(limit) <= SANDBOX_TIMEOUT_SECONDS:
        return ExecResult(False, None, True, "sandbox unavailable", False)
    try:
        completed = _run_isolated(
            argv,
            env,
            str(workspace),
            float(limit),
            hold_stdin=hold_stdin,
            confirm_tree=confirm_tree,
        )
    except OSError:
        _remove_jail_mountpoint(workspace)
        return ExecResult(False, None, True, "sandbox failed to start", False)
    finally:
        _remove_jail_mountpoint(workspace)
    started = _marker_present(ws_fd)
    if not completed["tree_terminated"]:
        return ExecResult(started, None, True, "reap_unconfirmed", False)
    if completed["output_exceeded"]:
        return ExecResult(True, None, False, "output_limit", True)
    if completed["timed_out"]:
        return ExecResult(started, None, False, "timeout", True)
    if not started:
        return ExecResult(False, None, True, "sandbox did not start the candidate", True)
    code = completed["returncode"]
    if code is None:
        return ExecResult(True, None, True, "reap_unconfirmed", False)
    return ExecResult(True, int(code), False, "executed", True)


def sandbox_launch() -> int:
    """Entrypoint inside ``unshare``. Refuses to run on the host namespace."""
    import socket  # Load before chroot. The jail has no Python library tree.
    del socket
    try:
        uid_map = open("/proc/self/uid_map", "r", encoding="ascii").read().strip()
    except OSError:
        return 2
    if os.geteuid() != 0:
        return 2
    parts = uid_map.split()
    if len(parts) != 3 or parts[0] != "0" or parts[2] != "1":
        return 2
    mode = os.environ.get("W03_MODE", "")
    workspace = os.environ.get("W03_WORKSPACE", "")
    libs = []
    for item in os.environ.get("W03_LIBS", "").split(":"):
        if not item or "|" not in item:
            continue
        src, dst = item.split("|", 1)
        libs.append((src, dst))
    if mode not in {"probe", "exec"} or not _abs_safe(workspace):
        return 2
    if not libs or any(not _abs_safe(src) or not _abs_safe(dst) for src, dst in libs):
        return 2
    try:
        _enter_jail(workspace, libs, mount_proc=(mode == "probe"))
    except Exception:
        return 3
    try:
        if mode == "probe":
            _write_probe_result()
            return 0
        if not _mount_denied():
            return 3
        return _exec_candidate()
    except Exception:
        return 3


def _decision_receipt(request, session, business_key, payload_hash, decision: ClaimDecision) -> dict | None:
    if decision.kind == "fresh":
        return None
    if decision.kind == "replay" and isinstance(decision.receipt, dict):
        session.remember(business_key, payload_hash, decision.receipt)
        return _replay_receipt(decision.receipt)
    if decision.kind == "mismatch" and isinstance(decision.info, dict):
        return _mismatch_receipt(request, decision.info)
    if decision.kind == "conflict":
        return _failure_receipt(
            request,
            IsolationError("RESERVATION_CONFLICT", "CONCURRENCY_CONFLICT", 409, "reserve"),
            "not_started",
            "skipped",
        )
    return _unknown_receipt(request)


def _unknown_receipt(request: BuildRequest) -> dict:
    return _failure_receipt(
        request,
        IsolationError("SIDE_EFFECT_UNKNOWN", "UNKNOWN_RESULT", 409, "reserve"),
        "not_started",
        "unknown",
    )


def _commit_success(
    session, store, decision, business_key, payload_hash, request, env,
    database_probe, ws_fd, capability, commands, exit_code, workspace, remover,
) -> dict:
    probe_evidence = _evidence_for_binding(capability, commands)
    output_digest = _output_digest_fd(ws_fd)
    binding = bind_execution_digest(
        output_digest=output_digest,
        input_digest=request.declared_digest,
        candidate=request.candidate,
        executor=EXECUTOR,
        exit_code=exit_code,
        probe_evidence=probe_evidence,
    )
    retained = retain_build_artifacts(workspace.parent, request.run_id, ws_fd, output_digest)
    receipt = _success_receipt(request, env, database_probe, output_digest)
    receipt["commands_executed"] = commands
    receipt["exit_code"] = exit_code
    receipt["execution_binding"] = binding
    receipt["probe_evidence"] = probe_evidence
    receipt["isolation_status"] = "available" if commands else "not_invoked"
    receipt["candidate_tuple"] = dict(binding["candidate_tuple"])
    receipt["artifact_retain"] = retained
    receipt["process_tree_terminated"] = True if commands else None
    if commands:
        receipt["resource_limits"] = _resource_limit_record()
    cleanup_state, cleanup_error = _cleanup(workspace, remover)
    workspace_left = workspace if cleanup_state == "failed" else None
    _apply_cleanup(receipt, cleanup_state, cleanup_error, workspace_left)
    cleanup_record = None
    if cleanup_state == "failed":
        cleanup_record = {
            "state": "failed",
            "error_class": cleanup_error,
            "detail_code": "CLEANUP_FAILED",
            "workspace_path": str(workspace),
        }
    stored = copy.deepcopy(receipt)
    try:
        store.commit(decision, stored, cleanup_record)
    except OSError:
        return _unknown_receipt(request)
    session.remember(business_key, payload_hash, stored)
    return copy.deepcopy(stored)


def _fail_after_claim(
    session, store, decision, business_key, payload_hash, request, exc,
    workspace, ws_fd, remover, capability, script_started, exit_code,
) -> dict:
    if script_started and exit_code is None:
        return _unknown_receipt(request)
    cleanup_state = "not_started"
    cleanup_error = None
    workspace_left = None
    output_digest = _empty_output_digest()
    if ws_fd is not None:
        try:
            output_digest = _output_digest_fd(ws_fd)
        except IsolationError:
            output_digest = _empty_output_digest()
    retained = None
    if (
        workspace is not None
        and ws_fd is not None
        and isinstance(request, BuildRequest)
        and exc.detail in {"SCRIPT_FAILED", "RESOURCE_LIMIT", "CROSS_APP_DENIED"}
    ):
        try:
            retained = retain_build_artifacts(workspace.parent, request.run_id, ws_fd, output_digest)
        except IsolationError:
            retained = {
                "verified": False,
                "digest": output_digest,
                "relative_root": None,
                "files": [],
            }
    if workspace is not None:
        cleanup_state, cleanup_error = _cleanup(workspace, remover)
        if cleanup_state == "failed":
            workspace_left = workspace
    receipt = _failure_receipt(request, exc, cleanup_state, "skipped", cleanup_error, workspace_left)
    if retained is not None:
        receipt["artifact_retain"] = retained
    known_script = script_started and exc.detail in {"SCRIPT_FAILED", "RESOURCE_LIMIT"} and exit_code is not None
    if known_script:
        probe_evidence = _evidence_for_binding(capability, 1)
        receipt["commands_executed"] = 1
        receipt["exit_code"] = exit_code
        receipt["probe_evidence"] = probe_evidence
        receipt["isolation_status"] = "available"
        receipt["failure"]["commands_executed"] = 1
        receipt["process_tree_terminated"] = True
        if exc.detail == "RESOURCE_LIMIT":
            receipt["resource_limits"] = _resource_limit_record()
        try:
            receipt["execution_binding"] = bind_execution_digest(
                output_digest=output_digest,
                input_digest=request.declared_digest,
                candidate=request.candidate,
                executor=EXECUTOR,
                exit_code=exit_code,
                probe_evidence=probe_evidence,
            )
            receipt["candidate_tuple"] = dict(receipt["execution_binding"]["candidate_tuple"])
        except IsolationError:
            receipt["execution_binding"] = None
        cleanup_record = None
        if cleanup_state == "failed":
            cleanup_record = {
                "state": "failed",
                "error_class": cleanup_error,
                "detail_code": "CLEANUP_FAILED",
                "workspace_path": str(workspace) if workspace is not None else "",
            }
        try:
            stored = copy.deepcopy(receipt)
            store.commit(decision, stored, cleanup_record)
            session.remember(business_key, payload_hash, stored)
        except OSError:
            return _unknown_receipt(request)
        return copy.deepcopy(stored)
    if not script_started:
        store.abort(decision)
    return receipt


def _evidence_for_binding(capability: IsolationCapability | None, commands: int) -> dict:
    if capability is None or commands == 0:
        evidence = _not_run_evidence()
        return evidence
    evidence = copy.deepcopy(capability.evidence)
    evidence["candidate_executed"] = True
    evidence["status"] = "available"
    return evidence


def _not_run_evidence() -> dict:
    evidence = {
        "status": "not_run",
        "macci_evidence": "pending",
        "candidate_executed": False,
        "host_acl_effective": False,
        "db_acl_effective": False,
        "real_database": False,
        "production_verification_enabled": False,
        "same_uid_threat_boundary": SAME_UID_THREAT_BOUNDARY,
    }
    evidence.update(_runtime_evidence("not_invoked"))
    return evidence


def _probe_canonical(evidence: Mapping[str, object]) -> str:
    return json.dumps(dict(evidence), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _blocked(reason: str) -> IsolationCapability:
    evidence = {
        "status": "BLOCKED",
        "backend": "none",
        "reason": reason,
        "executed_probe": False,
        "candidate_executed": False,
        "home_inaccessible": False,
        "environment_inaccessible": False,
        "external_file_inaccessible": False,
        "network_inaccessible": False,
        "cross_app_inaccessible": False,
        "unprivileged": False,
        "host_acl_effective": False,
        "db_acl_effective": False,
        "macci_evidence": "pending",
        "real_database": False,
        "production_verification_enabled": False,
        "same_uid_threat_boundary": SAME_UID_THREAT_BOUNDARY,
    }
    evidence.update(_runtime_evidence("none"))
    return IsolationCapability("BLOCKED", "none", reason, evidence)


def _probe_isolation() -> IsolationCapability:
    argv = _unshare_argv()
    libs = _trusted_shell_files()
    if argv is None or not libs:
        return _blocked("user-namespace sandbox is not available")
    host_net = _loopback_result()
    import tempfile
    with tempfile.TemporaryDirectory(prefix="w03-probe-") as folder:
        root = Path(folder)
        workspace = root / "workspace"
        workspace.mkdir(mode=0o700)
        external = root / "outside" / "external.txt"
        external.parent.mkdir(mode=0o700)
        external.write_bytes(b"external-marker\n")
        cross = root / "app_beta" / "data.txt"
        cross.parent.mkdir(mode=0o700)
        cross.write_bytes(b"cross-app-marker\n")
        home_file = root / "host-home" / "marker.txt"
        home_file.parent.mkdir(mode=0o700)
        home_file.write_bytes(b"home-marker\n")
        try:
            host_read = _run_capped(
                [sys.executable, "-I", "-B", "-c",
                 "import pathlib,sys; sys.stdout.write(pathlib.Path(sys.argv[1]).read_text())",
                 str(external)],
                timeout=5,
                env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return _blocked("host control could not read the external file")
        if host_read.stdout != b"external-marker\n":
            return _blocked("host control could not read the external file")
        targets = {
            "home": str(home_file),
            "real_home": str(Path.home()),
            "external": str(external),
            "cross": str(cross),
        }
        env = _sandbox_env(workspace, "probe", libs, targets)
        if any(_is_credential_name(name) for name in env):
            return _blocked("probe environment contained a credential name")
        try:
            completed = _run_isolated(argv, env, str(workspace), SANDBOX_TIMEOUT_SECONDS)
        except OSError:
            return _blocked("probe subprocess did not finish")
        if not completed["tree_terminated"]:
            return _blocked("probe process tree was not confirmed dead")
        if completed["timed_out"] or completed["output_exceeded"] or completed["returncode"] != 0:
            return _blocked("probe subprocess did not finish")
        probe_path = workspace / "probe-result.json"
        raw = b""
        try:
            raw = probe_path.read_bytes()
        except OSError:
            return _blocked("probe did not return evidence")
        if b"external-marker" in raw or b"cross-app-marker" in raw or b"home-marker" in raw:
            return _blocked("probe evidence contained a marker value")
        if external.read_bytes() != b"external-marker\n" or cross.read_bytes() != b"cross-app-marker\n":
            return _blocked("probe changed a file outside the workspace")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return _blocked("probe evidence was not json")
        if not isinstance(data, dict):
            return _blocked("probe process failed")
        return _capability_from_probe(data, host_net)


def _capability_from_probe(data: dict, host_net: str) -> IsolationCapability:
    tokens = {
        key: data.get(key) for key in (
            "real_home", "fake_home", "external", "shell_external", "cross", "loopback",
        )
    }
    if any(not isinstance(value, str) or len(value) > 32 for value in tokens.values()):
        return _blocked("probe tokens were not bounded")
    home_ok = data.get("home") == "/work" and tokens["real_home"] != "opened" and tokens["fake_home"] != "opened"
    external_ok = tokens["external"] != "opened" and tokens["shell_external"] != "opened"
    cross_ok = tokens["cross"] != "opened"
    network_ok = tokens["loopback"] == "ENETUNREACH" and host_net not in {"ENETUNREACH", "opened", ""}
    unprivileged = data.get("cap_eff_zero") is True and data.get("no_new_privs") is True and data.get("mount_denied") is True
    environment_ok = data.get("credential_env_present") is False and data.get("synthetic_present") is True
    same_uid = data.get("mapped_uid") == os.getuid() and data.get("only_lo") is True
    limits_ok = (
        data.get("nproc_hard") == LIMIT_NPROC
        and data.get("cpu_hard") == LIMIT_CPU_SECONDS
        and data.get("as_hard") == LIMIT_AS_BYTES
        and data.get("fsize_hard") == LIMIT_FSIZE_BYTES
    )
    passed = all((home_ok, external_ok, cross_ok, network_ok, unprivileged, environment_ok, same_uid, limits_ok))
    evidence = {
        "status": "available" if passed else "BLOCKED",
        "backend": "linux-unshare-optional" if passed else "none",
        "reason": "probe passed" if passed else "probe counterexample failed",
        "executed_probe": True,
        "candidate_executed": False,
        "home_inaccessible": home_ok,
        "environment_inaccessible": environment_ok,
        "external_file_inaccessible": external_ok,
        "network_inaccessible": network_ok,
        "cross_app_inaccessible": cross_ok,
        "unprivileged": unprivileged,
        "no_new_privs": data.get("no_new_privs") is True,
        "same_uid_maps_host_user": data.get("mapped_uid") == os.getuid(),
        "mapped_uid": data.get("mapped_uid") if isinstance(data.get("mapped_uid"), int) else None,
        "sandbox_loopback": tokens["loopback"] if tokens["loopback"] in {"ENETUNREACH", "ECONNREFUSED", "opened"} else "OSError",
        "host_loopback": host_net,
        "host_acl_effective": False,
        "db_acl_effective": False,
        "macci_evidence": "pending",
        "real_database": False,
        "production_verification_enabled": False,
        "same_uid_threat_boundary": SAME_UID_THREAT_BOUNDARY,
        "resource_limits_applied": limits_ok,
        "resource_limits": _resource_limit_record() if limits_ok else None,
    }
    evidence.update(_runtime_evidence("linux-unshare" if passed else "none"))
    status = "available" if passed else "BLOCKED"
    return IsolationCapability(status, evidence["backend"], evidence["reason"], evidence)


def _loopback_result() -> str:
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        sock.connect(("127.0.0.1", 1))
    except OSError as exc:
        return errno.errorcode.get(exc.errno, "OSError")
    else:
        return "opened"
    finally:
        sock.close()


def _sandbox_env(workspace: Path, mode: str, libs: list[tuple[str, str]], targets: dict | None) -> dict[str, str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/work",
        "LANG": "C.UTF-8",
        "TMPDIR": "/tmp",
        "PYTHONDONTWRITEBYTECODE": "1",
        "W03_MODE": mode,
        "W03_WORKSPACE": str(workspace),
        "W03_LIBS": ":".join(src + "|" + dst for src, dst in libs),
        SYNTHETIC_CREDENTIAL_NAME: SYNTHETIC_CREDENTIAL_VALUE,
    }
    if targets is not None:
        env["W03_TRY_HOME"] = targets["home"]
        env["W03_TRY_REAL_HOME"] = targets["real_home"]
        env["W03_TRY_EXTERNAL"] = targets["external"]
        env["W03_TRY_CROSS"] = targets["cross"]
    return env


def _unshare_argv() -> list[str] | None:
    unshare = shutil.which("unshare")
    if not unshare:
        return None
    return [
        unshare,
        "--user", "--map-root-user",
        "--mount", "--net", "--pid",
        "--fork", "--kill-child",
        "--propagation", "private",
        sys.executable, "-I", "-B",
        str(Path(__file__).resolve()),
        "--sandbox-launch",
    ]


def _trusted_shell_files() -> list[tuple[str, str]] | None:
    """Return ``(host file, jail path)`` pairs for dash and its loader.

    The jail path is the path the kernel and dynamic linker look up, not a
    realpath that would hide a ``/lib64`` interpreter entry.
    """
    dash_real = os.path.realpath("/usr/bin/dash")
    if not dash_real.startswith("/usr/") or not os.path.isfile(dash_real):
        return None
    try:
        completed = _run_capped(
            ["ldd", dash_real],
            timeout=5,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    pairs = [(dash_real, "/usr/bin/dash")]
    for line in completed.stdout.decode("utf-8", "replace").splitlines():
        path = _ldd_path(line.strip())
        if path and os.path.isfile(path) and _abs_safe(path):
            pairs.append((os.path.realpath(path), path))
    interp = _elf_interpreter(dash_real)
    if interp and os.path.isfile(interp) and _abs_safe(interp):
        pairs.append((os.path.realpath(interp), interp))
    unique = []
    seen = set()
    for item in pairs:
        if item[1] not in seen:
            seen.add(item[1])
            unique.append(item)
    if len(unique) < 2:
        return None
    return unique


def _elf_interpreter(path: str) -> str | None:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if data[:4] != b"\x7fELF" or data[4] != 2:
        return None
    little = data[5] == 1
    endian = "<" if little else ">"
    import struct
    e_phoff = struct.unpack_from(endian + "Q", data, 32)[0]
    e_phentsize = struct.unpack_from(endian + "H", data, 54)[0]
    e_phnum = struct.unpack_from(endian + "H", data, 56)[0]
    for index in range(e_phnum):
        start = e_phoff + index * e_phentsize
        p_type = struct.unpack_from(endian + "I", data, start)[0]
        if p_type != 3:
            continue
        p_offset = struct.unpack_from(endian + "Q", data, start + 8)[0]
        p_filesz = struct.unpack_from(endian + "Q", data, start + 32)[0]
        raw = data[p_offset:p_offset + p_filesz].split(b"\x00", 1)[0]
        try:
            return raw.decode("ascii")
        except UnicodeError:
            return None
    return None


def _ldd_path(line: str) -> str | None:
    if "=>" in line:
        right = line.split("=>", 1)[1].strip().split()
        if right and right[0].startswith("/"):
            return right[0]
        return None
    if line.startswith("/"):
        return line.split()[0]
    return None


def _abs_safe(path: str) -> bool:
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        return False
    return ".." not in path.split("/")


def _remove_jail_mountpoint(workspace: Path) -> None:
    jail = Path(str(workspace).rstrip("/") + ".jail")
    if jail.is_symlink() or not jail.exists():
        return
    try:
        shutil.rmtree(jail)
    except OSError:
        pass


def _marker_present(ws_fd: int) -> bool:
    try:
        fd = os.open(".isolation-started", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=ws_fd)
    except OSError:
        return False
    os.close(fd)
    return True


def _enter_jail(workspace: str, libs: list[tuple[str, str]], *, mount_proc: bool) -> None:
    _mount("none", "/", None, _MS_REC | _MS_PRIVATE)
    jail = workspace.rstrip("/") + ".jail"
    os.makedirs(jail, 0o755, exist_ok=True)
    _mount("tmpfs", jail, "tmpfs", _MS_NOSUID | _MS_NODEV, "size=32m,mode=755")
    for name in ("work", "tmp", "proc", "bin", "dev", "lib", "lib64", "usr", "usr/bin"):
        os.makedirs(jail + "/" + name, 0o755, exist_ok=True)
    os.chmod(jail + "/tmp", 0o1777)
    _mount(workspace, jail + "/work", None, _MS_BIND)
    for src, dst in libs:
        os.makedirs(os.path.dirname(jail + dst), 0o755, exist_ok=True)
        _bind_ro(src, jail + dst)
    _bind_ro(libs[0][0], jail + "/bin/sh")
    _bind_ro("/dev/null", jail + "/dev/null")
    if mount_proc:
        _mount("proc", jail + "/proc", "proc", _MS_NOSUID | _MS_NODEV | _MS_NOEXEC)
    os.chroot(jail)
    os.chdir("/work")
    _drop_caps()


def _bind_ro(src: str, dst: str) -> None:
    fd = os.open(dst, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
    os.close(fd)
    _mount(src, dst, None, _MS_BIND)
    _mount(None, dst, None, _MS_BIND | _MS_REMOUNT | _MS_RDONLY | _MS_NOSUID | _MS_NODEV)


def _mount(src: str | None, target: str, fstype: str | None, flags: int, data: str | None = None) -> None:
    libc = _libc()
    src_b = None if src is None else src.encode()
    fs_b = None if fstype is None else fstype.encode()
    data_b = None if data is None else data.encode()
    if libc.mount(src_b, target.encode(), fs_b, ctypes.c_ulong(flags), data_b) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), target)


def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p]
    libc.mount.restype = ctypes.c_int
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    libc.capset.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    libc.capset.restype = ctypes.c_int
    return libc


def _drop_caps() -> None:
    libc = _libc()
    for cap in range(64):
        libc.prctl(24, ctypes.c_ulong(cap), 0, 0, 0)
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "no_new_privs")

    class Header(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

    class Data(ctypes.Structure):
        _fields_ = [
            ("effective", ctypes.c_uint32),
            ("permitted", ctypes.c_uint32),
            ("inheritable", ctypes.c_uint32),
        ]

    header = Header(0x20080522, 0)
    data = (Data * 2)()
    if libc.capset(ctypes.byref(header), data) != 0:
        raise OSError(ctypes.get_errno(), "capset")


def _mount_denied() -> bool:
    try:
        _mount("tmpfs", "/tmp", "tmpfs", _MS_NOSUID | _MS_NODEV)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return False


def _write_probe_result() -> None:
    import socket
    result = {
        "home": os.environ.get("HOME"),
        "real_home": _attempt_open(os.environ.get("W03_TRY_REAL_HOME", "")),
        "fake_home": _attempt_open(os.environ.get("W03_TRY_HOME", "")),
        "external": _attempt_open(os.environ.get("W03_TRY_EXTERNAL", "")),
        "cross": _attempt_open(os.environ.get("W03_TRY_CROSS", "")),
        "credential_env_present": any(_is_credential_name(name) for name in os.environ),
        "synthetic_present": os.environ.get(SYNTHETIC_CREDENTIAL_NAME) == SYNTHETIC_CREDENTIAL_VALUE,
        "mount_denied": _mount_denied(),
    }
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        sock.connect(("127.0.0.1", 1))
        result["loopback"] = "opened"
    except OSError as exc:
        result["loopback"] = errno.errorcode.get(exc.errno, "OSError")
    finally:
        sock.close()
    status = open("/proc/self/status", "r", encoding="ascii").read().splitlines()
    cap = next((line.split(":", 1)[1].strip() for line in status if line.startswith("CapEff:")), "")
    nnp = next((line.split(":", 1)[1].strip() for line in status if line.startswith("NoNewPrivs:")), "")
    result["cap_eff_zero"] = cap == "0000000000000000"
    result["no_new_privs"] = nnp == "1"
    mapped = open("/proc/self/uid_map", "r", encoding="ascii").read().split()
    result["mapped_uid"] = int(mapped[1]) if len(mapped) >= 2 else -1
    net = open("/proc/net/dev", "r", encoding="ascii").read().splitlines()
    faces = [line.split(":", 1)[0].strip() for line in net if ":" in line]
    result["only_lo"] = faces == ["lo"]
    limits = _probe_limits()
    result["limits_applied"] = limits is not None
    if limits is not None:
        result.update(limits)
    external = os.environ.get("W03_TRY_EXTERNAL", "")
    shell = subprocess.run(
        ["/bin/sh", "-c", "cat \"$1\" > /work/shell-out 2> /work/shell-err", "sh", external],
        check=False,
        env={"PATH": "/bin:/usr/bin", "HOME": "/work"},
        timeout=5,
    )
    del shell
    try:
        size = os.path.getsize("/work/shell-out")
    except OSError:
        size = 0
    result["shell_external"] = "opened" if size else "denied"
    encoded = json.dumps(result, sort_keys=True).encode("utf-8")
    fd = os.open("/work/probe-result.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)


def _attempt_open(path: str) -> str:
    if not path:
        return "absent"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        return errno.errorcode.get(exc.errno, "OSError")
    os.close(fd)
    return "opened"


def _probe_limits() -> dict | None:
    """Apply the candidate rlimits in a child and record the hard values that stuck."""
    pid = os.fork()
    if pid == 0:
        try:
            _apply_candidate_limits(LIMIT_NPROC, LIMIT_CPU_SECONDS, LIMIT_AS_BYTES, LIMIT_FSIZE_BYTES)
            payload = json.dumps({
                "nproc_hard": resource.getrlimit(resource.RLIMIT_NPROC)[1],
                "cpu_hard": resource.getrlimit(resource.RLIMIT_CPU)[1],
                "as_hard": resource.getrlimit(resource.RLIMIT_AS)[1],
                "fsize_hard": resource.getrlimit(resource.RLIMIT_FSIZE)[1],
            }).encode("utf-8")
            fd = os.open("/work/limits.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
        except Exception:
            os._exit(1)
        os._exit(0)
    _child, status = os.waitpid(pid, 0)
    if status != 0:
        return None
    try:
        with open("/work/limits.json", "rb") as handle:
            raw = handle.read()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    expected = {
        "nproc_hard": LIMIT_NPROC,
        "cpu_hard": LIMIT_CPU_SECONDS,
        "as_hard": LIMIT_AS_BYTES,
        "fsize_hard": LIMIT_FSIZE_BYTES,
    }
    if data != expected:
        return None
    return data


def _exec_candidate() -> int:
    if os.environ.get("W03_FORCE_LIMIT_FAILURE") == "1":
        return 3
    limits = _read_exec_limits()
    if limits is None:
        return 3
    try:
        _apply_candidate_limits(*limits)
    except (OSError, ValueError):
        return 3
    fd = os.open("/work/.isolation-started", os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.write(fd, b"1")
    os.close(fd)
    clean = {
        "PATH": "/bin:/usr/bin",
        "HOME": "/work",
        "LANG": "C.UTF-8",
        "TMPDIR": "/tmp",
        "PYTHONDONTWRITEBYTECODE": "1",
        SYNTHETIC_CREDENTIAL_NAME: SYNTHETIC_CREDENTIAL_VALUE,
    }
    try:
        os.execve("/bin/sh", ["sh", "/work/candidate.sh"], clean)
    except OSError:
        try:
            os.unlink("/work/.isolation-started")
        except OSError:
            pass
        return 3
    return 3


def _claim_name(business_key: tuple[str, ...]) -> str:
    if not business_key or any((not isinstance(part, str) or "\n" in part) for part in business_key):
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    return hashlib.sha256("\n".join(business_key).encode("utf-8")).hexdigest()


def _write_new_file(dir_fd: int, name: str, payload: dict) -> None:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        os.write(fd, body)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(dir_fd)


def _replace_file(dir_fd: int, name: str, payload: dict) -> None:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    temporary = name + ".tmp"
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        os.write(fd, body)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    os.fsync(dir_fd)


def _fd_exists(dir_fd: int, name: str) -> bool:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    os.close(fd)
    return True


def _read_json_fd(dir_fd: int, name: str):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        chunks = []
        while True:
            block = os.read(fd, 65536)
            if not block:
                break
            chunks.append(block)
            if sum(len(item) for item in chunks) > 1024 * 1024:
                return None
    finally:
        os.close(fd)
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def construct(
    session: IsolationSession,
    request: BuildRequest,
    *,
    parent: Path,
    host_environ: Mapping[str, str] | None = None,
    remover=None,
    paused: threading.Event | None = None,
    hold: threading.Event | None = None,
    store: DurableStore | None = None,
    probe: Callable[[], IsolationCapability] | None = None,
) -> dict:
    """Construct one isolated build and return a public receipt.

    Session lookup, execution, and the durable store are separate steps.
    The session is in-process only. Identity and policy failures do not
    write a claim. An untrusted script runs only after a fresh durable
    claim and only inside a probed sandbox; otherwise the result is
    ``ISOLATION_BLOCKED`` and the candidate is not started.
    """
    try:
        _validate_request(request)
        _authorize(request, host_environ)
        _reject_embedded_credentials(request, host_environ)
        if request.expected_revision != session.revision:
            raise IsolationError("CONFLICT_REVISION", "REVISION_CONFLICT", 409, "validate")
    except IsolationError as exc:
        return _failure_receipt(request, exc, "not_started", "skipped")

    payload_hash = _payload_hash(request)
    business_key = _business_key(request)
    status, looked_up = session.lookup(business_key, payload_hash)
    if status == "mismatch" and looked_up is not None:
        return _mismatch_receipt(request, looked_up)
    if status == "replay" and looked_up is not None:
        return _replay_receipt(looked_up)

    try:
        _check_parent(parent)
    except IsolationError as exc:
        return _failure_receipt(request, exc, "not_started", "skipped")

    if store is None:
        store = DurableStore(Path(parent) / ".w03-claims")
    try:
        decision = store.begin(business_key, payload_hash, request.run_id)
    except IsolationError as exc:
        return _failure_receipt(request, exc, "not_started", "skipped")
    early = _decision_receipt(request, session, business_key, payload_hash, decision)
    if early is not None:
        return early

    script = request.script_text or ""
    capability: IsolationCapability | None = None
    if script:
        capability = probe() if probe is not None else probe_isolation()
        if capability.status != "available":
            store.abort(decision)
            receipt = _failure_receipt(
                request,
                IsolationError("POLICY_DENIED", "ISOLATION_BLOCKED", 403, "authorize"),
                "not_started",
                "skipped",
            )
            receipt["isolation_status"] = "BLOCKED"
            receipt["probe_evidence"] = copy.deepcopy(capability.evidence)
            receipt["commands_executed"] = 0
            receipt["exit_code"] = None
            return receipt

    lock_fd: int | None = None
    lock_path: Path | None = None
    workspace: Path | None = None
    ws_fd: int | None = None
    script_started = False
    exit_code: int | None = None
    try:
        lock_fd, lock_path = _acquire_lock(parent, request.candidate.app_id, request.run_id)
        if paused is not None:
            paused.set()
        if hold is not None and not hold.wait(HOLD_TIMEOUT_SECONDS):
            raise IsolationError("SIDE_EFFECT_UNKNOWN", "HOLD_TIMEOUT", 409, "reserve")
        workspace, ws_fd = _prepare_workspace(parent, request)
        env = build_environment(workspace, request.env_allowlist)
        _reject_inherited_values(env, host_environ)
        if env.get("HOME") == str(Path.home()):
            raise IsolationError("POLICY_DENIED", "HOST_TOKEN_INHERITANCE_DENIED", 403, "execute")
        _materialize(ws_fd, request.artifacts, host_environ)
        database_probe = _apply_database(session, workspace, ws_fd, request)
        _execute_plan(ws_fd, request)
        commands = 0
        if script:
            result = execute_isolated(ws_fd, workspace, script)
            script_started = result.started
            if result.reason == "reap_unconfirmed" or (result.started and not result.tree_terminated):
                return _unknown_receipt(request)
            if result.reason in {"output_limit", "timeout"}:
                if not result.tree_terminated:
                    return _unknown_receipt(request)
                exit_code = -signal.SIGKILL
                raise IsolationError("EXECUTION_FAILED", "RESOURCE_LIMIT", 409, "execute")
            if result.exit_code is None:
                raise IsolationError("POLICY_DENIED", "ISOLATION_BLOCKED", 403, "execute")
            if not result.tree_terminated:
                return _unknown_receipt(request)
            exit_code = result.exit_code
            commands = 1
            if exit_code < 0 and -exit_code in {signal.SIGXCPU, signal.SIGXFSZ, signal.SIGKILL}:
                raise IsolationError("EXECUTION_FAILED", "RESOURCE_LIMIT", 409, "execute")
            if exit_code != 0:
                raise IsolationError("EXECUTION_FAILED", "SCRIPT_FAILED", 409, "execute")
        return _commit_success(
            session, store, decision, business_key, payload_hash, request, env,
            database_probe, ws_fd, capability, commands, exit_code, workspace, remover,
        )
    except IsolationError as exc:
        return _fail_after_claim(
            session, store, decision, business_key, payload_hash, request, exc,
            workspace, ws_fd, remover, capability, script_started, exit_code,
        )
    finally:
        if ws_fd is not None:
            os.close(ws_fd)
        if lock_fd is not None:
            os.close(lock_fd)
        if lock_path is not None:
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass


def verify_host_identity(observation: Mapping[str, object]) -> dict:
    """A04 simulated check. A caller claim cannot set ``host_acl_effective``."""
    findings = []
    if observation.get("service_runs_as_installer_uid") and observation.get("service_home_is_installer_home"):
        findings.append({
            "code": "SAME_UID_HOME",
            "summary": (
                "install_job runs the service as the installer uid and sets HOME to the "
                "installer home. Directory mode does not isolate another process with that uid."
            ),
        })
    if observation.get("same_uid_directory_mode_is_isolation") is True or observation.get("host_acl_effective") is True:
        findings.append({
            "code": "CLAIM_REJECTED",
            "summary": "Simulated verification is not evidence that a host ACL is in effect.",
        })
    if observation.get("runtime_measured") is not False:
        findings.append({
            "code": "NOT_RUNTIME_MEASURED",
            "summary": "A04 was reviewed from source. This phase did not measure a live host.",
        })
    return _item_report("A04", findings, False, False)


def verify_database_boundary(observation: Mapping[str, object]) -> dict:
    """A05 simulated check. Trust auth and a same-uid socket are not an inter-app ACL."""
    findings = []
    if observation.get("initdb_auth") == "trust" and observation.get("role") == "app_owner":
        findings.append({
            "code": "TRUST_SAME_UID",
            "summary": (
                "The frozen installer uses initdb -A trust -U app_owner and a Unix socket "
                "under /private/tmp. That does not prove one app cannot read another."
            ),
        })
    if observation.get("same_uid_trust_is_inter_app_acl") is True or observation.get("db_acl_effective") is True:
        findings.append({
            "code": "CLAIM_REJECTED",
            "summary": "Simulated verification is not evidence that a database ACL is in effect.",
        })
    if observation.get("production_database_contacted") is True:
        findings.append({
            "code": "PRODUCTION_CONTACT_FORBIDDEN",
            "summary": "This phase must not contact a production database.",
        })
    if observation.get("runtime_measured") is not False:
        findings.append({
            "code": "NOT_RUNTIME_MEASURED",
            "summary": "A05 was reviewed from source. This phase did not open a host socket.",
        })
    return _item_report("A05", findings, False, False)


def verify_build_inheritance(observation: Mapping[str, object]) -> dict:
    """A06 simulated check of the host pytest and build path. The path is not modified here."""
    findings = []
    if observation.get("host_pytest_keeps_home") is True:
        findings.append({
            "code": "HOST_PYTEST_KEEPS_HOME",
            "summary": "test_app uses env -i and still passes the host HOME through.",
        })
    if observation.get("host_build_follows_load_env") is True:
        findings.append({
            "code": "BUILD_INHERITS_LOAD_ENV",
            "summary": (
                "update and prepare call load_env before build. load_env can export "
                "APP_GITHUB_TOKEN into that same process environment."
            ),
        })
    if observation.get("host_npm_ignore_scripts") is False:
        findings.append({
            "code": "LIFECYCLE_SCRIPTS_NOT_DISABLED",
            "summary": (
                "The host build invokes npm ci or npm install without --ignore-scripts. "
                "The Docker test path does pass --ignore-scripts; that does not cover the host path."
            ),
        })
    if observation.get("leak_measured_on_host") is True or observation.get("host_acl_effective") is True:
        findings.append({
            "code": "CLAIM_REJECTED",
            "summary": "A host credential leak was not measured here, and this result is not that measurement.",
        })
    if observation.get("manage_sh_modified_by_w03") is True or observation.get("runner_modified_by_w03") is True:
        findings.append({
            "code": "SHARED_FILE_UNEXPECTED",
            "summary": "W03 must not modify manage.sh or the existing MacCI runner.",
        })
    return _item_report("A06", findings, False, False)


def run_isolation_verification(observations: Mapping[str, Mapping[str, object]] | None = None) -> dict:
    """Run the A04/A05/A06 verification and the in-process fixture exercises."""
    source = observations or BASELINE_OBSERVATIONS
    exercises = _run_fixture_exercises()
    report = {
        "schema_version": SCHEMA_VERSION,
        "phase": "isolated_preparation",
        "baseline_sha": BASELINE_SHA,
        "simulated": True,
        "runtime_measured": False,
        "host_acl_effective": False,
        "db_acl_effective": False,
        "simulated_verification_is_host_acl_proof": False,
        "simulated_verification_is_db_acl_proof": False,
        "items": {
            "A04": verify_host_identity(source.get("A04", {})),
            "A05": verify_database_boundary(source.get("A05", {})),
            "A06": verify_build_inheritance(source.get("A06", {})),
        },
        "exercises": exercises,
        "real_machine_evidence_required": {
            item: list(entries) for item, entries in REAL_MACHINE_EVIDENCE.items()
        },
        "w02_alignment": {
            "status": W02_ALIGNMENT_STATUS,
            "approved_isolated_baseline_head": W02_BASELINE_HEAD,
            "bundled": False,
            "uses": "frozen_contract_test_double",
            "public_auth_interface": W02_PUBLIC_AUTH,
            "second_authentication_implementation": False,
            "pending": list(W02_ALIGNMENT_PENDING),
        },
        "same_uid_threat_boundary": SAME_UID_THREAT_BOUNDARY,
        "macci_evidence": "pending",
        "a04_a05_real_machine_status": "pending",
        "production_verification_enabled": False,
        "runtime_target": RUNTIME_TARGET,
        "macci_runtime": "BLOCKED",
        "host_downgrade": False,
        "trusted_execution_identity": False,
        "self_reported_executor_is_identity": False,
        "ownership_exceptions": [dict(item) for item in OWNERSHIP_EXCEPTIONS],
        "linux_namespace_probe_is_macci_evidence": False,
        "linux_namespace_probe_is_host_acl_proof": False,
        "isolation_probe": exercises.get("isolation_probe"),
        "blockers": [
            "W02 identity-boundary final review is still required before full integration.",
            "The public auth entry for later alignment is app/control_authz. app.auth.authorize_request is not that entry.",
            "The self-reported executor label is not a trusted execution identity.",
            "A04 and A05 real Mac host and database-role isolation remain unverified and are listed separately from the Linux probe.",
            "The W03 runtime target is macOS. Linux unshare is an optional adapter. MacCI container capabilities this host cannot exercise stay BLOCKED.",
            "A Linux user-namespace probe, when it runs, is not MacCI evidence and is not a host or database ACL.",
            "Simulated source checks do not show that host or database ACLs are in effect.",
            "The docs/v060/02-architecture-contract.md wording change from 六行 to 七行 is a coordinated ownership exception and is not expanded.",
            "manage.sh remains the host build path and stays reserved for W07.",
            "The existing MacCI runner was not modified.",
            "Production enablement, deployment, and database migration were not authorized.",
        ],
    }
    report["host_acl_effective"] = False
    report["db_acl_effective"] = False
    for item in report["items"].values():
        item["host_acl_effective"] = False
        item["db_acl_effective"] = False
    return report


def main(argv: list[str] | None = None) -> int:
    """Execute the isolation verification and print a JSON report."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--sandbox-launch"]:
        return sandbox_launch()
    if args != ["--verify"]:
        sys.stderr.write("usage: isolated_build.py --verify\n")
        return 2
    report = run_isolation_verification()
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    if report["host_acl_effective"] or report["db_acl_effective"]:
        return 2
    if report["simulated"] is not True or report["simulated_verification_is_host_acl_proof"] is not False:
        return 2
    if report["exercises"]["failed"]:
        return 1
    return 0


def _item_report(item: str, findings: list[dict[str, str]], host_acl: bool, db_acl: bool) -> dict:
    return {
        "item": item,
        "simulated": True,
        "runtime_measured": False,
        "host_acl_effective": host_acl,
        "db_acl_effective": db_acl,
        "findings": findings,
        "real_machine_evidence_required": list(REAL_MACHINE_EVIDENCE[item]),
    }


def _run_fixture_exercises() -> dict:
    import tempfile

    names: list[str] = []
    failures: list[dict[str, str]] = []

    def check(name: str, fn) -> None:
        names.append(name)
        try:
            fn()
        except IsolationError as exc:
            failures.append({"name": name, "error": "IsolationError", "detail": exc.detail})
        except Exception as exc:
            failures.append({"name": name, "error": type(exc).__name__, "detail": "EXERCISE_FAILED"})

    def sample(app_id: str, run_id: str, key: str, *, script: str | None = None, target: str = "", mode: str = "denied"):
        artifacts = (InputArtifact("readme.txt", b"alpha\n", 0o644),)
        return BuildRequest(
            actor=Actor("test", "fixture", "test-proof://w03/exercise", app_id),
            candidate=Candidate(
                "layho/web-app-init-base-env", "tsk_w03_exercise", app_id,
                "a" * 40, "b" * 40, "c" * 40, "d" * 40,
            ),
            artifacts=artifacts,
            declared_digest=artifact_digest(artifacts),
            env_allowlist=DEFAULT_ENV_ALLOWLIST,
            permissions=PermissionProfile(database_mode=mode),
            database_mode=mode,
            run_id=run_id,
            idempotency_key=key,
            script_text=script,
            plan=(BuildStep("write_output", "output/marker.txt", b"ok\n"),),
            database_target=target,
        )

    token = "FIXTURE_HOST_TOKEN_9f3c"

    def scrub() -> None:
        with tempfile.TemporaryDirectory(prefix="w03-exercise-") as folder:
            session = IsolationSession()
            receipt = construct(
                session, sample("app_alpha", "exercise-scrub", "idem-w03-exercise-scrub"),
                parent=Path(folder), host_environ={"APP_GITHUB_TOKEN": token, "HOME": "/tmp"},
            )
            if receipt["state"] != "SUCCEEDED" or token in json.dumps(receipt):
                raise IsolationError("POLICY_DENIED", "HOST_TOKEN_INHERITANCE_DENIED", 403, "execute")
            if "APP_GITHUB_TOKEN" in receipt["env_names"] or receipt["env_home_is_host_home"] is not False:
                raise IsolationError("POLICY_DENIED", "HOST_TOKEN_INHERITANCE_DENIED", 403, "execute")

    def script() -> None:
        with tempfile.TemporaryDirectory(prefix="w03-exercise-") as folder:
            sentinel = Path(folder) / "sentinel"
            sentinel.write_bytes(b"keep")
            receipt = construct(
                IsolationSession(),
                sample("app_alpha", "exercise-script", "idem-w03-exercise-script", script="cat ../../sentinel"),
                parent=Path(folder),
            )
            if sentinel.read_bytes() != b"keep":
                raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
            executed = receipt["commands_executed"]
            if executed and receipt.get("isolation_status") != "available":
                raise IsolationError("POLICY_DENIED", "ISOLATION_BLOCKED", 403, "execute")
            if not executed and receipt["detail_code"] not in {"ISOLATION_BLOCKED", "UNKNOWN_RESULT"}:
                raise IsolationError("POLICY_DENIED", "ISOLATION_BLOCKED", 403, "execute")

    def database() -> None:
        with tempfile.TemporaryDirectory(prefix="w03-exercise-") as folder:
            session = IsolationSession()
            receipt = construct(
                session,
                sample(
                    "app_alpha", "exercise-db", "idem-w03-exercise-db",
                    target="postgresql+psycopg://app_owner@/postgres?host=/private/tmp/label",
                ),
                parent=Path(folder),
            )
            if receipt["code"] != "PRODUCTION_DB_FORBIDDEN" or session.live_database_connects != 0:
                raise IsolationError("PRODUCTION_DB_FORBIDDEN", "DATABASE_DENIED", 403, "authorize")

    def mock() -> None:
        with tempfile.TemporaryDirectory(prefix="w03-exercise-") as folder:
            session = IsolationSession()
            receipt = construct(
                session, sample("app_alpha", "exercise-mock", "idem-w03-exercise-mock", mode="mock"),
                parent=Path(folder),
            )
            if receipt["state"] != "SUCCEEDED" or receipt["database_mode"] != "mock":
                raise IsolationError("POLICY_DENIED", "DATABASE_DENIED", 403, "execute")
            try:
                session.mock_db.get("app_beta", "app_alpha", "probe")
            except IsolationError as exc:
                if exc.detail != "CROSS_APP_DENIED":
                    raise
            else:
                raise IsolationError("POLICY_DENIED", "CROSS_APP_DENIED", 403, "authorize")

    probe_box: dict[str, dict] = {}

    def isolation() -> None:
        capability = probe_isolation()
        probe_box["evidence"] = capability.evidence
        if capability.evidence.get("host_acl_effective") or capability.evidence.get("db_acl_effective"):
            raise IsolationError("POLICY_DENIED", "PERMISSION_DENIED", 403, "execute")
        if capability.evidence.get("macci_evidence") != "pending":
            raise IsolationError("EVIDENCE_MISSING", "SCHEMA_INVALID", 409, "execute")
        if capability.evidence.get("candidate_executed") is not False:
            raise IsolationError("POLICY_DENIED", "SCRIPT_EXECUTION_DENIED", 403, "execute")
        if capability.evidence.get("runtime_target") != "macos":
            raise IsolationError("POLICY_DENIED", "ISOLATION_BLOCKED", 403, "execute")
        if capability.evidence.get("macci_runtime") != "BLOCKED":
            raise IsolationError("POLICY_DENIED", "ISOLATION_BLOCKED", 403, "execute")
        if capability.evidence.get("host_downgrade") is not False:
            raise IsolationError("POLICY_DENIED", "ISOLATION_BLOCKED", 403, "execute")
        if capability.evidence.get("trusted_execution_identity") is not False:
            raise IsolationError("POLICY_DENIED", "IDENTITY_REJECTED", 403, "execute")
        if capability.evidence.get("a04_a05_real_machine_status") != "pending":
            raise IsolationError("EVIDENCE_MISSING", "SCHEMA_INVALID", 409, "execute")
        if capability.evidence.get("production_verification_enabled") is not False:
            raise IsolationError("POLICY_DENIED", "PERMISSION_DENIED", 403, "execute")
        if capability.evidence.get("capture_output_unbounded") is not False:
            raise IsolationError("POLICY_DENIED", "ISOLATION_BLOCKED", 403, "execute")
        if "macOS" not in str(capability.evidence.get("macci_runtime_reason")):
            raise IsolationError("POLICY_DENIED", "ISOLATION_BLOCKED", 403, "execute")
        worked = _unshare_works()
        if worked and capability.status != "available":
            raise IsolationError("POLICY_DENIED", "ISOLATION_BLOCKED", 403, "execute")
        if not worked and capability.status != "BLOCKED":
            raise IsolationError("POLICY_DENIED", "ISOLATION_BLOCKED", 403, "execute")
        if capability.status != "available":
            return
        if capability.evidence.get("resource_limits_applied") is not True:
            raise IsolationError("POLICY_DENIED", "RESOURCE_LIMIT", 403, "execute")
        if capability.evidence.get("optional_adapter") != "linux-unshare":
            raise IsolationError("POLICY_DENIED", "ISOLATION_BLOCKED", 403, "execute")
        for key in (
            "home_inaccessible", "environment_inaccessible", "external_file_inaccessible",
            "network_inaccessible", "cross_app_inaccessible", "unprivileged",
        ):
            if capability.evidence.get(key) is not True:
                raise IsolationError("POLICY_DENIED", "ISOLATION_BLOCKED", 403, "execute")

    check("scrub_host_token", scrub)
    check("deny_escape_script", script)
    check("deny_production_database", database)
    check("mock_database_is_app_scoped", mock)
    check("isolation_probe", isolation)
    return {
        "passed": len(names) - len(failures),
        "failed": len(failures),
        "names": names,
        "failures": failures,
        "isolation_probe": probe_box.get("evidence"),
        "mock_database_is_security_boundary": False,
    }


def _validate_request(request: BuildRequest) -> None:
    if not isinstance(request, BuildRequest):
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    _require_actor_shape(request.actor)
    _require_candidate_shape(request.candidate)
    if not isinstance(request.artifacts, tuple) or not 1 <= len(request.artifacts) <= MAX_ARTIFACTS:
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    if not isinstance(request.plan, tuple) or len(request.plan) > MAX_PLAN_STEPS:
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    if not isinstance(request.env_allowlist, tuple) or len(request.env_allowlist) != len(set(request.env_allowlist)):
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    if not isinstance(request.env_values, tuple):
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    if not isinstance(request.permissions, PermissionProfile):
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    if request.database_mode not in DATABASE_MODES or request.permissions.database_mode != request.database_mode:
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    if not _RUN_ID.fullmatch(request.run_id) or not _IDEM.fullmatch(request.idempotency_key):
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    if type(request.expected_revision) is not int or request.expected_revision < 0:
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    if request.script_text is not None and (
        not isinstance(request.script_text, str) or len(request.script_text) > MAX_SCRIPT_CHARS
    ):
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    if not isinstance(request.database_target, str) or len(request.database_target) > 300:
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    seen_paths = set()
    for artifact in request.artifacts:
        if not isinstance(artifact, InputArtifact) or not isinstance(artifact.content, bytes):
            raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
        if artifact.mode not in ARTIFACT_MODES or len(artifact.content) > MAX_ARTIFACT_BYTES:
            raise IsolationError("POLICY_DENIED", "PERMISSION_DENIED", 403, "authorize")
        _reject_credential_relative(artifact.relative_path)
        relative = _pure_relative(artifact.relative_path)
        if relative in seen_paths:
            raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
        seen_paths.add(relative)
    for step in request.plan:
        if not isinstance(step, BuildStep) or step.op not in {"write_output", "read_input"}:
            raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
        if not isinstance(step.content, bytes) or len(step.content) > MAX_ARTIFACT_BYTES:
            raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
        _pure_relative(step.relative_path)
        if step.target_app_id and not _APP_ID.fullmatch(step.target_app_id):
            raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    if request.declared_digest != artifact_digest(request.artifacts):
        raise IsolationError("EVIDENCE_MISSING", "DIGEST_MISMATCH", 409, "validate")
    _validate_allowlist(request.env_allowlist)


def _authorize(request: BuildRequest, host_environ: Mapping[str, str] | None) -> None:
    actor = request.actor
    if actor.kind != "test" or not actor.proof_ref.startswith("test-proof://"):
        raise IsolationError("NOT_AUTHORIZED", "IDENTITY_REJECTED", 403, "authorize")
    if not _PROOF.fullmatch(actor.proof_ref) or ".." in actor.proof_ref:
        raise IsolationError("NOT_AUTHORIZED", "IDENTITY_REJECTED", 403, "authorize")
    if actor.app_id != request.candidate.app_id:
        raise IsolationError("POLICY_DENIED", "CROSS_APP_DENIED", 403, "authorize")
    if request.production_verification_requested:
        raise IsolationError("POLICY_DENIED", "PERMISSION_DENIED", 403, "authorize")
    profile = request.permissions
    if profile.network:
        raise IsolationError("POLICY_DENIED", "NETWORK_DENIED", 403, "authorize")
    if profile.lifecycle_scripts:
        raise IsolationError("POLICY_DENIED", "LIFECYCLE_SCRIPT_DENIED", 403, "authorize")
    if profile.host_home_access:
        raise IsolationError("POLICY_DENIED", "HOST_TOKEN_INHERITANCE_DENIED", 403, "authorize")
    if profile.foreign_app_access or profile.follow_symlinks:
        detail = "CROSS_APP_DENIED" if profile.foreign_app_access else "WORKSPACE_ESCAPE_DENIED"
        raise IsolationError("POLICY_DENIED", detail, 403, "authorize")
    if not profile.read_workspace:
        raise IsolationError("POLICY_DENIED", "PERMISSION_DENIED", 403, "authorize")
    if any(step.op == "write_output" for step in request.plan) and not profile.write_output:
        raise IsolationError("POLICY_DENIED", "PERMISSION_DENIED", 403, "authorize")
    if request.env_values:
        names = {name for name, _value in request.env_values}
        if any(_is_credential_name(name) for name in names):
            raise IsolationError("POLICY_DENIED", "HOST_TOKEN_INHERITANCE_DENIED", 403, "authorize")
        raise IsolationError("POLICY_DENIED", "ENV_VALUES_REJECTED", 403, "authorize")
    if request.database_target:
        classify_database_target(request.database_target)
    if host_environ:
        for name in host_environ:
            if name in request.env_allowlist and _is_credential_name(name):
                raise IsolationError("POLICY_DENIED", "HOST_TOKEN_INHERITANCE_DENIED", 403, "authorize")


def _validate_allowlist(allowlist: tuple[str, ...]) -> None:
    for name in allowlist:
        if _is_credential_name(name):
            raise IsolationError("POLICY_DENIED", "HOST_TOKEN_INHERITANCE_DENIED", 403, "authorize")
        if name not in FIXED_ENV_KEYS:
            raise IsolationError("POLICY_DENIED", "ENV_NOT_ALLOWLISTED", 403, "authorize")


def _reject_credential_relative(relative: str) -> None:
    if not isinstance(relative, str):
        raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "validate")
    for part in relative.split("/"):
        if part in HIDDEN_OR_CREDENTIAL_PARTS or part.startswith("."):
            raise IsolationError("POLICY_DENIED", "CREDENTIAL_ARTIFACT_DENIED", 403, "authorize")


def _reject_embedded_credentials(request: BuildRequest, host_environ: Mapping[str, str] | None) -> None:
    values = _credential_values(host_environ)
    for artifact in request.artifacts:
        if any(value.encode("utf-8") in artifact.content for value in values):
            raise IsolationError("POLICY_DENIED", "HOST_TOKEN_INHERITANCE_DENIED", 403, "authorize")
    if request.script_text and _text_contains_credential(request.script_text, host_environ):
        raise IsolationError("POLICY_DENIED", "HOST_TOKEN_INHERITANCE_DENIED", 403, "authorize")


def _reject_inherited_values(env: Mapping[str, str], host_environ: Mapping[str, str] | None) -> None:
    for value in _credential_values(host_environ):
        for env_value in env.values():
            if value in env_value:
                raise IsolationError("POLICY_DENIED", "HOST_TOKEN_INHERITANCE_DENIED", 403, "execute")
    if host_environ:
        for name in host_environ:
            if _is_credential_name(name) and name in env:
                raise IsolationError("POLICY_DENIED", "HOST_TOKEN_INHERITANCE_DENIED", 403, "execute")


def _prepare_workspace(parent: Path, request: BuildRequest) -> tuple[Path, int]:
    """Create an exclusive workspace and return a directory fd for it.

    ``O_NOFOLLOW`` rejects a symlink. It does not stop a same-UID peer from
    substituting another directory that uid owns. See ``SAME_UID_THREAT_BOUNDARY``.
    """
    _check_parent(parent)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    name = f"build-{request.run_id}"
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise IsolationError("SIDE_EFFECT_UNKNOWN", "WORKSPACE_UNAVAILABLE", 409, "execute") from exc
        ws_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    info = os.fstat(ws_fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        os.close(ws_fd)
        raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
    os.fchmod(ws_fd, 0o700)
    for child in ("tmp", "inputs", "output"):
        child_fd = _ensure_child_dir(ws_fd, child)
        os.close(child_fd)
    return Path(parent) / name, ws_fd


def _materialize(ws_fd: int, artifacts: tuple[InputArtifact, ...], host_environ: Mapping[str, str] | None) -> None:
    values = _credential_values(host_environ)
    for artifact in artifacts:
        if any(value.encode("utf-8") in artifact.content for value in values):
            raise IsolationError("POLICY_DENIED", "HOST_TOKEN_INHERITANCE_DENIED", 403, "execute")
        relative = "inputs/" + _pure_relative(artifact.relative_path)
        _write_tree_file(ws_fd, relative, artifact.content, artifact.mode)


def _apply_database(session: IsolationSession, workspace: Path, ws_fd: int, request: BuildRequest) -> dict:
    mode = request.database_mode
    app_id = request.candidate.app_id
    if mode == "denied":
        return {"engine": "none", "contained": True, "same_app": True}
    if mode == "mock":
        session.mock_db.put(app_id, "probe", app_id)
        seen = session.mock_db.get(app_id, app_id, "probe")
        if seen != app_id or session.live_database_connects != 0:
            raise IsolationError("POLICY_DENIED", "DATABASE_DENIED", 403, "execute")
        return {"engine": "mock", "contained": True, "same_app": True, "security_boundary": False}
    path = create_temporary_database(workspace, app_id, root_fd=ws_fd)
    if os.path.commonpath((os.path.abspath(workspace), os.path.abspath(path))) != os.path.abspath(workspace):
        raise IsolationError("POLICY_DENIED", "DATABASE_DENIED", 403, "execute")
    if stat.S_IMODE(path.stat().st_mode) != 0o600 or session.live_database_connects != 0:
        raise IsolationError("POLICY_DENIED", "DATABASE_DENIED", 403, "execute")
    return {"engine": "sqlite", "contained": True, "same_app": True}


def _execute_plan(ws_fd: int, request: BuildRequest) -> None:
    app_id = request.candidate.app_id
    for step in request.plan:
        if step.target_app_id and step.target_app_id != app_id:
            raise IsolationError("POLICY_DENIED", "CROSS_APP_DENIED", 403, "execute")
        relative = _pure_relative(step.relative_path)
        if step.op == "read_input":
            if not relative.startswith("inputs/"):
                raise IsolationError("POLICY_DENIED", "PERMISSION_DENIED", 403, "execute")
            _read_no_follow(ws_fd, relative)
            continue
        if not relative.startswith("output/") or relative == "output":
            raise IsolationError("POLICY_DENIED", "PERMISSION_DENIED", 403, "execute")
        _write_tree_file(ws_fd, relative, step.content, 0o644)


def _translate_open_error(exc: OSError) -> IsolationError:
    if exc.errno == errno.ENOENT:
        return IsolationError("EVIDENCE_MISSING", "INPUT_MISSING", 409, "execute")
    if exc.errno == errno.EEXIST:
        return IsolationError("SIDE_EFFECT_UNKNOWN", "WORKSPACE_UNAVAILABLE", 409, "execute")
    return IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")


def _walk_no_follow(root_fd: int, relative: str, *, allow_missing_final: bool) -> None:
    parts = relative.split("/")
    fd = os.dup(root_fd)
    try:
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if not last:
                flags |= os.O_DIRECTORY
            try:
                nxt = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if last and allow_missing_final:
                    return
                raise IsolationError("EVIDENCE_MISSING", "INPUT_MISSING", 409, "execute") from None
            except OSError as exc:
                raise _translate_open_error(exc) from None
            info = os.fstat(nxt)
            if not last and not stat.S_ISDIR(info.st_mode):
                os.close(nxt)
                raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
            if last and stat.S_ISLNK(info.st_mode):
                os.close(nxt)
                raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
            os.close(fd)
            fd = nxt
    finally:
        os.close(fd)


def _ensure_child_dir(dir_fd: int, name: str) -> int:
    if "/" in name or name in {"", ".", ".."}:
        raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        try:
            os.mkdir(name, 0o700, dir_fd=dir_fd)
        except OSError as exc:
            raise _translate_open_error(exc) from None
        try:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
        except OSError as exc:
            raise _translate_open_error(exc) from None
    except OSError as exc:
        raise _translate_open_error(exc) from None
    os.fchmod(fd, 0o700)
    return fd


def _write_exclusive_at(dir_fd: int, name: str, content: bytes, mode: int) -> None:
    if "/" in name or name in {"", ".", ".."}:
        raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(name, flags, mode, dir_fd=dir_fd)
    except OSError as exc:
        raise _translate_open_error(exc) from None
    try:
        os.write(fd, content)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _write_tree_file(root_fd: int, relative: str, content: bytes, mode: int) -> None:
    parts = relative.split("/")
    parent = root_fd
    opened: list[int] = []
    try:
        for part in parts[:-1]:
            parent = _ensure_child_dir(parent, part)
            opened.append(parent)
        _write_exclusive_at(parent, parts[-1], content, mode)
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _read_no_follow(root_fd: int, relative: str) -> bytes:
    parts = relative.split("/")
    parent = root_fd
    opened: list[int] = []
    try:
        for part in parts[:-1]:
            try:
                parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            except OSError as exc:
                raise _translate_open_error(exc) from None
            opened.append(parent)
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        except OSError as exc:
            raise _translate_open_error(exc) from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
            return os.read(fd, MAX_ARTIFACT_BYTES + 1)
        finally:
            os.close(fd)
    finally:
        for fd in reversed(opened):
            os.close(fd)


def retain_build_artifacts(parent: Path, run_id: str, ws_fd: int, output_digest: str) -> dict:
    """Copy ``output/`` aside and hash that copy before the workspace can be removed."""
    if not isinstance(parent, Path) or not _RUN_ID.fullmatch(run_id):
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    if not output_digest.startswith("sha256:") or output_digest != _output_digest_fd(ws_fd):
        raise IsolationError("EVIDENCE_MISSING", "ARTIFACT_NOT_VERIFIED", 409, "execute")
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        artifacts_fd = _ensure_child_dir(parent_fd, ".w03-artifacts")
        try:
            try:
                os.mkdir(run_id, 0o700, dir_fd=artifacts_fd)
            except FileExistsError as exc:
                raise IsolationError("SIDE_EFFECT_UNKNOWN", "WORKSPACE_UNAVAILABLE", 409, "execute") from exc
            retain_fd = os.open(run_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=artifacts_fd)
            try:
                _copy_output_tree(ws_fd, retain_fd)
            finally:
                os.close(retain_fd)
        finally:
            os.close(artifacts_fd)
    finally:
        os.close(parent_fd)
    checked = verify_retained_artifacts(parent, run_id, output_digest)
    if not checked["verified"]:
        raise IsolationError("EVIDENCE_MISSING", "ARTIFACT_NOT_VERIFIED", 409, "execute")
    return checked


def verify_retained_artifacts(parent: Path, run_id: str, expected_digest: str) -> dict:
    """Re-read a retained output tree and compare its digest. The workspace is not required."""
    if not isinstance(parent, Path) or not _RUN_ID.fullmatch(run_id) or not str(expected_digest).startswith("sha256:"):
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    root_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        artifacts_fd = os.open(".w03-artifacts", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            retain_fd = os.open(run_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=artifacts_fd)
        finally:
            os.close(artifacts_fd)
    finally:
        os.close(root_fd)
    try:
        digest = _output_digest_fd(retain_fd)
        files = _retained_file_records(retain_fd)
    finally:
        os.close(retain_fd)
    return {
        "verified": digest == expected_digest,
        "digest": digest,
        "relative_root": ".w03-artifacts/" + run_id,
        "files": files,
    }


def _copy_output_tree(src_root: int, dst_root: int) -> None:
    src = os.open("output", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=src_root)
    try:
        dst = _ensure_child_dir(dst_root, "output")
        try:
            _copy_dir(src, dst)
        finally:
            os.close(dst)
    finally:
        os.close(src)


def _copy_dir(src: int, dst: int) -> None:
    for name in sorted(os.listdir(f"/proc/self/fd/{src}")):
        if name in {".", ".."} or name.startswith("."):
            continue
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=src)
        try:
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                child = _ensure_child_dir(dst, name)
                try:
                    _copy_dir(fd, child)
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
            payload = _read_capped(fd)
            _write_exclusive_at(dst, name, payload, stat.S_IMODE(info.st_mode))
        finally:
            os.close(fd)


def _read_capped(fd: int) -> bytes:
    chunks = []
    total = 0
    while True:
        block = os.read(fd, 65536)
        if not block:
            break
        total += len(block)
        if total > LIMIT_FSIZE_BYTES:
            raise IsolationError("EXECUTION_FAILED", "RESOURCE_LIMIT", 409, "execute")
        chunks.append(block)
    return b"".join(chunks)


def _retained_file_records(root_fd: int) -> list[dict]:
    records: list[dict] = []
    out = os.open("output", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    try:
        _record_tree(out, "output", records)
    finally:
        os.close(out)
    records.sort(key=lambda item: item["path"])
    return records


def _record_tree(dir_fd: int, prefix: str, records: list[dict]) -> None:
    for name in sorted(os.listdir(f"/proc/self/fd/{dir_fd}")):
        if name in {".", ".."} or name.startswith("."):
            continue
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        try:
            info = os.fstat(fd)
            relative = prefix + "/" + name
            if stat.S_ISDIR(info.st_mode):
                _record_tree(fd, relative, records)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
            payload = _read_capped(fd)
            records.append({
                "path": relative,
                "mode": f"{stat.S_IMODE(info.st_mode):04o}",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            })
        finally:
            os.close(fd)


def _output_digest_fd(ws_fd: int) -> str:
    lines: list[str] = []
    try:
        out = os.open("output", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=ws_fd)
    except OSError as exc:
        raise _translate_open_error(exc) from None
    try:
        _collect_output(out, "output", lines)
    finally:
        os.close(out)
    lines.sort()
    return "sha256:" + hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _collect_output(dir_fd: int, prefix: str, lines: list[str]) -> None:
    for name in sorted(os.listdir(f"/proc/self/fd/{dir_fd}")):
        if name in {".", ".."} or name.startswith("."):
            continue
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        except OSError as exc:
            raise _translate_open_error(exc) from None
        try:
            info = os.fstat(fd)
            relative = prefix + "/" + name
            if stat.S_ISDIR(info.st_mode):
                _collect_output(fd, relative, lines)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
            chunks = []
            total = 0
            while True:
                block = os.read(fd, 65536)
                if not block:
                    break
                total += len(block)
                if total > LIMIT_FSIZE_BYTES:
                    raise IsolationError("EXECUTION_FAILED", "RESOURCE_LIMIT", 409, "execute")
                chunks.append(block)
            digest = hashlib.sha256(b"".join(chunks)).hexdigest()
            lines.append(f"{stat.S_IMODE(info.st_mode):04o} {digest} {relative}")
        finally:
            os.close(fd)


def _empty_output_digest() -> str:
    return "sha256:" + hashlib.sha256(b"").hexdigest()


def _unshare_works() -> bool:
    unshare = shutil.which("unshare")
    if not unshare:
        return False
    try:
        proc = subprocess.Popen(
            [unshare, "--user", "--map-root-user", "--mount", "--net", "--pid", "--fork", "--kill-child", "true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin"},
            start_new_session=True,
        )
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        if "proc" in locals() and proc.poll() is None:
            _reap_process_tree(proc)
        return False
    return proc.returncode == 0


def _cleanup(workspace: Path, remover) -> tuple[str, str | None]:
    try:
        if workspace.is_symlink():
            return "failed", "OSError"
        if remover is None:
            shutil.rmtree(workspace)
        else:
            remover(workspace)
    except OSError as exc:
        return "failed", errno.errorcode.get(exc.errno or 0, "OSError")
    except Exception as exc:
        name = type(exc).__name__
        return "failed", name if name.isalpha() else "OSError"
    if workspace.exists() or workspace.is_symlink():
        return "failed", "OSError"
    return "removed", None


def _apply_cleanup(receipt: dict, cleanup_state: str, cleanup_error: str | None, workspace: Path | None) -> None:
    receipt["cleanup_state"] = cleanup_state
    if cleanup_error is not None:
        receipt["cleanup_error_class"] = cleanup_error
    if cleanup_state != "failed":
        return
    receipt["cleanup_detail_code"] = "CLEANUP_FAILED"
    if workspace is not None:
        receipt["workspace_path"] = str(workspace)
    if receipt["state"] == "SUCCEEDED":
        receipt["state"] = "FAILED"
        receipt["http"] = 409
        receipt["code"] = "SIDE_EFFECT_UNKNOWN"
        receipt["detail_code"] = "CLEANUP_FAILED"
        receipt["failure"] = _failure_body("SIDE_EFFECT_UNKNOWN", 409, "CLEANUP_FAILED", "cleanup", "failed")


def _check_parent(parent: Path) -> None:
    if not isinstance(parent, Path):
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    try:
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EMLINK}:
            raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute") from exc
        raise IsolationError("SIDE_EFFECT_UNKNOWN", "WORKSPACE_UNAVAILABLE", 409, "execute") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
        try:
            git = os.open(".git", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        except FileNotFoundError:
            git = None
        except OSError as exc:
            if exc.errno != errno.ELOOP:
                raise
            raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute") from exc
        if git is not None:
            os.close(git)
            raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
    finally:
        os.close(fd)
    real = Path(os.path.realpath(parent))
    home = Path(os.path.realpath(Path.home()))
    if real == home or real.as_posix() in TMP_ROOTS or (set(real.parts) & FORBIDDEN_PARENT_PARTS):
        raise IsolationError("POLICY_DENIED", "HOST_TOKEN_INHERITANCE_DENIED", 403, "execute")


def _acquire_lock(parent: Path, app_id: str, run_id: str) -> tuple[int, Path]:
    _check_parent(parent)
    lock_path = Path(parent) / f".reservation-{app_id}"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, 0o600)
    except FileExistsError as exc:
        raise IsolationError("RESERVATION_CONFLICT", "CONCURRENCY_CONFLICT", 409, "reserve") from exc
    try:
        os.write(fd, run_id.encode("ascii"))
    except OSError:
        os.close(fd)
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            pass
        raise IsolationError("SIDE_EFFECT_UNKNOWN", "WORKSPACE_UNAVAILABLE", 409, "reserve") from None
    return fd, lock_path


def _directory(workspace: Path) -> Path:
    if not isinstance(workspace, Path) or not workspace.is_dir() or workspace.is_symlink():
        raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
    return workspace


def _pure_relative(relative: str) -> str:
    if not isinstance(relative, str) or not relative or len(relative) > 180:
        raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "validate")
    if relative.startswith("/") or "\\" in relative or "\x00" in relative or ".." in relative:
        raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "validate")
    parts = relative.split("/")
    if any(not _REL_PART.fullmatch(part) for part in parts):
        raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "validate")
    return relative


def _require_actor_shape(actor: Actor) -> None:
    if not isinstance(actor, Actor) or not _ACTOR_ID.fullmatch(actor.id) or not _APP_ID.fullmatch(actor.app_id):
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    if not isinstance(actor.kind, str) or not isinstance(actor.proof_ref, str):
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")


def _require_candidate_shape(candidate: Candidate) -> None:
    if not isinstance(candidate, Candidate):
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    if not _REPO_ID.fullmatch(candidate.repo_id) or ".." in candidate.repo_id:
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    if not _TASK_ID.fullmatch(candidate.task_id) or not _APP_ID.fullmatch(candidate.app_id):
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    for sha in (candidate.head, candidate.base, candidate.merge, candidate.tree):
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")


def _canonical_manifest(artifacts: tuple[InputArtifact, ...]) -> bytes:
    lines = []
    for artifact in sorted(artifacts, key=lambda item: item.relative_path):
        if not isinstance(artifact.content, bytes):
            raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
        digest = hashlib.sha256(artifact.content).hexdigest()
        lines.append(f"{artifact.mode:04o} {digest} {_pure_relative(artifact.relative_path)}\n")
    return "".join(lines).encode("utf-8")


def _is_credential_name(name: str) -> bool:
    if not isinstance(name, str):
        return False
    upper = name.upper()
    return name in CREDENTIAL_ENV_NAMES or any(marker in upper for marker in CREDENTIAL_NAME_MARKERS)


def _credential_values(host_environ: Mapping[str, str] | None) -> tuple[str, ...]:
    if not host_environ:
        return ()
    values = []
    for name, value in host_environ.items():
        if isinstance(value, str) and len(value) >= 12 and _is_credential_name(name):
            values.append(value)
    return tuple(values)


def _text_contains_credential(text: str, host_environ: Mapping[str, str] | None) -> bool:
    return any(value in text for value in _credential_values(host_environ))


def _payload_hash(request: BuildRequest) -> str:
    script = request.script_text or ""
    plan_lines = [
        "|".join((
            step.op,
            step.relative_path,
            hashlib.sha256(step.content).hexdigest(),
            step.target_app_id,
        ))
        for step in request.plan
    ]
    flags = (
        f"read={int(request.permissions.read_workspace)}",
        f"write={int(request.permissions.write_output)}",
        f"database={request.database_mode}",
    )
    blob = "\n".join((
        candidate_id(request.candidate),
        request.declared_digest,
        ",".join(request.env_allowlist),
        ",".join(flags),
        "\n".join(plan_lines),
        hashlib.sha256(script.encode("utf-8")).hexdigest(),
        str(request.expected_revision),
        request.actor.id,
        request.actor.app_id,
    ))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _business_key(request: BuildRequest) -> tuple[str, ...]:
    return (
        request.actor.id,
        request.actor.app_id,
        request.candidate.repo_id,
        request.candidate.task_id,
        "ci_build",
        request.idempotency_key,
    )


def _common_fields(request: BuildRequest | None) -> dict:
    fields = {
        "schema_version": SCHEMA_VERSION,
        "operation": "ci_build",
        "phase": "isolated_preparation",
        "baseline_sha": BASELINE_SHA,
        "evidence_ref": EVIDENCE_REF,
        "simulated": True,
        "runtime_measured": False,
        "host_acl_effective": False,
        "db_acl_effective": False,
        "simulated_verification_is_host_acl_proof": False,
        "simulated_verification_is_db_acl_proof": False,
        "commands_executed": 0,
        "w02_alignment_status": W02_ALIGNMENT_STATUS,
        "w02_baseline_head": W02_BASELINE_HEAD,
        "w02_bundled": False,
        "production_verification_enabled": False,
        "executor": EXECUTOR,
        "trusted_execution_identity": False,
        "execution_identity": None,
        "self_reported_executor_is_identity": False,
        "w02_public_auth": W02_PUBLIC_AUTH,
        "actor_double_tests_only": True,
        "runtime_target": RUNTIME_TARGET,
        "macci_runtime": "BLOCKED",
        "host_downgrade": False,
        "a04_a05_real_machine_status": "pending",
        "artifact_retain": None,
        "process_tree_terminated": None,
        "exit_code": None,
        "execution_binding": None,
        "probe_evidence": None,
        "isolation_status": None,
        "candidate_tuple": None,
        "macci_evidence": "pending",
        "same_uid_threat_boundary": SAME_UID_THREAT_BOUNDARY,
    }
    if request is not None and isinstance(request, BuildRequest) and isinstance(request.candidate, Candidate):
        try:
            fields["candidate_id"] = candidate_id(request.candidate)
            fields["artifact_digest"] = request.declared_digest
            fields["app_id"] = request.candidate.app_id
            fields["run_id"] = request.run_id
            fields["idempotency_key"] = request.idempotency_key
            fields["candidate_tuple"] = {
                "head": request.candidate.head,
                "base": request.candidate.base,
                "merge": request.candidate.merge,
                "tree": request.candidate.tree,
            }
        except IsolationError:
            fields["candidate_id"] = None
    return fields


def _failure_body(code: str, http: int, detail: str, phase: str, cleanup: str) -> dict:
    return {
        "code": code,
        "http": http,
        "detail_code": detail,
        "phase": phase,
        "cleanup_state": cleanup,
        "host_acl_effective": False,
        "db_acl_effective": False,
        "simulated": True,
        "commands_executed": 0,
    }


def _failure_receipt(
    request: BuildRequest | None,
    exc: IsolationError,
    cleanup: str,
    idempotency: str,
    cleanup_error: str | None = None,
    workspace: Path | None = None,
) -> dict:
    receipt = _common_fields(request if isinstance(request, BuildRequest) else None)
    receipt.update({
        "state": "FAILED",
        "http": exc.http,
        "code": exc.code,
        "detail_code": exc.detail,
        "failure": _failure_body(exc.code, exc.http, exc.detail, exc.phase, cleanup),
        "cleanup_state": cleanup,
        "idempotency": idempotency,
        "database_mode": request.database_mode if isinstance(request, BuildRequest) else None,
        "env_names": [],
        "output_digest": None,
    })
    if cleanup_error is not None:
        receipt["cleanup_error_class"] = cleanup_error
        receipt["cleanup_detail_code"] = "CLEANUP_FAILED"
    if workspace is not None:
        receipt["workspace_path"] = str(workspace)
    return receipt


def _success_receipt(
    request: BuildRequest,
    env: Mapping[str, str],
    database_probe: Mapping[str, object],
    output_digest: str,
) -> dict:
    receipt = _common_fields(request)
    receipt.update({
        "state": "SUCCEEDED",
        "http": 200,
        "code": None,
        "detail_code": "NONE",
        "failure": None,
        "cleanup_state": "pending",
        "idempotency": "stored",
        "database_mode": request.database_mode,
        "database_probe": dict(database_probe),
        "env_allowlist": list(request.env_allowlist),
        "env_names": sorted(env),
        "env_path": env.get("PATH"),
        "env_home_is_host_home": env.get("HOME") == str(Path.home()),
        "output_digest": output_digest,
        "permissions": {
            "read_workspace": True,
            "write_output": request.permissions.write_output,
            "network": False,
            "follow_symlinks": False,
            "host_home_access": False,
            "foreign_app_access": False,
            "lifecycle_scripts": False,
            "database_mode": request.database_mode,
        },
    })
    return receipt


def _mismatch_receipt(request: BuildRequest, info: Mapping[str, object]) -> dict:
    receipt = _common_fields(request)
    receipt.update({
        "state": "FAILED",
        "http": 409,
        "code": "IDEMPOTENCY_PAYLOAD_MISMATCH",
        "detail_code": "SCHEMA_INVALID",
        "failure": _failure_body(
            "IDEMPOTENCY_PAYLOAD_MISMATCH", 409, "SCHEMA_INVALID", "idempotency", "not_started",
        ),
        "cleanup_state": "not_started",
        "idempotency": "mismatch",
        "database_mode": request.database_mode,
        "env_names": [],
        "output_digest": None,
        "original_result": info.get("original_result"),
        "current_state": info.get("current_state"),
    })
    return receipt


def _replay_receipt(stored: dict) -> dict:
    replay = copy.deepcopy(stored)
    replay["code"] = "IDEMPOTENCY_REPLAY"
    replay["idempotency"] = "replay"
    replay["original_result"] = stored.get("state")
    replay["current_state"] = stored.get("state")
    replay["commands_executed"] = 0
    replay.pop("workspace_path", None)
    return replay


if __name__ == "__main__":
    sys.exit(main())
