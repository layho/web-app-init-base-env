"""Isolated lifecycle plans. Execution of host installers stays denied."""

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from app.control_lifecycle.errors import LifecycleError
from app.control_lifecycle.fdpath import containment_holds, fd_namespace
from app.control_lifecycle.grant import Authorizer
from app.control_lifecycle.journal import Journal
from app.control_lifecycle.platform import (
    ADAPTERS,
    DEPENDENCIES,
    DOMAIN_PATTERN,
    FINGERPRINT_PATTERN,
    HELP,
    REQUIRED_DISK_BYTES,
    SHA_PATTERN,
    USER_DATA,
    classify,
    legacy_install_path,
    logical_install_path,
    service_unit_digest,
)

REQUIRED_SCOPE = {
    "entry.plan": "lifecycle.plan",
    "preflight": "lifecycle.preflight",
    "path.resolve": "lifecycle.plan",
    "port.reserve": "lifecycle.reserve_port",
    "journal.note_counter_loss": "lifecycle.plan",
    "cloudflare.plan": "lifecycle.cloudflare",
    "cloudflare.apply_synthetic": "lifecycle.cloudflare",
    "recovery.plan": "lifecycle.recover",
    "recovery.apply_synthetic": "lifecycle.recover",
    "resource.observe": "lifecycle.plan",
    "cleanup.synthetic": "lifecycle.cleanup",
    "uninstall.plan": "lifecycle.uninstall",
    "rebuild.plan": "lifecycle.rebuild",
    "rebuild.apply_synthetic": "lifecycle.rebuild",
    "seed.observe": "lifecycle.plan",
    "service.plan": "lifecycle.service",
    "service.record_receipt": "lifecycle.service",
    "service.commit": "lifecycle.service",
    "fd.observe": "lifecycle.plan",
}

SECRET_KEYS = {"api_token", "token", "password", "secret", "authorization", "api_key"}
CHECK_ORDER = (
    "environment",
    "dependencies",
    "disk",
    "ports",
    "paths",
    "identity",
    "permissions",
    "background_credentials",
    "domain",
)
RELATIVE_NAME = re.compile(r"[A-Za-z0-9._-]{1,80}\Z")


class Result:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def __str__(self):
        return f"{self.code}: {self.diagnosis}"


class LifecycleService:
    def __init__(self, workspace, host, clock=None):
        self.workspace = Path(workspace)
        if self.workspace.is_symlink() or not self.workspace.is_dir():
            raise LifecycleError("UNSAFE_PATH", 403, "workspace is not a real directory")
        self.home = self.workspace / "home"
        self.inventory = self.workspace / "inventory"
        self.host = host
        self.clock = clock
        self.auth = Authorizer()
        self.journal = Journal(self.workspace / "lifecycle-journal.json", host.faults)

    def issue_grant(self, principal, scopes, object_id):
        return self.auth.issue(principal, scopes, object_id)

    def inspect(self):
        return self.journal.inspect()

    def call(self, grant, operation, payload, *, idempotency_key, expected_revision):
        try:
            self._precheck(grant, operation, payload)
        except LifecycleError as exc:
            return self._blocked(exc, operation, payload)
        scope = REQUIRED_SCOPE[operation]
        digest = _payload_hash(payload)
        run_id = payload.get("run_id") or ""
        return self.journal.transact(
            expected_revision=expected_revision,
            principal=grant.principal,
            scope=scope,
            operation=operation,
            idempotency_key=idempotency_key,
            payload_hash=digest,
            run_id=run_id,
            handler=lambda data: self._handle(operation, grant, payload, data),
            result_for=lambda code, http, ok, diagnosis, plan, simulated, data, original: self._make(
                code, http, ok, diagnosis, operation, payload, plan, simulated, data, original
            ),
        )

    def _precheck(self, grant, operation, payload):
        if not isinstance(payload, dict):
            raise LifecycleError("SCHEMA_UNSUPPORTED", 422, "payload must be an object")
        if "received_at" in payload:
            raise LifecycleError("SCHEMA_UNSUPPORTED", 422, "received_at is server-owned")
        _reject_secrets(payload)
        if operation not in REQUIRED_SCOPE:
            raise LifecycleError("SCHEMA_UNSUPPORTED", 422, "operation is not in the lifecycle contract")
        if not self.auth.authenticate(grant):
            raise LifecycleError("NOT_AUTHORIZED", 403, "server grant is missing")
        if REQUIRED_SCOPE[operation] not in grant.scopes:
            raise LifecycleError("POLICY_DENIED", 403, "scope does not allow this operation")
        if payload.get("app_id") != grant.object_id:
            raise LifecycleError("POLICY_DENIED", 403, "object is outside the server grant")
        if "home" in payload and not self._home_allowed(payload["home"]):
            raise LifecycleError("PATH_OUTSIDE_AUTHORIZATION", 403, "home is outside the authorized workspace", {"commands": []})

    def _home_allowed(self, value):
        try:
            requested = Path(value).resolve()
        except OSError:
            return False
        return requested == self.home.resolve()

    def _blocked(self, exc, operation, payload):
        state = self.inspect()
        return Result(
            ok=False,
            code=exc.code,
            http=exc.http,
            diagnosis=exc.diagnosis,
            operation=operation,
            run_id=str(payload.get("run_id") or "") if isinstance(payload, dict) else "",
            revision=state["revision"],
            host_side_effect=False,
            executed_commands=(),
            evidence_class="synthetic-test-double",
            host_capability_proof=False,
            macci_proof=False,
            test_double=True,
            original_result=None,
            current_state=state,
            plan=exc.plan,
            simulated=False,
        )

    def _make(self, code, http, ok, diagnosis, operation, payload, plan, simulated, data, original):
        state = self.journal._public(data)
        plan = dict(plan)
        plan.setdefault("run_id", payload.get("run_id"))
        return Result(
            ok=ok,
            code=code,
            http=http,
            diagnosis=diagnosis,
            operation=operation,
            run_id=str(payload.get("run_id") or ""),
            revision=state["revision"],
            host_side_effect=False,
            executed_commands=(),
            evidence_class="synthetic-test-double",
            host_capability_proof=False,
            macci_proof=False,
            test_double=True,
            original_result=original,
            current_state=state,
            plan=plan,
            simulated=simulated,
        )

    def _handle(self, operation, grant, payload, data):
        runs = data["runs"].get(payload.get("run_id"), [])
        if operation in runs:
            return _outcome("RUN_REENTRY", 409, False, "this run already recorded the operation")
        handler = getattr(self, "_op_" + operation.replace(".", "_"))
        return handler(grant, payload, data)

    def _op_entry_plan(self, grant, payload, data):
        adapter_id, supported = classify(self.host.probe)
        if adapter_id is None:
            return _outcome(
                "PLATFORM_UNSUPPORTED",
                422,
                False,
                "platform combination is outside the unverified pilot matrix",
                {"commands": [], "verified": False, "supported_unverified": False},
            )
        probe = self.host.probe
        if probe.get("uid") == 0 or probe.get("euid_is_root"):
            return _outcome(
                "PRIVILEGE_ESCALATION_DENIED",
                403,
                False,
                "root is refused and privilege escalation is disabled",
                {"commands": [], "verified": False},
            )
        if adapter_id == "darwin-launchd" and not probe.get("tty"):
            return _outcome("TTY_REQUIRED", 409, False, "Darwin entry requires a TTY in the server probe", {"commands": []})
        if payload.get("foundation_digest") != probe.get("expected_foundation_digest"):
            return _outcome("DIGEST_MISMATCH", 409, False, "foundation digest does not match the server pin", {"commands": []})
        service = probe.get("service") or {}
        if service.get("active"):
            expected = service_unit_digest(adapter_id, payload["repo"], payload["port"], service.get("scope_level") or "user")
            if service.get("unit_digest") != expected:
                return _outcome("UNIT_MISMATCH", 409, False, "installed unit digest does not match the plan", {"commands": []})
            if not self._cluster_complete(payload):
                return _outcome("CLUSTER_INCOMPLETE", 409, False, "database cluster marker is not intact; initdb was not run", {"commands": []})
            return _outcome("ALREADY_ACTIVE", 200, True, "matching install is already active", {"commands": [], "verified": False})
        return _outcome(
            "OK",
            200,
            True,
            "entry plan only",
            {
                "adapter_id": adapter_id,
                "commands": [],
                "verified": False,
                "supported_unverified": supported,
                "help": HELP,
                "forwarded": {
                    "repo": payload.get("repo"),
                    "domain": payload.get("domain"),
                    "port": payload.get("port"),
                    "track": payload.get("track"),
                    "title": payload.get("title"),
                    "parameters": payload.get("parameters") or {},
                },
            },
        )

    def _op_preflight(self, grant, payload, data):
        checks = {}
        failures = []
        extra = {"package_manager_executed": False}

        adapter_id, _supported = classify(self.host.probe)
        checks["environment"] = "pass" if adapter_id else "fail"
        if adapter_id is None:
            failures.append("PLATFORM_UNSUPPORTED")

        missing = [name for name in DEPENDENCIES if not (self.host.probe.get("dependencies") or {}).get(name)]
        checks["dependencies"] = "fail" if missing else "pass"
        if missing:
            failures.append("DEPENDENCY_MISSING")
            extra["missing"] = missing
            extra["package_manager"] = (self.host.probe.get("package_manager") or "")

        free = self.host.probe.get("disk_free_bytes")
        disk_ok = isinstance(free, int) and free >= REQUIRED_DISK_BYTES
        checks["disk"] = "pass" if disk_ok else "fail"
        if not disk_ok:
            failures.append("DISK_INSUFFICIENT")

        occupant = (self.host.probe.get("ports") or {}).get(payload.get("port"))
        port_ok = occupant is None
        checks["ports"] = "pass" if port_ok else "fail"
        if not port_ok:
            failures.append("PORT_CONFLICT")

        path_code = self._path_conflict(payload)
        checks["paths"] = "pass" if path_code is None else "fail"
        if path_code:
            failures.append(path_code)

        identity = self.host.probe.get("identity") or {}
        identity_ok = identity.get("principal") == grant.principal and identity.get("app_id") == grant.object_id
        checks["identity"] = "pass" if identity_ok else "fail"
        if not identity_ok:
            failures.append("IDENTITY_MISMATCH")

        permission_ok, permission_code = self._permissions(payload)
        checks["permissions"] = "pass" if permission_ok else "fail"
        if not permission_ok:
            failures.append(permission_code)

        credential_ok = self._credentials_present()
        checks["background_credentials"] = "pass" if credential_ok else "fail"
        if not credential_ok:
            failures.append("CREDENTIAL_BOUNDARY")

        domain_ok = isinstance(payload.get("domain"), str) and DOMAIN_PATTERN.fullmatch(payload["domain"]) is not None
        checks["domain"] = "pass" if domain_ok else "fail"
        if not domain_ok:
            failures.append("DOMAIN_INVALID")

        plan = {"checks": checks, "commands": [], **extra}
        if failures:
            code = failures[0]
            http = 422 if code == "PLATFORM_UNSUPPORTED" else 409
            if code in {"PERMISSION_DENIED", "CREDENTIAL_BOUNDARY"}:
                http = 403
            return _outcome(code, http, False, "preflight blocked", plan)
        return _outcome("OK", 200, True, "preflight passed with no host mutation", plan)

    def _op_path_resolve(self, grant, payload, data):
        conflict = self._path_conflict(payload)
        if conflict:
            return _outcome(conflict, 409, False, "install paths conflict; nothing was moved or deleted", {"commands": []})
        path = logical_install_path(self.home, payload["repo"])
        return _outcome(
            "OK",
            200,
            True,
            "logical install path",
            {
                "path": str(path),
                "created": False,
                "commands": [],
                "registry": {
                    "directory": {"state": "planned", "path": str(path)},
                    "cluster": {"state": "planned", "path": str(path / ".postgres")},
                    "service_unit": {"state": "planned"},
                    "socket": {"state": "planned"},
                    "user_data": list(USER_DATA),
                },
            },
        )

    def _op_port_reserve(self, grant, payload, data):
        port = payload.get("port")
        occupant = (self.host.probe.get("ports") or {}).get(port)
        if occupant:
            return _outcome(
                "PORT_CONFLICT",
                409,
                False,
                "port is held by another owner",
                {"port": port, "occupant": dict(occupant), "killed": False, "moved": False, "commands": []},
            )
        data["reservations"][str(port)] = {"owner": grant.object_id, "held": True, "port": port}
        return _outcome("OK", 200, True, "port reserved in the journal", {"port": port, "killed": False, "moved": False})

    def _op_journal_note_counter_loss(self, grant, payload, data):
        old = str(data["epoch"])
        data["epochs"].setdefault(old, {"gap": False, "closed": False, "applied_seqs": []})
        data["epochs"][old]["gap"] = True
        data["epochs"][old]["closed"] = False
        data["epoch"] += 1
        data["seq"] = 0
        data["epochs"][str(data["epoch"])] = {"gap": False, "closed": False, "applied_seqs": []}
        return _outcome("OK", 200, True, "counter loss opened a new epoch", {"commands": []}, count_seq=False)

    def _op_cloudflare_plan(self, grant, payload, data):
        blocked = self._cloudflare_boundary(payload)
        if blocked is not None:
            return blocked
        return _outcome("OK", 200, True, "cloudflare plan", {"deletes": [], "commands": []})

    def _op_cloudflare_apply_synthetic(self, grant, payload, data):
        blocked = self._cloudflare_boundary(payload)
        if blocked is not None:
            return blocked
        remote = self.host.probe["cloudflare"]
        domain = payload["domain"]
        service = f"http://127.0.0.1:{payload['port']}"
        ingress = list(remote.get("ingress") or [])
        if not any(item.get("hostname") == domain for item in ingress):
            ingress = [{"hostname": domain, "path": None, "service": service}, *ingress]
            remote["ingress"] = ingress
        if "dns_fails" in self.host.faults:
            data["deployment_state"] = "PARTIAL"
            return _outcome(
                "PARTIAL",
                409,
                False,
                "ingress was simulated and DNS was not; nothing was deleted",
                {"deletes": [], "commands": []},
                simulated=True,
            )
        alias = remote.get("tunnel_alias")
        remote["dns"] = [{"type": "CNAME", "name": domain, "content": alias, "proxied": True}]
        data["deployment_state"] = "MAPPED"
        return _outcome("OK", 200, True, "synthetic mapping applied", {"deletes": [], "commands": []}, simulated=True)

    def _op_recovery_plan(self, grant, payload, data):
        remote = self.host.probe.get("cloudflare") or {}
        if _foreign_dns(remote, payload.get("domain")):
            return _outcome("DNS_FOREIGN", 409, False, "foreign DNS is left in place", {"steps": [], "deletes": [], "commands": []})
        if data.get("deployment_state") == "PARTIAL":
            return _outcome("OK", 200, True, "recovery continues only the missing DNS step", {"steps": ["dns"], "deletes": [], "commands": []})
        return _outcome("OK", 200, True, "nothing to recover", {"steps": [], "deletes": [], "commands": []})

    def _op_recovery_apply_synthetic(self, grant, payload, data):
        if "recovery_fails" in self.host.faults:
            data["deployment_state"] = "SAFE_FROZEN"
            return _outcome("RECOVERY_FAILED", 409, False, "recovery failed closed", {"deletes": [], "commands": []})
        return _outcome("OK", 200, True, "recovery plan recorded", {"deletes": [], "commands": []}, simulated=True)

    def _op_resource_observe(self, grant, payload, data):
        for item in payload.get("resources") or []:
            data["resources"][item["resource_id"]] = {
                "kind": item.get("kind"),
                "owner": item.get("owner"),
                "relative": item.get("relative"),
            }
        return _outcome("OK", 200, True, "resource observation recorded", {"commands": []})

    def _op_cleanup_synthetic(self, grant, payload, data):
        records = []
        for resource_id in payload.get("resource_ids") or []:
            record = data["resources"].get(resource_id)
            if record is None or record.get("owner") is None:
                return _outcome("OWNERSHIP_UNKNOWN", 409, False, "unknown ownership blocks cleanup", {"commands": []})
            records.append(record)
        for record in records:
            if record.get("owner") != grant.object_id:
                return _outcome("CROSS_APP_BOUNDARY", 403, False, "cross-app resource blocks cleanup", {"commands": []})
        paths = []
        for record in records:
            relative = record.get("relative")
            if not isinstance(relative, str) or RELATIVE_NAME.fullmatch(relative) is None:
                return _outcome("PATH_OUTSIDE_AUTHORIZATION", 403, False, "resource path is outside inventory", {"commands": []})
            path = self.inventory / relative
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                info = None
            if info is not None and stat.S_ISLNK(info.st_mode):
                return _outcome("UNSAFE_PATH", 409, False, "symlink cleanup is refused", {"commands": []})
            if info is not None and not stat.S_ISREG(info.st_mode):
                return _outcome("UNSAFE_PATH", 409, False, "cleanup only unlinks a regular file", {"commands": []})
            paths.append(path)
        if "cleanup_fails" in self.host.faults:
            return _outcome("RECOVERY_FAILED", 409, False, "cleanup fault left owned files in place", {"commands": []})
        for path in paths:
            if path.exists():
                os.unlink(path)
        return _outcome("OK", 200, True, "owned synthetic file removed", {"commands": []}, simulated=True)

    def _op_uninstall_plan(self, grant, payload, data):
        previous = payload.get("previous_healthy_sha") or ""
        if payload.get("install_kind") == "first_install" and previous not in ("", None):
            return _outcome(
                "EVIDENCE_MISSING",
                409,
                False,
                "first install cannot claim a previous healthy sha",
                {"previous_healthy_sha": "", "commands": []},
            )
        resource_ids = payload.get("resource_ids") or []
        if payload.get("removal_proof"):
            for resource_id in resource_ids:
                record = data["resources"].get(resource_id)
                if record is None:
                    return _outcome("EVIDENCE_MISSING", 409, False, "removal proof does not match inventory", {"previous_healthy_sha": "", "commands": []})
                path = self.inventory / record["relative"]
                if path.exists() or path.is_symlink():
                    data["deployment_state"] = "SAFE_FROZEN"
                    return _outcome("SAFE_FROZEN", 409, False, "removal is not proven while the resource remains", {"previous_healthy_sha": "", "commands": []})
            data["deployment_state"] = "ROLLED_BACK"
            return _outcome("ROLLED_BACK", 200, True, "owned resources are already absent", {"previous_healthy_sha": "", "install_kind": "first_install", "commands": []})
        data["deployment_state"] = "SAFE_FROZEN"
        return _outcome(
            "SAFE_FROZEN",
            409,
            False,
            "destructive uninstall has no removal proof",
            {"previous_healthy_sha": "", "destructive_authorization": "separate", "commands": []},
        )

    def _op_rebuild_plan(self, grant, payload, data):
        if payload.get("install_kind") == "upgrade" and SHA_PATTERN.fullmatch(payload.get("previous_healthy_sha") or "") is None:
            return _outcome("EVIDENCE_MISSING", 409, False, "upgrade requires the previous healthy sha", {"preserves_user_data": True, "commands": []})
        return _outcome("OK", 200, True, "rebuild plan preserves user data", {"preserves_user_data": True, "operation": "rebuild", "commands": []})

    def _op_rebuild_apply_synthetic(self, grant, payload, data):
        if payload.get("install_kind") == "upgrade" and SHA_PATTERN.fullmatch(payload.get("previous_healthy_sha") or "") is None:
            return _outcome("EVIDENCE_MISSING", 409, False, "upgrade requires the previous healthy sha", {"preserves_user_data": True})
        if payload.get("overwrite_user_data"):
            return _outcome("USER_DATA_PROTECTED", 409, False, "overwrite of user data is disabled", {"preserves_user_data": True, "commands": []})
        code = logical_install_path(self.home, payload["repo"]) / "app" / "code.py"
        code.parent.mkdir(parents=True, exist_ok=True)
        code.write_text(payload.get("replacement") or "", encoding="utf-8")
        return _outcome("OK", 200, True, "synthetic rebuild preserved user data", {"preserves_user_data": True, "commands": []}, simulated=True)

    def _op_seed_observe(self, grant, payload, data):
        if payload.get("static_only") is True:
            return _outcome("EVIDENCE_MISSING", 409, False, "a static check is not sufficient", {"static_only_sufficient": False, "commands": []})
        plan = {
            "static_only_sufficient": False,
            "auto_advance": False,
            "beyond_draft": False,
            "scheduler": "present" if payload.get("scheduler_present") else "absent",
            "single_transaction": False,
            "commands": [],
        }
        if payload.get("post_observed"):
            if not payload.get("receipt_present") or payload.get("post_id") != payload.get("receipt_id"):
                return _outcome("SIDE_EFFECT_UNKNOWN", 409, False, "feedback post and local receipt are separate observations", plan)
        return _outcome("OK", 200, True, "seed observation recorded", plan)

    def _op_service_plan(self, grant, payload, data):
        adapter_id, _supported = classify(self.host.probe)
        adapter = dict(ADAPTERS.get(adapter_id) or {})
        scope_level = payload.get("scope_level")
        if scope_level == "system":
            return _outcome(
                "PRIVILEGE_ESCALATION_DENIED",
                403,
                False,
                "system scope is refused without a separate privilege grant",
                {"commands": [], "execution": "denied", "adapter": adapter},
            )
        generation = payload.get("generation")
        if generation != data.get("service_generation"):
            return _outcome("LEASE_FENCE_REJECTED", 409, False, "service generation is stale; the host was not restarted", {"commands": [], "execution": "denied", "adapter": adapter})
        action = payload.get("action")
        plan = {
            "commands": [],
            "execution": "denied",
            "adapter": adapter,
            "unit": {"autostart": True, "scope_level": scope_level},
            "action": action,
        }
        if action == "restart":
            data["service_generation"] = data.get("service_generation", 0) + 1
            return _outcome("OK", 200, True, "restart planned in the journal only", plan)
        if action == "health":
            if "health_fails" in self.host.faults or not (self.host.probe.get("health") or {}).get("ok"):
                return _outcome("HEALTH_FAILED", 409, False, "health probe failed", plan)
            receipt = data.get("receipt")
            if not receipt:
                data["deployment_state"] = "RECONCILE"
                return _outcome("RECONCILE", 409, False, "health has no durable receipt", plan)
            data["deployment_state"] = "COMMIT_PENDING"
            return _outcome("COMMIT_PENDING", 200, True, "health is pending a commit that this package does not perform", plan)
        return _outcome("OK", 200, True, "service plan only; execution denied", plan)

    def _op_service_record_receipt(self, grant, payload, data):
        context = payload.get("context")
        if context == "macmini/isolated-tests-v1":
            return _outcome("FORGED_MACCI_CONTEXT", 409, False, "MacCI context cannot be recorded by this package", {"commands": []})
        if context != "linux-host/isolated-tests-v1":
            return _outcome("EVIDENCE_MISSING", 409, False, "receipt context is not the linux isolated context", {"commands": []})
        data["receipt"] = {"context": context, "running_sha": payload.get("running_sha")}
        return _outcome("OK", 200, True, "synthetic receipt stored", {"context": context, "commands": []})

    def _op_service_commit(self, grant, payload, data):
        if payload.get("install_kind") == "first_install" and payload.get("action") == "no_change":
            return _outcome("FIRST_INSTALL_NO_CHANGE", 409, False, "first install cannot commit through no_change", {"commands": []})
        return _outcome("EXECUTION_DENIED", 403, False, "commit execution is disabled", {"commands": []})

    def _op_fd_observe(self, grant, payload, data):
        adapter_id = payload.get("adapter_id")
        if adapter_id == "darwin-launchd" and payload.get("use_w06_proc_path"):
            return _outcome(
                "W06_PROC_FD_INCOMPATIBLE",
                409,
                False,
                "W06 /proc/self/fd containment is not a Darwin proof",
                {"contained": False, "prefix": "/proc/self/fd", "commands": []},
            )
        if adapter_id == "darwin-launchd":
            directory = payload.get("synthetic_directory") or ""
            located = payload.get("synthetic_located") or ""
            if containment_holds(directory, located):
                return _outcome(
                    "OK",
                    200,
                    True,
                    "synthetic Darwin fd containment",
                    {"contained": True, "prefix": "/dev/fd", "observation": "darwin-dev-fd-synthetic", "commands": []},
                )
            return _outcome("PATH_ESCAPED", 409, False, "synthetic path escaped the directory", {"contained": False, "prefix": "/dev/fd", "commands": []})
        if adapter_id != "linux-systemd":
            return _outcome("PLATFORM_UNSUPPORTED", 422, False, "fd observation adapter is unsupported", {"contained": False, "commands": []})
        directory = Path(payload.get("directory") or "")
        relative = payload.get("relative")
        if not _inside(self.workspace, directory) or not isinstance(relative, str) or RELATIVE_NAME.fullmatch(relative) is None:
            return _outcome("PATH_OUTSIDE_AUTHORIZATION", 403, False, "fd directory is outside the workspace", {"contained": False, "commands": []})
        contained, prefix = _linux_fd_contains(directory, relative)
        if not contained:
            return _outcome("PATH_ESCAPED", 409, False, "file descriptor escaped the directory", {"contained": False, "prefix": prefix, "commands": []})
        return _outcome(
            "OK",
            200,
            True,
            "local linux fd containment; not a Darwin or MacCI proof",
            {"contained": True, "prefix": "/proc/self/fd", "observation": "linux-proc-self-fd", "commands": []},
        )

    def _cloudflare_boundary(self, payload):
        credentials = self.host.probe.get("credentials") or {}
        cloudflare = credentials.get("cloudflare") or {}
        github = credentials.get("github_feedback") or {}
        supplied = payload.get("credential_fingerprint")
        expected = cloudflare.get("fingerprint")
        if supplied != expected or supplied == github.get("fingerprint") or FINGERPRINT_PATTERN.fullmatch(supplied or "") is None:
            return _outcome("CREDENTIAL_BOUNDARY", 403, False, "credential boundary rejected the request", {"deletes": [], "commands": []})
        remote = self.host.probe.get("cloudflare") or {}
        if _foreign_dns(remote, payload.get("domain")):
            return _outcome("DNS_FOREIGN", 409, False, "DNS points elsewhere and was not deleted", {"deletes": [], "commands": []})
        if _foreign_ingress(remote, payload.get("domain"), payload.get("port")):
            return _outcome("INGRESS_FOREIGN", 409, False, "ingress points elsewhere and was not deleted", {"deletes": [], "commands": []})
        return None

    def _path_conflict(self, payload):
        preferred = logical_install_path(self.home, payload["repo"])
        legacy = legacy_install_path(self.home, payload["repo"])
        if preferred.exists() and legacy.exists():
            return "PATH_CONFLICT"
        return None

    def _permissions(self, payload):
        probe = self.host.probe.get("permissions") or {}
        if probe.get("owner_matches") is False or probe.get("uid") == 0:
            return False, "PERMISSION_DENIED"
        path = logical_install_path(self.home, payload["repo"])
        if path.exists():
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_mode & 0o077:
                return False, "PERMISSION_DENIED"
        return True, ""

    def _credentials_present(self):
        credentials = self.host.probe.get("credentials") or {}
        cloudflare = credentials.get("cloudflare") or {}
        github = credentials.get("github_feedback") or {}
        left = cloudflare.get("fingerprint")
        right = github.get("fingerprint")
        if not cloudflare.get("present") or not github.get("present"):
            return False
        if not isinstance(left, str) or not isinstance(right, str):
            return False
        if FINGERPRINT_PATTERN.fullmatch(left) is None or FINGERPRINT_PATTERN.fullmatch(right) is None:
            return False
        return left != right

    def _cluster_complete(self, payload):
        cluster = self.host.probe.get("cluster") or {}
        marker = logical_install_path(self.home, payload["repo"]) / ".postgres" / "PG_VERSION"
        if not cluster.get("present") or cluster.get("owner") != payload.get("app_id"):
            return False
        if not marker.is_file() or marker.is_symlink():
            return False
        return True


def _outcome(code, http, ok, diagnosis, plan=None, simulated=False, count_seq=True, record_run=True):
    return {
        "code": code,
        "http": http,
        "ok": ok,
        "diagnosis": diagnosis,
        "plan": {} if plan is None else plan,
        "simulated": simulated,
        "count_seq": count_seq,
        "record_run": record_run,
    }


def _reject_secrets(payload):
    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in SECRET_KEYS:
                    raise LifecycleError("CREDENTIAL_BOUNDARY", 403, "credential material is not accepted in the request")
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)


def _payload_hash(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _foreign_dns(remote, domain):
    records = remote.get("dns") or []
    if not records:
        return False
    alias = remote.get("tunnel_alias")
    if len(records) != 1:
        return True
    record = records[0]
    content = str(record.get("content") or "").rstrip(".")
    return not (
        record.get("type") == "CNAME"
        and record.get("name") == domain
        and content == alias
        and record.get("proxied") is True
    )


def _foreign_ingress(remote, domain, port):
    ingress = remote.get("ingress") or []
    if not ingress or ingress[-1].get("hostname") or ingress[-1].get("path"):
        return True
    assigned = [item for item in ingress if item.get("hostname") == domain]
    if not assigned:
        return False
    service = f"http://127.0.0.1:{port}"
    return not (len(assigned) == 1 and assigned[0].get("service") == service and not assigned[0].get("path"))


def _inside(root, path):
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:
        return False
    return resolved == base or str(resolved).startswith(str(base) + os.sep)


def _linux_fd_contains(directory, relative):
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        file_fd = os.open(relative, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
        try:
            directory_real = os.path.realpath(f"/proc/self/fd/{dir_fd}")
            located_real = os.path.realpath(f"/proc/self/fd/{file_fd}")
        finally:
            os.close(file_fd)
    finally:
        os.close(dir_fd)
    return containment_holds(directory_real, located_real), "/proc/self/fd"
