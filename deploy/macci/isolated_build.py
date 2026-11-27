"""Phase-1 isolated build construction for W03 items A04, A05, and A06.

This module prepares a candidate build in a caller-supplied temporary
directory. It is not wired into ``deploy/macci/runner.py`` or
``deploy/macmini/manage.sh``. Those host paths stay unchanged until W07.

The checks here use the frozen V0.6.0 contract and test doubles. They do
not read host credential files, open a production database, create a host
account, or enable a gate. A passing run is simulated verification only:
``host_acl_effective`` and ``db_acl_effective`` stay false.

W02 alignment is pending. The approved isolated baseline for later
integration is ``5dd0eb38e175a8a905836fbdbac5633ff9eb19ae``. This branch
does not import or vendor that tree. ``Actor`` is a test double of the
frozen identity fields (kind ``test``, ``test-proof://``), not the W02
principal in ``app/control_identity``.
"""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

SCHEMA_VERSION = 2
BASELINE_SHA = "b2011968a9c14845b89fe6d15dda4a595699e9ec"
W02_BASELINE_HEAD = "5dd0eb38e175a8a905836fbdbac5633ff9eb19ae"
W02_ALIGNMENT_STATUS = "pending_final_review"
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
    "INPUT_MISSING",
    "WORKSPACE_UNAVAILABLE",
    "SCHEMA_INVALID",
    "IDENTITY_REJECTED",
    "REVISION_CONFLICT",
})

W02_ALIGNMENT_PENDING = (
    "Replace Actor with the W02 app/control_identity principal: generation, scope, and no stored secret.",
    "Call the W02 policy authorize step before idempotency. This gate only follows the frozen order.",
    "Bind a MacCI signer that is distinct from the shared Owner login.",
    "Keep production_verification_enabled false. A test-proof:// string is not a production credential.",
    "Full integration remains blocked on the W02 identity-boundary final review.",
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
_CREDENTIAL_TEXT = re.compile(
    r"github-token|APP_GITHUB|\.env|load_env|\.npmrc|\.netrc|id_rsa|SSH_AUTH_SOCK|macmini-github",
    re.IGNORECASE,
)
_DATABASE_TEXT = re.compile(r"postgresql|\bpsql\b|app_owner|/private/tmp|DATABASE_URL", re.IGNORECASE)
_LIFECYCLE_TEXT = re.compile(
    r"\bnpm\b|\bpreinstall\b|\bpostinstall\b|\bprepare\b|\bprepublish\b",
    re.IGNORECASE,
)
_ESCAPE_TEXT = re.compile(r"\.\.|~|/etc(?:/|$)|/private/|/tmp/|/home/|/Users/|/root/")
_NETWORK_TEXT = re.compile(r"\bcurl\b|\bwget\b|\bssh\b|https?://")
_EXEC_TEXT = re.compile(r"\bsubprocess\b|\bos\.system\b|\beval\s*\(|\bexec\s*\(|`")


class IsolationError(Exception):
    """Bounded failure. ``str`` is the detail code and never a secret value."""

    def __init__(self, code: str, detail: str, http: int, phase: str):
        if detail not in DETAIL_CODES or code not in {
            "NOT_AUTHORIZED", "POLICY_DENIED", "PRODUCTION_DB_FORBIDDEN",
            "EVIDENCE_MISSING", "CONFLICT_REVISION", "RESERVATION_CONFLICT",
            "IDEMPOTENCY_PAYLOAD_MISMATCH", "SCHEMA_UNSUPPORTED", "SIDE_EFFECT_UNKNOWN",
        }:
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
    """Frozen-contract test identity.

    W02_ALIGNMENT: swap this double for the reviewed W02 principal. Do not
    treat ``id`` or a shared Owner login as a MacCI signer.
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
    """In-memory per-app double. It has no socket and no production role."""

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
    """In-process reservation and idempotency. Not a host flock and not W01 storage."""

    def __init__(self, revision: int = 1) -> None:
        self.revision = revision
        self.mock_db = MockDatabase()
        self.live_database_connects = 0
        self._idem: dict[tuple[str, ...], dict[str, object]] = {}
        self._lock = threading.Lock()

    def lookup(self, key: tuple[str, ...], payload_hash: str) -> tuple[str, dict | None]:
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

    def store(self, key: tuple[str, ...], payload_hash: str, receipt: Mapping[str, object]) -> str:
        with self._lock:
            current = self._idem.get(key)
            if current is None:
                self._idem[key] = {
                    "payload_hash": payload_hash,
                    "receipt": copy.deepcopy(dict(receipt)),
                }
                return "stored"
            if current["payload_hash"] == payload_hash:
                return "exists"
            return "mismatch"


def candidate_id(candidate: Candidate) -> str:
    """Frozen encoding: six lines, no trailing newline, then ``cand_`` + sha256."""
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


def classify_script(script_text: str) -> str | None:
    """Return a denial detail, or None when the text has no hostile pattern.

    The caller still does not execute the text. None only means the closed
    pattern set did not match.
    """
    if _CREDENTIAL_TEXT.search(script_text):
        return "HOST_TOKEN_INHERITANCE_DENIED"
    if _DATABASE_TEXT.search(script_text):
        return "DATABASE_DENIED"
    if _LIFECYCLE_TEXT.search(script_text):
        return "LIFECYCLE_SCRIPT_DENIED"
    if _ESCAPE_TEXT.search(script_text):
        return "WORKSPACE_ESCAPE_DENIED"
    if _NETWORK_TEXT.search(script_text):
        return "NETWORK_DENIED"
    if _EXEC_TEXT.search(script_text):
        return "SCRIPT_EXECUTION_DENIED"
    return None


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
    """Resolve ``relative`` under ``workspace`` without following symlinks."""
    workspace = _directory(workspace)
    relative = _pure_relative(relative)
    info = workspace.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
    current = workspace
    parts = relative.split("/")
    for index, part in enumerate(parts):
        current = current / part
        is_final = index == len(parts) - 1
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not is_final:
                raise IsolationError("EVIDENCE_MISSING", "INPUT_MISSING", 409, "execute") from None
            break
        if stat.S_ISLNK(info.st_mode):
            raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
        if not is_final and not stat.S_ISDIR(info.st_mode):
            raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
    workspace_abs = os.path.abspath(workspace)
    target_abs = os.path.abspath(current)
    if os.path.commonpath((workspace_abs, target_abs)) != workspace_abs:
        raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
    return current


def create_temporary_database(workspace: Path, app_id: str) -> Path:
    """Create a sqlite file inside ``workspace``. This is not a host cluster."""
    if not _APP_ID.fullmatch(app_id):
        raise IsolationError("SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", 422, "validate")
    database_dir = resolve_contained(workspace, "db")
    database_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(database_dir, 0o700)
    path = resolve_contained(workspace, "db/app.sqlite")
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    connection = sqlite3.connect(path)
    try:
        connection.execute("create table probe(app_id text primary key)")
        connection.execute("insert into probe(app_id) values (?)", (app_id,))
        connection.commit()
        row = connection.execute("select app_id from probe").fetchone()
    finally:
        connection.close()
    if row != (app_id,):
        raise IsolationError("EVIDENCE_MISSING", "DATABASE_DENIED", 409, "execute")
    return path


def construct(
    session: IsolationSession,
    request: BuildRequest,
    *,
    parent: Path,
    host_environ: Mapping[str, str] | None = None,
    remover=None,
    paused: threading.Event | None = None,
    hold: threading.Event | None = None,
) -> dict:
    """Construct one isolated build and return a public receipt.

    Identity and policy failures do not read or write idempotency state.
    Candidate shell text is classified and never executed. ``commands_executed``
    stays 0. Cleanup failure after an otherwise successful build becomes
    ``SIDE_EFFECT_UNKNOWN`` and does not claim the workspace was removed.
    """
    try:
        _validate_request(request)
        _authorize(request, host_environ)
        _reject_embedded_credentials(request, host_environ)
        _reject_script(request, host_environ)
        if request.expected_revision != session.revision:
            raise IsolationError("CONFLICT_REVISION", "REVISION_CONFLICT", 409, "validate")
    except IsolationError as exc:
        return _failure_receipt(request, exc, "not_started", "skipped")

    payload_hash = _payload_hash(request)
    idem_key = _idempotency_key(request)
    status, looked_up = session.lookup(idem_key, payload_hash)
    if status == "mismatch" and looked_up is not None:
        return _mismatch_receipt(request, looked_up)
    if status == "replay" and looked_up is not None:
        return _replay_receipt(looked_up)

    lock_fd: int | None = None
    lock_path: Path | None = None
    workspace: Path | None = None
    try:
        lock_fd, lock_path = _acquire_lock(parent, request.candidate.app_id, request.run_id)
        if paused is not None:
            paused.set()
        if hold is not None and not hold.wait(HOLD_TIMEOUT_SECONDS):
            raise IsolationError("SIDE_EFFECT_UNKNOWN", "HOLD_TIMEOUT", 409, "reserve")
        workspace = _prepare_workspace(parent, request)
        env = build_environment(workspace, request.env_allowlist)
        _reject_inherited_values(env, host_environ)
        if env.get("HOME") == str(Path.home()):
            raise IsolationError("POLICY_DENIED", "HOST_TOKEN_INHERITANCE_DENIED", 403, "execute")
        _materialize(workspace, request.artifacts, host_environ)
        database_probe = _apply_database(session, workspace, request)
        _execute_plan(workspace, request)
        receipt = _success_receipt(request, env, database_probe, workspace)
        cleanup_state, cleanup_error = _cleanup(workspace, remover)
        workspace_left = workspace if cleanup_state == "failed" else None
        workspace = None
        _apply_cleanup(receipt, cleanup_state, cleanup_error, workspace_left)
        if session.store(idem_key, payload_hash, receipt) == "mismatch":
            return _mismatch_receipt(request, {
                "original_result": "SUCCEEDED",
                "current_state": "SUCCEEDED",
            })
        return copy.deepcopy(receipt)
    except IsolationError as exc:
        cleanup_state = "not_started"
        cleanup_error = None
        workspace_left = None
        if workspace is not None:
            cleanup_state, cleanup_error = _cleanup(workspace, remover)
            if cleanup_state == "failed":
                workspace_left = workspace
            workspace = None
        receipt = _failure_receipt(request, exc, cleanup_state, "skipped", cleanup_error, workspace_left)
        return receipt
    finally:
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
            "pending": list(W02_ALIGNMENT_PENDING),
        },
        "blockers": [
            "W02 identity-boundary final review is still required before full integration.",
            "Simulated checks do not show that host or database ACLs are in effect.",
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
            if receipt["detail_code"] != "WORKSPACE_ESCAPE_DENIED" or receipt["commands_executed"] != 0:
                raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
            if sentinel.read_bytes() != b"keep":
                raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")

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

    check("scrub_host_token", scrub)
    check("deny_escape_script", script)
    check("deny_production_database", database)
    check("mock_database_is_app_scoped", mock)
    return {
        "passed": len(names) - len(failures),
        "failed": len(failures),
        "names": names,
        "failures": failures,
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


def _reject_script(request: BuildRequest, host_environ: Mapping[str, str] | None) -> None:
    script = request.script_text
    if script is None or script == "":
        return
    if _text_contains_credential(script, host_environ):
        raise IsolationError("POLICY_DENIED", "HOST_TOKEN_INHERITANCE_DENIED", 403, "authorize")
    detail = classify_script(script) or "SCRIPT_EXECUTION_DENIED"
    production_database = detail == "DATABASE_DENIED" and "postgres" in script.lower()
    code = "PRODUCTION_DB_FORBIDDEN" if production_database else "POLICY_DENIED"
    raise IsolationError(code, detail, 403, "authorize")


def _reject_inherited_values(env: Mapping[str, str], host_environ: Mapping[str, str] | None) -> None:
    for value in _credential_values(host_environ):
        for env_value in env.values():
            if value in env_value:
                raise IsolationError("POLICY_DENIED", "HOST_TOKEN_INHERITANCE_DENIED", 403, "execute")
    if host_environ:
        for name in host_environ:
            if _is_credential_name(name) and name in env:
                raise IsolationError("POLICY_DENIED", "HOST_TOKEN_INHERITANCE_DENIED", 403, "execute")


def _prepare_workspace(parent: Path, request: BuildRequest) -> Path:
    _check_parent(parent)
    workspace = Path(parent) / f"build-{request.run_id}"
    try:
        os.mkdir(workspace, 0o700)
    except FileExistsError as exc:
        raise IsolationError("SIDE_EFFECT_UNKNOWN", "WORKSPACE_UNAVAILABLE", 409, "execute") from exc
    os.chmod(workspace, 0o700)
    for relative in ("tmp", "inputs", "output"):
        path = resolve_contained(workspace, relative)
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
    return workspace


def _materialize(workspace: Path, artifacts: tuple[InputArtifact, ...], host_environ: Mapping[str, str] | None) -> None:
    values = _credential_values(host_environ)
    for artifact in artifacts:
        if any(value.encode("utf-8") in artifact.content for value in values):
            raise IsolationError("POLICY_DENIED", "HOST_TOKEN_INHERITANCE_DENIED", 403, "execute")
        relative = "inputs/" + _pure_relative(artifact.relative_path)
        path = resolve_contained(workspace, relative)
        _mkdir_parents(workspace, path.parent)
        _write_exclusive(path, artifact.content, artifact.mode)


def _apply_database(session: IsolationSession, workspace: Path, request: BuildRequest) -> dict:
    mode = request.database_mode
    app_id = request.candidate.app_id
    if mode == "denied":
        return {"engine": "none", "contained": True, "same_app": True}
    if mode == "mock":
        session.mock_db.put(app_id, "probe", app_id)
        seen = session.mock_db.get(app_id, app_id, "probe")
        if seen != app_id or session.live_database_connects != 0:
            raise IsolationError("POLICY_DENIED", "DATABASE_DENIED", 403, "execute")
        return {"engine": "mock", "contained": True, "same_app": True}
    path = create_temporary_database(workspace, app_id)
    if os.path.commonpath((os.path.abspath(workspace), os.path.abspath(path))) != os.path.abspath(workspace):
        raise IsolationError("POLICY_DENIED", "DATABASE_DENIED", 403, "execute")
    if stat.S_IMODE(path.stat().st_mode) != 0o600 or session.live_database_connects != 0:
        raise IsolationError("POLICY_DENIED", "DATABASE_DENIED", 403, "execute")
    return {"engine": "sqlite", "contained": True, "same_app": True}


def _execute_plan(workspace: Path, request: BuildRequest) -> None:
    app_id = request.candidate.app_id
    for step in request.plan:
        if step.target_app_id and step.target_app_id != app_id:
            raise IsolationError("POLICY_DENIED", "CROSS_APP_DENIED", 403, "execute")
        path = resolve_contained(workspace, step.relative_path)
        if step.op == "read_input":
            if not step.relative_path.startswith("inputs/"):
                raise IsolationError("POLICY_DENIED", "PERMISSION_DENIED", 403, "execute")
            try:
                info = path.lstat()
            except FileNotFoundError as exc:
                raise IsolationError("EVIDENCE_MISSING", "INPUT_MISSING", 409, "execute") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
            with open(path, "rb") as handle:
                handle.read(MAX_ARTIFACT_BYTES + 1)
            continue
        if not step.relative_path.startswith("output/") or step.relative_path == "output":
            raise IsolationError("POLICY_DENIED", "PERMISSION_DENIED", 403, "execute")
        _mkdir_parents(workspace, path.parent)
        _write_exclusive(path, step.content, 0o644)


def _mkdir_parents(workspace: Path, directory: Path) -> None:
    workspace_abs = os.path.abspath(workspace)
    current = workspace
    relative = directory.relative_to(workspace)
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
        if current.exists():
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
            continue
        os.mkdir(current, 0o700)
        os.chmod(current, 0o700)
        if os.path.commonpath((workspace_abs, os.path.abspath(current))) != workspace_abs:
            raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")


def _write_exclusive(path: Path, content: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, mode)
    try:
        os.write(fd, content)
    finally:
        os.close(fd)
    os.chmod(path, mode)


def _cleanup(workspace: Path, remover) -> tuple[str, str | None]:
    try:
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
        info = parent.lstat()
    except OSError as exc:
        raise IsolationError("SIDE_EFFECT_UNKNOWN", "WORKSPACE_UNAVAILABLE", 409, "execute") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
    if (parent / ".git").exists():
        raise IsolationError("POLICY_DENIED", "WORKSPACE_ESCAPE_DENIED", 403, "execute")
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


def _output_digest(workspace: Path) -> str:
    lines = []
    output = workspace / "output"
    for path in sorted(output.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(workspace).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"0644 {digest} {relative}")
    return "sha256:" + hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


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
        request.run_id,
        str(request.expected_revision),
        request.actor.id,
        request.actor.app_id,
    ))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _idempotency_key(request: BuildRequest) -> tuple[str, ...]:
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
    }
    if request is not None and isinstance(request, BuildRequest) and isinstance(request.candidate, Candidate):
        try:
            fields["candidate_id"] = candidate_id(request.candidate)
            fields["artifact_digest"] = request.declared_digest
            fields["app_id"] = request.candidate.app_id
            fields["run_id"] = request.run_id
            fields["idempotency_key"] = request.idempotency_key
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
    workspace: Path,
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
        "output_digest": _output_digest(workspace),
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
