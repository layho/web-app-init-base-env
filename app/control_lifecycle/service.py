"""Isolated lifecycle plans. Execution of host installers stays denied."""

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from app.control_lifecycle.errors import LifecycleError
from app.control_lifecycle.fdpath import containment_holds, fd_namespace, string_map_is_safe_path_resolution
from app.control_lifecycle.grant import DenyAllAuthorizationPort, W02AuthorizationPort
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
    "port.release": "lifecycle.reserve_port",
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
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")


class Result:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def __str__(self):
        return f"{self.code}: {self.diagnosis}"


class LifecycleService:
    def __init__(self, workspace, host, clock=None, authorizer=None):
        self.workspace = Path(workspace)
        if self.workspace.is_symlink() or not self.workspace.is_dir():
            raise LifecycleError("UNSAFE_PATH", 403, "workspace is not a real directory")
        self.home = self.workspace / "home"
        self.inventory = self.workspace / "inventory"
        self.host = host
        self.clock = clock
        if not isinstance(authorizer, (W02AuthorizationPort, DenyAllAuthorizationPort)):
            authorizer = DenyAllAuthorizationPort()
        self.auth = authorizer
        self.journal = Journal(self.workspace / "lifecycle-journal.json", host.faults)

    def issue_grant(self, principal, scopes, object_id):
        grant = self.auth.issue(principal, scopes, object_id)
        if grant is None:
            raise LifecycleError("NOT_AUTHORIZED", 403, "authorization port denied the request")
        return grant

    def inspect(self, grant):
        if not self.auth.authenticate(grant):
            raise LifecycleError("NOT_AUTHORIZED", 403, "inspect requires a server grant")
        policy = getattr(self.auth, "policy", None)
        if policy is None or grant.object_id != policy.app_id or grant.repo != policy.repo or grant.host_id != policy.host_id:
            raise LifecycleError("POLICY_DENIED", 403, "inspect is outside the server grant")
        return self.journal.inspect()

    def call(self, grant, operation, payload, *, idempotency_key, expected_revision):
        try:
            self._precheck(grant, operation, payload)
        except LifecycleError as exc:
            return self._blocked(exc, operation, payload)
        scope = REQUIRED_SCOPE[operation]
        digest = _binding_hash(grant, operation, idempotency_key, payload)
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
        if grant.authority != "w02-server-policy":
            raise LifecycleError("NOT_AUTHORIZED", 403, "test double is not an authorization authority")
        if REQUIRED_SCOPE[operation] not in grant.scopes:
            raise LifecycleError("POLICY_DENIED", 403, "scope does not allow this operation")
        if payload.get("app_id") != grant.object_id:
            raise LifecycleError("POLICY_DENIED", 403, "object is outside the server grant")
        if payload.get("repo") != grant.repo:
            raise LifecycleError("POLICY_DENIED", 403, "repo is outside the server grant")
        policy = getattr(self.auth, "policy", None)
        if policy is None or grant.host_id != policy.host_id:
            raise LifecycleError("POLICY_DENIED", 403, "host is outside the server grant")
        run_id = str(payload.get("run_id") or "")
        if run_id and RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise LifecycleError("POLICY_DENIED", 403, "run is outside the server grant")
        if grant.run_scope != f"runs:{grant.object_id}" or grant.resource_scope != f"inventory:{grant.object_id}":
            raise LifecycleError("POLICY_DENIED", 403, "resource or run scope is outside the server grant")
        for resource_id in _resource_ids(payload):
            if not isinstance(resource_id, str) or RELATIVE_NAME.fullmatch(resource_id) is None:
                raise LifecycleError("POLICY_DENIED", 403, "resource is outside the server grant")
        if "home" in payload and not self._home_allowed(payload["home"]):
            raise LifecycleError("PATH_OUTSIDE_AUTHORIZATION", 403, "home is outside the authorized workspace", {"commands": []})

    def _home_allowed(self, value):
        try:
            requested = Path(value).resolve()
        except OSError:
            return False
        return requested == self.home.resolve()

    def _blocked(self, exc, operation, payload):
        state = self.journal._public(self.journal.data)
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
        if not _legal_port(port):
            return _outcome("PORT_ILLEGAL", 422, False, "port is not legal", {"port": port, "killed": False, "moved": False, "commands": []})
        generation = _reservation_generation(payload)
        if generation is None:
            return _outcome("LEASE_FENCE_REJECTED", 409, False, "reservation generation is not exact", {"port": port, "killed": False, "moved": False, "commands": []})
        existing = data["reservations"].get(str(port))
        if existing:
            if not _reservation_matches(existing, grant, generation):
                return _outcome(
                    "PORT_CONFLICT",
                    409,
                    False,
                    "port is held by another owner",
                    {"port": port, "occupant": dict(existing), "killed": False, "moved": False, "commands": []},
                )
            return _outcome("OK", 200, True, "port reservation already matches the owner and generation", {"port": port, "killed": False, "moved": False})
        occupant = (self.host.probe.get("ports") or {}).get(port)
        if occupant:
            return _outcome(
                "PORT_CONFLICT",
                409,
                False,
                "port is held by another owner",
                {"port": port, "occupant": dict(occupant), "killed": False, "moved": False, "commands": []},
            )
        data["reservations"][str(port)] = {
            "owner": grant.object_id,
            "held": True,
            "port": port,
            "generation": generation,
            "repo": grant.repo,
            "host_id": grant.host_id,
        }
        return _outcome("OK", 200, True, "port reserved in the journal", {"port": port, "killed": False, "moved": False})

    def _op_port_release(self, grant, payload, data):
        port = payload.get("port")
        if not _legal_port(port):
            return _outcome("PORT_ILLEGAL", 422, False, "port is not legal", {"port": port, "killed": False, "moved": False, "commands": []})
        generation = _reservation_generation(payload)
        existing = data["reservations"].get(str(port))
        if generation is None or not existing or not _reservation_matches(existing, grant, generation):
            occupant = dict(existing) if isinstance(existing, dict) else {}
            return _outcome(
                "PORT_CONFLICT",
                409,
                False,
                "release requires the exact owner and generation",
                {"port": port, "occupant": occupant, "killed": False, "moved": False, "commands": []},
            )
        del data["reservations"][str(port)]
        return _outcome("OK", 200, True, "port reservation released in the journal", {"port": port, "killed": False, "moved": False})

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
        intent = _intent(
            f"cloudflare:{payload.get('domain')}:{payload.get('port')}",
            "cloudflare.apply_synthetic",
            str(payload.get("domain") or ""),
            grant,
            data.get("service_generation", 0),
        )

        def effect():
            remote = self.host.probe["cloudflare"]
            domain = payload["domain"]
            service = f"http://127.0.0.1:{payload['port']}"
            ingress = list(remote.get("ingress") or [])
            if not any(item.get("hostname") == domain for item in ingress):
                ingress = [{"hostname": domain, "path": None, "service": service}, *ingress]
                remote["ingress"] = ingress
            if "dns_fails" in self.host.faults:

                def mutate_partial(stored):
                    stored["deployment_state"] = "PARTIAL"
                    _mark_intent(stored, intent["intent_id"], "partial")

                return {
                    **_outcome(
                        "PARTIAL",
                        409,
                        False,
                        "ingress was simulated and DNS was not; nothing was deleted",
                        {"deletes": [], "commands": []},
                        simulated=True,
                    ),
                    "mutate": mutate_partial,
                }
            alias = remote.get("tunnel_alias")
            remote["dns"] = [{"type": "CNAME", "name": domain, "content": alias, "proxied": True}]

            def mutate_mapped(stored):
                stored["deployment_state"] = "MAPPED"
                _mark_intent(stored, intent["intent_id"], "reconciled")

            return {
                **_outcome("OK", 200, True, "synthetic mapping applied", {"deletes": [], "commands": []}, simulated=True),
                "mutate": mutate_mapped,
            }

        return {**_outcome("OK", 200, True, "synthetic mapping applied", {"deletes": [], "commands": []}, simulated=True), "effect": effect, "intent": intent}

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
            generation = item.get("generation", 0)
            if type(generation) is not int or generation < 0:
                return _outcome("SCHEMA_UNSUPPORTED", 422, False, "resource generation is not exact", {"commands": []})
            data["resources"][item["resource_id"]] = {
                "kind": item.get("kind"),
                "owner": item.get("owner"),
                "relative": item.get("relative"),
                "generation": generation,
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
        identities = []
        for record in records:
            relative = record.get("relative")
            if not isinstance(relative, str) or RELATIVE_NAME.fullmatch(relative) is None:
                return _outcome("PATH_OUTSIDE_AUTHORIZATION", 403, False, "resource path is outside inventory", {"commands": []})
            identity = self._inventory_identity(relative)
            if identity is None:
                return _outcome("UNSAFE_PATH", 409, False, "symlink cleanup is refused", {"commands": []})
            if identity == "missing":
                identities.append({"relative": relative, "record": record, "missing": True})
                continue
            identities.append({"relative": relative, "record": record, "missing": False, **identity})
        if "cleanup_fails" in self.host.faults:
            return _outcome("RECOVERY_FAILED", 409, False, "cleanup fault left owned files in place", {"commands": []})
        present = [item for item in identities if not item["missing"]]
        intent = _intent(
            "cleanup:" + ",".join(item["relative"] for item in identities),
            "cleanup.synthetic",
            ",".join(record.get("relative") or "" for record in records),
            grant,
            records[0].get("generation", 0) if records else 0,
        )
        intent["identities"] = [
            {
                "relative": item["relative"],
                "inode": item.get("inode"),
                "device": item.get("device"),
                "generation": item["record"].get("generation", 0),
                "owner": item["record"].get("owner"),
            }
            for item in identities
        ]

        def effect():
            if "replace_with_symlink_before_unlink" in self.host.faults:
                self._swap_inventory_symlink(present[0]["relative"] if present else identities[0]["relative"], "keeper.txt")
            removed = []
            for item in present:
                status = self._unlink_inventory(item["relative"], item["inode"], item["device"])
                if status == "unsafe":
                    return _outcome("UNSAFE_PATH", 409, False, "symlink cleanup is refused", {"commands": []})
                if status != "removed":
                    return _outcome("SIDE_EFFECT_UNKNOWN", 409, False, "cleanup result is not confirmed", {"commands": []})
                removed.append(item["relative"])

            def mutate(stored):
                _mark_intent(stored, intent["intent_id"], "reconciled")

            return {
                **_outcome("OK", 200, True, "owned synthetic file removed", {"commands": [], "removed": removed}, simulated=True),
                "mutate": mutate,
            }

        return {
            **_outcome("OK", 200, True, "owned synthetic file removed", {"commands": []}, simulated=True),
            "effect": effect,
            "intent": intent,
        }

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
        identity = _regular_file_identity(code)
        if not identity:
            return _outcome("UNSAFE_PATH", 409, False, "rebuild target is not a regular file", {"preserves_user_data": True, "commands": []})
        intent = _intent(
            f"rebuild:{code.name}:{identity['inode']}",
            "rebuild.apply_synthetic",
            code.name,
            grant,
            data.get("service_generation", 0),
        )
        intent["inode"] = identity["inode"]
        intent["device"] = identity["device"]
        replacement = payload.get("replacement") or ""

        def effect():
            if not _write_regular_file(code.parent, code.name, replacement, identity["inode"], identity["device"]):
                return _outcome("SIDE_EFFECT_UNKNOWN", 409, False, "rebuild result is not confirmed", {"preserves_user_data": True, "commands": []})

            def mutate(stored):
                _mark_intent(stored, intent["intent_id"], "reconciled")

            return {
                **_outcome("OK", 200, True, "synthetic rebuild preserved user data", {"preserves_user_data": True, "commands": []}, simulated=True),
                "mutate": mutate,
            }

        return {
            **_outcome("OK", 200, True, "synthetic rebuild preserved user data", {"preserves_user_data": True, "commands": []}, simulated=True),
            "effect": effect,
            "intent": intent,
        }

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
            if not receipt or receipt.get("proves_service") is not False:
                data["deployment_state"] = "RECONCILE"
                return _outcome("RECONCILE", 409, False, "health has no durable receipt", plan)
            plan["proves_service"] = False
            data["deployment_state"] = "COMMIT_PENDING"
            return _outcome("COMMIT_PENDING", 200, True, "health is pending a commit that this package does not perform", plan)
        return _outcome("OK", 200, True, "service plan only; execution denied", plan)

    def _op_service_record_receipt(self, grant, payload, data):
        context = payload.get("context")
        if payload.get("proves_service") or payload.get("host_capability_proof") or payload.get("service_proven"):
            return _outcome(
                "EVIDENCE_MISSING",
                409,
                False,
                "caller context and running_sha do not prove a real service",
                {"proves_service": False, "commands": []},
            )
        if context == "macmini/isolated-tests-v1":
            return _outcome(
                "FORGED_MACCI_CONTEXT",
                409,
                False,
                "MacCI context cannot be recorded by this package",
                {"proves_service": False, "commands": []},
            )
        if context != "linux-host/isolated-tests-v1":
            return _outcome(
                "EVIDENCE_MISSING",
                409,
                False,
                "receipt context is not the linux isolated context",
                {"proves_service": False, "commands": []},
            )
        probe_sha = (self.host.probe.get("health") or {}).get("running_sha")
        if not isinstance(probe_sha, str) or payload.get("running_sha") != probe_sha or SHA_PATTERN.fullmatch(probe_sha) is None:
            return _outcome(
                "EVIDENCE_MISSING",
                409,
                False,
                "running_sha from the caller is not service proof",
                {"proves_service": False, "commands": []},
            )
        data["receipt"] = {
            "context": context,
            "running_sha": payload.get("running_sha"),
            "proves_service": False,
            "authority": "not-a-service-proof",
        }
        return _outcome(
            "OK",
            200,
            True,
            "synthetic receipt stored; it does not prove a real service",
            {"context": context, "proves_service": False, "commands": []},
        )

    def _op_service_commit(self, grant, payload, data):
        if payload.get("install_kind") == "first_install" and payload.get("action") == "no_change":
            return _outcome("FIRST_INSTALL_NO_CHANGE", 409, False, "first install cannot commit through no_change", {"commands": []})
        return _outcome("EXECUTION_DENIED", 403, False, "commit execution is disabled", {"commands": []})

    def _op_fd_observe(self, grant, payload, data):
        adapter_id = payload.get("adapter_id")
        if payload.get("resolve_dev_fd") or "dev_fd_map" in payload:
            if not string_map_is_safe_path_resolution(adapter_id, payload.get("dev_fd_map")):
                prefix = "/dev/fd" if adapter_id == "darwin-launchd" else ""
                return _outcome(
                    "DEV_FD_STRING_MAP_UNSAFE",
                    409,
                    False,
                    "a Darwin /dev/fd string map is not safe path resolution",
                    {"contained": False, "safe_path_resolution": False, "prefix": prefix, "commands": []},
                )
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
                    {
                        "contained": True,
                        "prefix": "/dev/fd",
                        "observation": "darwin-dev-fd-synthetic",
                        "safe_path_resolution": False,
                        "commands": [],
                    },
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
            {
                "contained": True,
                "prefix": "/proc/self/fd",
                "observation": "linux-proc-self-fd",
                "safe_path_resolution": True,
                "two_platform_proof": False,
                "commands": [],
            },
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

    def _open_inventory(self):
        try:
            directory = os.open(self.inventory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            return None
        info = os.fstat(directory)
        if not stat.S_ISDIR(info.st_mode):
            os.close(directory)
            return None
        return directory

    def _inventory_identity(self, relative):
        directory = self._open_inventory()
        if directory is None:
            return None
        try:
            try:
                info = os.stat(relative, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return "missing"
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return None
            fd = os.open(relative, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            try:
                current = os.fstat(fd)
            finally:
                os.close(fd)
            if (
                current.st_ino != info.st_ino
                or current.st_dev != info.st_dev
                or current.st_nlink != 1
                or not stat.S_ISREG(current.st_mode)
            ):
                return None
            return {"inode": current.st_ino, "device": current.st_dev}
        except OSError:
            return None
        finally:
            os.close(directory)

    def _unlink_inventory(self, relative, inode, device):
        directory = self._open_inventory()
        if directory is None:
            return "unknown"
        fd = None
        try:
            fd = os.open(relative, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            current = os.fstat(fd)
            if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                return "unsafe"
            if current.st_ino != inode or current.st_dev != device:
                return "unsafe"
            base = os.path.realpath(f"/proc/self/fd/{directory}")
            located = os.path.realpath(f"/proc/self/fd/{fd}")
            if not containment_holds(base, located):
                return "unsafe"
            os.close(fd)
            fd = None
            os.unlink(relative, dir_fd=directory)
            return "removed"
        except OSError:
            return "unsafe"
        finally:
            if fd is not None:
                os.close(fd)
            os.close(directory)

    def _swap_inventory_symlink(self, relative, keeper):
        directory = self._open_inventory()
        if directory is None:
            return
        try:
            try:
                os.unlink(relative, dir_fd=directory)
            except OSError:
                return
            os.symlink(keeper, relative, dir_fd=directory)
        finally:
            os.close(directory)

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


def _binding_hash(grant, operation, idempotency_key, payload):
    body = {
        "app_id": grant.object_id,
        "host_id": grant.host_id,
        "idempotency_key": idempotency_key,
        "operation": operation,
        "payload_hash": _payload_hash(payload),
        "principal": grant.principal,
        "repo": grant.repo,
        "resource_ids": sorted(str(item) for item in _resource_ids(payload)),
        "resource_scope": grant.resource_scope,
        "run_id": str(payload.get("run_id") or ""),
        "run_scope": grant.run_scope,
        "scope": REQUIRED_SCOPE.get(operation, ""),
        "scopes": sorted(grant.scopes),
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _resource_ids(payload):
    found = []
    if isinstance(payload.get("resource_ids"), list):
        found.extend(payload["resource_ids"])
    if isinstance(payload.get("resources"), list):
        for item in payload["resources"]:
            if isinstance(item, dict) and "resource_id" in item:
                found.append(item["resource_id"])
    return found


def _legal_port(port):
    return type(port) is int and 1 <= port <= 65535


def _reservation_generation(payload):
    value = payload.get("reservation_generation", 0)
    if type(value) is not int or value < 0:
        return None
    return value


def _reservation_matches(existing, grant, generation):
    return (
        existing.get("owner") == grant.object_id
        and existing.get("generation") == generation
        and existing.get("repo") == grant.repo
        and existing.get("host_id") == grant.host_id
        and existing.get("held") is True
    )


def _intent(intent_id, operation, resource_id, grant, generation):
    return {
        "intent_id": intent_id,
        "operation": operation,
        "resource_id": resource_id,
        "owner": grant.object_id,
        "repo": grant.repo,
        "host_id": grant.host_id,
        "generation": generation,
        "state": "intent",
    }


def _mark_intent(data, intent_id, state):
    intent = data.setdefault("intents", {}).get(intent_id)
    if intent is not None:
        intent["state"] = state


def _regular_file_identity(path):
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        current = os.fstat(fd)
    finally:
        os.close(fd)
    if current.st_ino != info.st_ino or current.st_nlink != 1 or not stat.S_ISREG(current.st_mode):
        return None
    return {"inode": current.st_ino, "device": current.st_dev}


def _write_regular_file(directory, relative, content, inode, device):
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    fd = None
    try:
        fd = os.open(relative, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
            return False
        if current.st_ino != inode or current.st_dev != device:
            return False
        payload = content.encode("utf-8")
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            wrote = os.write(fd, view[offset:])
            if wrote <= 0:
                return False
            offset += wrote
        os.fsync(fd)
        os.fsync(dir_fd)
        return True
    except OSError:
        return False
    finally:
        if fd is not None:
            os.close(fd)
        os.close(dir_fd)


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
