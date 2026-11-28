"""Isolated unit tests for V0.6.0 W09 lifecycle contracts.

These tests use temporary directories and a labeled synthetic host double.
They do not install, uninstall, escalate, bind host ports, call Cloudflare,
or prove Darwin, systemd, MacCI, merge, deploy, or acceptance.
"""

import ast
import hashlib
import threading
import unittest
from pathlib import Path

from app.control_lifecycle import (
    CAPABILITIES,
    DIGEST,
    Grant,
    LifecycleService,
    REQUIRED_DISK_BYTES,
    SyntheticHost,
    fd_namespace,
    logical_install_path,
    service_unit_digest,
    w06_proc_self_fd_compatible,
)
from app.control_lifecycle.grant import ServerPolicy, W02AuthorizationPort
from app.control_lifecycle.flags import (
    ACCEPTANCE_ENABLED,
    GITHUB_ACTIONS,
    MACCI_PROOF,
    MOUNTED_IN_APP_MAIN,
    OVERWRITE_USER_DATA,
    PRIVILEGE_ESCALATION,
    PRODUCTION_DB_ACCESS,
    PRODUCTION_ENABLEMENT,
    PRODUCTION_VERIFICATION_ENABLED,
    REAL_DEPLOY,
    REAL_INSTALL,
    REAL_MERGE,
    REAL_UNINSTALL,
    ROUTE_MODULE,
)

ROOT = Path(__file__).resolve().parents[1]
REPO = "acme/app"
APP = "acme--app"
DOMAIN = "app.example.com"
PORT = 18080
CF = "sha256:" + ("ab" * 32)
GH = "sha256:" + ("cd" * 32)
SHA = "a" * 40
PLAN_SCOPES = frozenset(
    {
        "lifecycle.plan",
        "lifecycle.preflight",
        "lifecycle.reserve_port",
        "lifecycle.cloudflare",
        "lifecycle.service",
        "lifecycle.recover",
        "lifecycle.cleanup",
        "lifecycle.rebuild",
    }
)
RESERVED_SHA256 = {
    "deploy/app-init/bootstrap.sh": "328c620d64fa79ff1ea04d03ca59caacd59ce008923dd8b50956f9ff714f7d20",
    # W11 calls the isolated lifecycle adapter before any install prompt.
    "deploy/app-init/app_init.py": "2da15d6606a8eff127dbe6fbbed35ec7fc30443371197ac90508fb828e101dab",
    "deploy/app-init/upgrade_plan.py": "e0ec81f5fd54863feb49c499bad81e70784993f1bc668150d623b5747e373959",
    "deploy/app-init/upgrade_pr.py": "db9155ef2ca73b3a6bb0a0b7fd2dc93db95cc5c6e529149b18bb1b8b1b2a7cc9",
    "deploy/app-init/upgrade_scheduler.py": "a08e620d272815792d870eb9b2a10bc653bd39eece960f94e3b38329eedd5052",
    "deploy/app-init/upgrade_auto_merge.py": "2b42e2759ff8afe5905d9f9f295b06c5509aa727af98afe543ed58365c5be5c8",
    "deploy/app-init/upgrade_runtime_auto.py": "ec9bccdbfa473ff4f710236896754c58a972ce68466fd453a056d640cf39064d",
    "app/module_sdk.py": "7e14a207afc455725bf70b8b3e5c22790a583b65ed4ebb4c4997acff7d324b8e",
    "app/foundation_version.py": "01ef135f6562e126e6e8615642b2cebd3321732ceb6da07456c13b2f1366ec76",
    "app/auth.py": "4ebd6b43e360bdbf7c8c2ea4aecd8c4ec3050b388acd8bcd88ea9ffe82075d9e",
    "app/operations.py": "6a37a664fe2ac61d249104b76cf5b8a0d4d737cb866398fe381e1f3043b4ac34",
}
INIT_NAMES = [
    "README.md",
    "UPGRADE-PLAN.md",
    "UPGRADE-PR.md",
    "UPGRADE-SCHEDULER.md",
    "app-init",
    "app_init.py",
    "bootstrap.sh",
    "control_lifecycle_adapter.py",
    "install-upgrade-scheduler.py",
    "install-upgrade-scheduler.sh",
    "legacy_isolation.py",
    "ownership_preflight.py",
    "upgrade_auto_merge.py",
    "upgrade_plan.py",
    "upgrade_pr.py",
    "upgrade_runtime_auto.py",
    "upgrade_scheduler.py",
]
FALSE_FLAGS = (
    "PRODUCTION_ENABLEMENT",
    "MOUNTED_IN_APP_MAIN",
    "REAL_INSTALL",
    "REAL_UNINSTALL",
    "PRIVILEGE_ESCALATION",
    "REAL_MERGE",
    "REAL_DEPLOY",
    "GITHUB_ACTIONS",
    "MACCI_PROOF",
    "ACCEPTANCE_ENABLED",
    "OVERWRITE_USER_DATA",
    "PRODUCTION_DB_ACCESS",
    "PRODUCTION_VERIFICATION_ENABLED",
)


def linux_probe(**overrides):
    probe = {
        "os": "linux",
        "version": "24.04",
        "arch": "x86_64",
        "distro": "ubuntu",
        "package_manager": "apt",
        "service_manager": "systemd",
        "uid": 1000,
        "euid_is_root": False,
        "tty": False,
        "disk_free_bytes": REQUIRED_DISK_BYTES,
        "dependencies": {
            "git": True,
            "node": True,
            "npm": True,
            "initdb": True,
            "postgres": True,
            "pg_isready": True,
        },
        "ports": {PORT: None},
        "identity": {"principal": "installer", "app_id": APP},
        "permissions": {"uid": 1000, "mode": 0o700, "owner_matches": True},
        "credentials": {
            "cloudflare": {"present": True, "fingerprint": CF, "scope": "cloudflare.map"},
            "github_feedback": {"present": True, "fingerprint": GH, "scope": "feedback"},
        },
        "expected_foundation_digest": DIGEST,
        "cloudflare": {
            "tunnel_alias": "tunnel.cfargotunnel.com",
            "dns": [],
            "ingress": [{"hostname": None, "path": None, "service": "http_status:404"}],
        },
        "service": {"active": False, "unit_digest": None, "scope_level": "user", "generation": 0},
        "cluster": {"present": False, "owner": None},
        "health": {"ok": True, "running_sha": SHA},
    }
    probe.update(overrides)
    return probe


def darwin_probe(**overrides):
    probe = linux_probe(
        os="darwin",
        version="15.0",
        arch="arm64",
        distro="macos",
        package_manager="brew",
        service_manager="launchd",
        tty=True,
    )
    probe.update(overrides)
    return probe


class LifecycleCase(unittest.TestCase):
    def setUp(self):
        self._tmp = __import__("tempfile").TemporaryDirectory(prefix="w09-lifecycle-")
        self.tmp = Path(self._tmp.name)
        self._boxes = 0
        self.home = None
        self.inventory = None
        self.ws = None

    def tearDown(self):
        self._tmp.cleanup()

    def server_policy(self, **overrides):
        fields = {
            "principal": "installer",
            "app_id": APP,
            "repo": REPO,
            "host_id": "synthetic-unverified-host",
            "scopes": PLAN_SCOPES | frozenset({"lifecycle.uninstall"}),
            "run_scope": f"runs:{APP}",
            "resource_scope": f"inventory:{APP}",
            "enabled": True,
        }
        fields.update(overrides)
        return ServerPolicy(**fields)

    def service(self, probe=None, faults=(), policy=None):
        self._boxes += 1
        root = self.tmp / f"ws{self._boxes}"
        root.mkdir()
        home = root / "home"
        inventory = root / "inventory"
        home.mkdir()
        inventory.mkdir()
        host = SyntheticHost(probe if probe is not None else linux_probe(), faults)
        self.home = home
        self.inventory = inventory
        self.ws = root
        self.policy = policy or self.server_policy()
        return LifecycleService(root, host=host, authorizer=W02AuthorizationPort(self.policy)), host

    def view(self, svc):
        return svc.inspect(self.grant(svc))

    def grant(self, svc, scopes=PLAN_SCOPES, object_id=APP, principal="installer"):
        return svc.issue_grant(principal, scopes, object_id)

    def payload(self, **extra):
        body = {
            "run_id": "run-1",
            "app_id": APP,
            "repo": REPO,
            "domain": DOMAIN,
            "port": PORT,
            "track": "main",
            "title": "app",
            "foundation_digest": DIGEST,
            "parameters": {"install_deps": False, "github_https": False},
        }
        body.update(extra)
        return body

    def call(self, svc, operation, key, revision=0, grant=None, **extra):
        return svc.call(
            grant or self.grant(svc),
            operation,
            self.payload(**extra),
            idempotency_key=key,
            expected_revision=revision,
        )

    def assert_unproven(self, result):
        self.assertTrue(result.test_double)
        self.assertFalse(result.host_capability_proof)
        self.assertFalse(result.macci_proof)
        self.assertFalse(result.host_side_effect)
        self.assertEqual(result.executed_commands, ())
        self.assertEqual(result.evidence_class, "synthetic-test-double")

    def logical(self):
        return logical_install_path(self.home, REPO)


class FlagAndIsolationTests(LifecycleCase):
    def test_enablement_flags_are_false(self):
        self.assertFalse(PRODUCTION_ENABLEMENT)
        self.assertFalse(MOUNTED_IN_APP_MAIN)
        self.assertFalse(REAL_INSTALL)
        self.assertFalse(REAL_UNINSTALL)
        self.assertFalse(PRIVILEGE_ESCALATION)
        self.assertFalse(REAL_MERGE)
        self.assertFalse(REAL_DEPLOY)
        self.assertFalse(GITHUB_ACTIONS)
        self.assertFalse(MACCI_PROOF)
        self.assertFalse(ACCEPTANCE_ENABLED)
        self.assertFalse(OVERWRITE_USER_DATA)
        self.assertFalse(PRODUCTION_DB_ACCESS)
        self.assertFalse(PRODUCTION_VERIFICATION_ENABLED)
        self.assertEqual(ROUTE_MODULE, "app.control_routes_w09")
        self.assertEqual(CAPABILITIES, ("I01", "I02", "I03", "I04", "I05", "I06", "I08", "I09", "I10", "I11"))
        self.assertNotIn("I07", CAPABILITIES)

    def test_flags_are_boolean_constants(self):
        tree = ast.parse((ROOT / "app/control_lifecycle/flags.py").read_text(encoding="utf-8"))
        found = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id in FALSE_FLAGS:
                    found[node.targets[0].id] = node.value
        self.assertEqual(set(found), set(FALSE_FLAGS))
        for name, value in found.items():
            self.assertIsInstance(value, ast.Constant, name)
            self.assertIs(value.value, False, name)

    def test_shared_entry_points_and_live_modules_stay_untouched(self):
        for relative, digest in RESERVED_SHA256.items():
            actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
            self.assertEqual(actual, digest, relative)
        self.assertFalse((ROOT / "app/control_routes_w09.py").exists())
        main = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.assertNotIn("control_lifecycle", main)
        self.assertNotIn("control_routes_w09", main)
        names = sorted(
            path.name for path in (ROOT / "deploy/app-init").iterdir() if path.name != "__pycache__"
        )
        self.assertEqual(names, INIT_NAMES)
        workflows = sorted(path.name for path in (ROOT / ".github/workflows").iterdir())
        self.assertEqual(workflows, ["temporary-fallback-tests.yml"])
        for path in (ROOT / "app/control_lifecycle").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("subprocess", text, path.name)
            self.assertNotIn("os.system", text, path.name)
            self.assertNotIn("sudo ", text, path.name)
            self.assertNotIn("api.cloudflare.com", text, path.name)
            self.assertNotIn("import socket", text, path.name)


class EntryContractTests(LifecycleCase):
    def test_darwin_entry_forwards_parameters_and_checks_digest(self):
        svc, _host = self.service(darwin_probe())
        result = self.call(svc, "entry.plan", "idem-i01")
        self.assertTrue(result.ok)
        self.assertEqual(result.code, "OK")
        self.assertEqual(result.plan["adapter_id"], "darwin-launchd")
        self.assertEqual(result.plan["forwarded"]["repo"], REPO)
        self.assertEqual(result.plan["forwarded"]["domain"], DOMAIN)
        self.assertEqual(result.plan["forwarded"]["port"], PORT)
        self.assertEqual(result.plan["forwarded"]["track"], "main")
        self.assertEqual(result.plan["forwarded"]["parameters"]["install_deps"], False)
        self.assertFalse(result.plan["verified"])
        self.assertTrue(result.plan["supported_unverified"])
        self.assert_unproven(result)
        self.assertFalse(self.logical().exists())

    def test_digest_mismatch_blocks_before_install(self):
        svc, _host = self.service(darwin_probe())
        result = self.call(svc, "entry.plan", "idem-digest", foundation_digest="sha256:" + ("ff" * 32))
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "DIGEST_MISMATCH")
        self.assertEqual(result.plan["commands"], [])
        self.assert_unproven(result)

    def test_help_names_both_adapters_and_denies_execution(self):
        svc, _host = self.service(darwin_probe())
        result = self.call(svc, "entry.plan", "idem-help")
        help_text = result.plan["help"]
        self.assertIn("launchd", help_text)
        self.assertIn("systemd", help_text)
        self.assertIn("denied", help_text.lower())
        self.assertNotIn("macmini/isolated-tests-v1", help_text)

    def test_root_and_missing_tty_are_denied(self):
        root, _host = self.service(darwin_probe(uid=0, euid_is_root=True))
        denied = self.call(root, "entry.plan", "idem-root")
        self.assertEqual(denied.code, "PRIVILEGE_ESCALATION_DENIED")
        self.assertEqual(denied.plan["commands"], [])
        headless, _host = self.service(darwin_probe(tty=False))
        tty = self.call(headless, "entry.plan", "idem-tty")
        self.assertEqual(tty.code, "TTY_REQUIRED")
        self.assert_unproven(tty)

    def test_pilot_matrix_rejects_unknown_combinations(self):
        accepted = (
            linux_probe(),
            linux_probe(arch="arm64"),
            linux_probe(distro="debian", version="13"),
            linux_probe(distro="debian", version="13", arch="arm64"),
            darwin_probe(),
            darwin_probe(arch="x86_64"),
        )
        rejected = (
            linux_probe(distro="ubuntu", version="22.04"),
            linux_probe(distro="fedora", version="40", package_manager="dnf"),
            linux_probe(service_manager="openrc"),
            darwin_probe(package_manager="apt", service_manager="systemd"),
            linux_probe(arch=None),
        )
        for index, probe in enumerate(accepted):
            svc, _host = self.service(probe)
            result = self.call(svc, "entry.plan", f"idem-ok-{index}")
            self.assertEqual(result.code, "OK", probe["distro"])
            self.assertTrue(result.plan["supported_unverified"])
            self.assertFalse(result.plan["verified"])
            self.assertEqual(result.plan["commands"], [])
        for index, probe in enumerate(rejected):
            svc, _host = self.service(probe)
            result = self.call(svc, "entry.plan", f"idem-no-{index}")
            self.assertEqual(result.code, "PLATFORM_UNSUPPORTED", probe)
            self.assertEqual(result.http, 422)
            self.assertEqual(result.plan["commands"], [])
            self.assertFalse(result.plan["verified"])
            self.assert_unproven(result)


class PreflightTests(LifecycleCase):
    def test_preflight_reports_every_check_without_host_mutation(self):
        svc, host = self.service()
        result = self.call(svc, "preflight", "idem-preflight")
        self.assertTrue(result.ok)
        self.assertEqual(result.code, "OK")
        self.assertEqual(
            set(result.plan["checks"]),
            {
                "environment",
                "dependencies",
                "disk",
                "ports",
                "paths",
                "identity",
                "permissions",
                "background_credentials",
                "domain",
            },
        )
        self.assertTrue(all(item == "pass" for item in result.plan["checks"].values()))
        self.assertFalse(result.host_side_effect)
        self.assertEqual(host.calls, [])
        self.assertFalse(self.logical().exists())
        self.assertEqual(result.current_state["reservations"], {})

    def test_missing_dependency_and_disk_do_not_install(self):
        probe = linux_probe()
        probe["dependencies"] = dict(probe["dependencies"], node=False)
        probe["disk_free_bytes"] = REQUIRED_DISK_BYTES - 1
        svc, host = self.service(probe)
        result = self.call(svc, "preflight", "idem-deps")
        self.assertEqual(result.code, "DEPENDENCY_MISSING")
        self.assertIn("node", result.plan["missing"])
        self.assertEqual(result.plan["package_manager"], "apt")
        self.assertEqual(result.plan["package_manager_executed"], False)
        self.assertEqual(host.calls, [])
        self.assert_unproven(result)
        disk, _host = self.service(linux_probe(disk_free_bytes=REQUIRED_DISK_BYTES - 1))
        low = self.call(disk, "preflight", "idem-disk")
        self.assertEqual(low.code, "DISK_INSUFFICIENT")

    def test_identity_permissions_domain_and_credentials(self):
        identity, _host = self.service(linux_probe(identity={"principal": "other", "app_id": APP}))
        self.assertEqual(self.call(identity, "preflight", "idem-id").code, "IDENTITY_MISMATCH")
        perms, _host = self.service(linux_probe(permissions={"uid": 1000, "mode": 0o700, "owner_matches": False}))
        self.assertEqual(self.call(perms, "preflight", "idem-perm").code, "PERMISSION_DENIED")
        domain, _host = self.service()
        self.assertEqual(self.call(domain, "preflight", "idem-domain", domain="not a domain").code, "DOMAIN_INVALID")
        secret = "cf-secret-token-value-should-not-leak"
        leaked = self.call(self.service()[0], "preflight", "idem-secret", api_token=secret)
        self.assertEqual(leaked.code, "CREDENTIAL_BOUNDARY")
        self.assertNotIn(secret, leaked.diagnosis)
        self.assertNotIn(secret, str(leaked.plan))
        blob = " ".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in self.tmp.rglob("*")
            if path.is_file()
        )
        self.assertNotIn(secret, blob)
        swapped = self.call(self.service()[0], "cloudflare.plan", "idem-swap", credential_fingerprint=GH)
        self.assertEqual(swapped.code, "CREDENTIAL_BOUNDARY")
        self.assertNotIn(GH, swapped.diagnosis)

    def test_world_writable_existing_directory_is_not_overruled_by_the_probe(self):
        svc, _host = self.service(linux_probe(permissions={"uid": 1000, "mode": 0o700, "owner_matches": True}))
        path = self.logical()
        path.mkdir(parents=True)
        path.chmod(0o755)
        result = self.call(svc, "preflight", "idem-mode")
        self.assertEqual(result.code, "PERMISSION_DENIED")
        self.assertEqual(result.plan["checks"]["permissions"], "fail")


class PathAndPortTests(LifecycleCase):
    def test_logical_default_path_stays_inside_the_temporary_home(self):
        scratch = self.tmp / "scratch-home"
        scratch.mkdir()
        path = logical_install_path(scratch, "Acme/App")
        self.assertEqual(path, scratch / "Projects" / "app-init" / "acme--app")
        self.assertFalse(path.exists())
        real = Path.home() / "Projects" / "app-init" / "acme--app"
        before = real.exists()
        svc, _host = self.service()
        outside = self.call(svc, "path.resolve", "idem-outside", home=str(Path.home()))
        self.assertEqual(outside.code, "PATH_OUTSIDE_AUTHORIZATION")
        self.assertEqual(real.exists(), before)
        resolved = self.call(svc, "path.resolve", "idem-path", revision=0)
        self.assertEqual(resolved.code, "OK")
        self.assertEqual(resolved.plan["path"], str(self.logical()))
        self.assertFalse(resolved.plan["created"])
        self.assertIn("directory", resolved.plan["registry"])
        self.assertIn("cluster", resolved.plan["registry"])
        self.assertFalse(self.logical().exists())

    def test_both_install_directories_block_without_deletion(self):
        svc, _host = self.service()
        preferred = self.logical()
        legacy = self.home / "Applications" / "app-init" / APP
        preferred.mkdir(parents=True)
        legacy.mkdir(parents=True)
        (preferred / "keep-preferred").write_text("preferred", encoding="utf-8")
        (legacy / "keep-legacy").write_text("legacy", encoding="utf-8")
        result = self.call(svc, "path.resolve", "idem-both")
        self.assertEqual(result.code, "PATH_CONFLICT")
        self.assertEqual((preferred / "keep-preferred").read_text(encoding="utf-8"), "preferred")
        self.assertEqual((legacy / "keep-legacy").read_text(encoding="utf-8"), "legacy")
        self.assertEqual(result.plan["commands"], [])

    def test_port_conflict_is_reported_and_not_silently_moved(self):
        probe = linux_probe(ports={PORT: {"owner": "other-app", "pid": 4242, "command": "other-app"}})
        svc, _host = self.service(probe)
        preview = self.call(svc, "preflight", "idem-port-preview")
        self.assertEqual(preview.code, "PORT_CONFLICT")
        self.assertEqual(preview.current_state["reservations"], {})
        result = self.call(svc, "port.reserve", "idem-port", revision=preview.revision)
        self.assertEqual(result.code, "PORT_CONFLICT")
        self.assertEqual(result.plan["port"], PORT)
        self.assertEqual(result.plan["occupant"]["pid"], 4242)
        self.assertEqual(result.plan["occupant"]["owner"], "other-app")
        self.assertFalse(result.plan["killed"])
        self.assertFalse(result.plan["moved"])
        self.assertEqual(result.current_state["reservations"], {})
        self.assert_unproven(result)

    def test_reservation_is_held_in_the_journal_without_binding(self):
        svc, _host = self.service()
        result = self.call(svc, "port.reserve", "idem-hold")
        self.assertEqual(result.code, "OK")
        held = result.current_state["reservations"][str(PORT)]
        self.assertEqual(held["owner"], APP)
        self.assertTrue(held["held"])
        again = self.call(svc, "port.reserve", "idem-hold", revision=5)
        self.assertEqual(again.code, "IDEMPOTENCY_REPLAY")
        self.assertEqual(again.original_result["code"], "OK")
        self.assertEqual(again.current_state["reservations"][str(PORT)]["owner"], APP)


class JournalTests(LifecycleCase):
    def test_journal_survives_restart_and_rejects_client_clock(self):
        svc, host = self.service()
        first = self.call(svc, "preflight", "idem-journal")
        self.assertEqual(first.code, "OK")
        self.assertEqual(first.current_state["epoch"], 1)
        self.assertGreaterEqual(first.current_state["seq"], 1)
        self.assertEqual(first.plan["run_id"], "run-1")
        restarted = LifecycleService(self.ws, host=host, authorizer=W02AuthorizationPort(self.policy))
        self.assertEqual(self.view(restarted)["revision"], first.revision)
        replay = self.call(restarted, "preflight", "idem-journal", revision=99, grant=self.grant(restarted))
        self.assertEqual(replay.code, "IDEMPOTENCY_REPLAY")
        self.assertEqual(replay.original_result["code"], "OK")
        self.assertEqual(replay.revision, first.revision)
        clock = self.call(svc, "preflight", "idem-clock", received_at=123)
        self.assertEqual(clock.code, "SCHEMA_UNSUPPORTED")
        self.assertEqual(clock.http, 422)

    def test_authorization_precedes_idempotency(self):
        svc, _host = self.service()
        forged = Grant("installer", PLAN_SCOPES, APP, "forged", True)
        denied = svc.call(
            forged,
            "preflight",
            self.payload(),
            idempotency_key="idem-auth",
            expected_revision=0,
        )
        self.assertEqual(denied.code, "NOT_AUTHORIZED")
        self.assertEqual(denied.http, 403)
        self.assertEqual(self.view(svc)["revision"], 0)
        self.assertEqual(self.view(svc)["idempotency_count"], 0)
        mismatch = self.call(svc, "entry.plan", "idem-object", app_id="other--app")
        self.assertEqual(mismatch.code, "POLICY_DENIED")
        self.assertEqual(self.view(svc)["idempotency_count"], 0)
        allowed = self.call(svc, "preflight", "idem-auth")
        self.assertEqual(allowed.code, "OK")
        self.assertEqual(self.view(svc)["idempotency_count"], 1)

    def test_payload_mismatch_does_not_change_the_reservation(self):
        svc, _host = self.service(linux_probe(ports={PORT: None, PORT + 1: None}))
        first = self.call(svc, "port.reserve", "idem-cas-payload")
        self.assertEqual(first.code, "OK")
        second = self.call(svc, "port.reserve", "idem-cas-payload", revision=first.revision, port=PORT + 1)
        self.assertEqual(second.code, "IDEMPOTENCY_PAYLOAD_MISMATCH")
        self.assertEqual(second.http, 409)
        self.assertEqual(list(self.view(svc)["reservations"]), [str(PORT)])

    def test_lost_counter_opens_a_new_epoch_without_filling_the_old_gap(self):
        svc, _host = self.service()
        first = self.call(svc, "preflight", "idem-epoch-1")
        lost = self.call(svc, "journal.note_counter_loss", "idem-epoch-loss", revision=first.revision, run_id="run-loss")
        self.assertEqual(lost.code, "OK")
        second = self.call(
            svc,
            "path.resolve",
            "idem-epoch-2",
            revision=lost.revision,
            run_id="run-2",
        )
        self.assertEqual(second.code, "OK")
        epochs = self.view(svc)["epochs"]
        self.assertTrue(epochs["1"]["gap"])
        self.assertFalse(epochs["1"]["closed"])
        self.assertEqual(epochs["2"]["applied_seqs"], [1])
        self.assertTrue(epochs["1"]["gap"])


class CloudflareTests(LifecycleCase):
    def test_foreign_mapping_blocks_without_deletion(self):
        probe = linux_probe()
        probe["cloudflare"]["dns"] = [
            {"type": "A", "name": DOMAIN, "content": "203.0.113.5", "proxied": False}
        ]
        svc, host = self.service(probe)
        result = self.call(svc, "cloudflare.plan", "idem-dns", credential_fingerprint=CF)
        self.assertEqual(result.code, "DNS_FOREIGN")
        self.assertEqual(host.probe["cloudflare"]["dns"], probe["cloudflare"]["dns"])
        self.assertEqual(result.plan["deletes"], [])
        ingress = linux_probe()
        ingress["cloudflare"]["ingress"] = [
            {"hostname": DOMAIN, "path": None, "service": "http://127.0.0.1:9"},
            {"hostname": None, "path": None, "service": "http_status:404"},
        ]
        blocked, other = self.service(ingress)
        mapped = self.call(blocked, "cloudflare.plan", "idem-ingress", credential_fingerprint=CF)
        self.assertEqual(mapped.code, "INGRESS_FOREIGN")
        self.assertEqual(other.probe["cloudflare"]["ingress"], ingress["cloudflare"]["ingress"])

    def test_partial_dns_failure_stays_partial_and_recovery_does_not_delete(self):
        svc, host = self.service(faults={"dns_fails"})
        partial = self.call(svc, "cloudflare.apply_synthetic", "idem-partial", credential_fingerprint=CF, apply=True)
        self.assertEqual(partial.code, "PARTIAL")
        self.assertFalse(partial.ok)
        self.assertEqual(partial.current_state["deployment_state"], "PARTIAL")
        self.assertTrue(any(item.get("hostname") == DOMAIN for item in host.probe["cloudflare"]["ingress"]))
        self.assertEqual(host.probe["cloudflare"]["dns"], [])
        self.assertTrue(partial.simulated)
        self.assert_unproven(partial)
        planned = self.call(svc, "recovery.plan", "idem-recover-plan", revision=partial.revision, run_id="run-recover")
        self.assertEqual(planned.code, "OK")
        self.assertEqual(planned.plan["steps"], ["dns"])
        self.assertEqual(planned.plan["deletes"], [])
        host.faults.add("recovery_fails")
        failed = self.call(
            svc,
            "recovery.apply_synthetic",
            "idem-recover-fail",
            revision=planned.revision,
            run_id="run-recover-2",
        )
        self.assertEqual(failed.code, "RECOVERY_FAILED")
        self.assertEqual(failed.current_state["deployment_state"], "SAFE_FROZEN")
        self.assertEqual(host.probe["cloudflare"]["dns"], [])
        self.assertTrue(any(item.get("hostname") == DOMAIN for item in host.probe["cloudflare"]["ingress"]))
        self.assertEqual(failed.plan["deletes"], [])


class ReentryRecoveryTests(LifecycleCase):
    def _active_probe(self, digest, present=True):
        return linux_probe(
            service={"active": True, "unit_digest": digest, "scope_level": "user", "generation": 1},
            cluster={"present": present, "owner": APP},
        )

    def _write_cluster(self):
        cluster = self.logical() / ".postgres"
        cluster.mkdir(parents=True)
        cluster.chmod(0o700)
        marker = cluster / "PG_VERSION"
        marker.write_text("17\n", encoding="utf-8")
        marker.chmod(0o600)

    def test_consistent_duplicate_is_idle_and_mismatch_or_cluster_blocks(self):
        digest = service_unit_digest("linux-systemd", REPO, PORT, "user")
        svc, _host = self.service(self._active_probe(digest))
        self._write_cluster()
        result = self.call(svc, "entry.plan", "idem-dup")
        self.assertEqual(result.code, "ALREADY_ACTIVE")
        self.assertTrue(result.ok)
        self.assertEqual(result.plan["commands"], [])
        mismatch, _host = self.service(self._active_probe("sha256:" + ("ff" * 32)))
        self._write_cluster()
        self.assertEqual(self.call(mismatch, "entry.plan", "idem-unit").code, "UNIT_MISMATCH")
        incomplete, _host = self.service(self._active_probe(digest, present=False))
        blocked = self.call(incomplete, "entry.plan", "idem-cluster")
        self.assertEqual(blocked.code, "CLUSTER_INCOMPLETE")
        self.assertNotIn("initdb", blocked.plan["commands"])
        self.assert_unproven(blocked)

    def test_same_run_reentry_blocks(self):
        svc, _host = self.service(linux_probe(ports={PORT: None, PORT + 1: None}))
        first = self.call(svc, "port.reserve", "idem-run-a")
        self.assertEqual(first.code, "OK")
        second = self.call(svc, "port.reserve", "idem-run-b", revision=first.revision, port=PORT + 1)
        self.assertEqual(second.code, "RUN_REENTRY")
        self.assertEqual(list(self.view(svc)["reservations"]), [str(PORT)])

    def test_unknown_cross_app_symlink_and_failed_cleanup_delete_nothing_unowned(self):
        svc, host = self.service()
        owned = self.inventory / "owned.txt"
        foreign = self.inventory / "foreign.txt"
        mystery = self.inventory / "mystery.txt"
        target = self.inventory / "target.txt"
        owned.write_text("owned", encoding="utf-8")
        foreign.write_text("foreign", encoding="utf-8")
        mystery.write_text("mystery", encoding="utf-8")
        target.write_text("keep", encoding="utf-8")
        link = self.inventory / "linked.txt"
        link.symlink_to(target.name)
        observed = self.call(
            svc,
            "resource.observe",
            "idem-observe",
            resources=[
                {"resource_id": "owned", "kind": "file", "owner": APP, "relative": "owned.txt"},
                {"resource_id": "foreign", "kind": "file", "owner": "other--app", "relative": "foreign.txt"},
                {"resource_id": "mystery", "kind": "file", "owner": None, "relative": "mystery.txt"},
                {"resource_id": "linked", "kind": "file", "owner": APP, "relative": "linked.txt"},
            ],
        )
        self.assertEqual(observed.code, "OK")
        mixed = self.call(
            svc,
            "cleanup.synthetic",
            "idem-mixed",
            revision=observed.revision,
            run_id="run-clean",
            resource_ids=["owned", "foreign"],
        )
        self.assertEqual(mixed.code, "CROSS_APP_BOUNDARY")
        self.assertTrue(owned.exists())
        self.assertTrue(foreign.exists())
        unknown = self.call(
            svc,
            "cleanup.synthetic",
            "idem-unknown",
            revision=mixed.revision,
            run_id="run-unknown",
            resource_ids=["mystery"],
        )
        self.assertEqual(unknown.code, "OWNERSHIP_UNKNOWN")
        self.assertTrue(mystery.exists())
        unsafe = self.call(
            svc,
            "cleanup.synthetic",
            "idem-link",
            revision=unknown.revision,
            run_id="run-link",
            resource_ids=["linked"],
        )
        self.assertEqual(unsafe.code, "UNSAFE_PATH")
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")
        host.faults.add("cleanup_fails")
        failed = self.call(
            svc,
            "cleanup.synthetic",
            "idem-fault-clean",
            revision=unsafe.revision,
            run_id="run-fault",
            resource_ids=["owned"],
        )
        self.assertEqual(failed.code, "RECOVERY_FAILED")
        self.assertEqual(owned.read_text(encoding="utf-8"), "owned")
        host.faults.discard("cleanup_fails")
        healthy = self.call(
            svc,
            "cleanup.synthetic",
            "idem-owned",
            revision=failed.revision,
            run_id="run-owned",
            resource_ids=["owned"],
        )
        self.assertEqual(healthy.code, "OK")
        self.assertFalse(owned.exists())
        self.assertEqual(foreign.read_text(encoding="utf-8"), "foreign")
        self.assertEqual(mystery.read_text(encoding="utf-8"), "mystery")


class RebuildAndUserDataTests(LifecycleCase):
    def test_uninstall_requires_its_own_scope_and_removal_proof(self):
        svc, _host = self.service()
        denied = self.call(svc, "uninstall.plan", "idem-uninstall")
        self.assertEqual(denied.code, "POLICY_DENIED")
        self.assertEqual(self.view(svc)["idempotency_count"], 0)
        privileged = self.grant(svc, scopes=frozenset({"lifecycle.uninstall"}))
        frozen = self.call(
            svc,
            "uninstall.plan",
            "idem-uninstall",
            grant=privileged,
            install_kind="first_install",
            previous_healthy_sha="",
            removal_proof=False,
            destructive=True,
        )
        self.assertEqual(frozen.code, "SAFE_FROZEN")
        self.assertEqual(frozen.current_state["deployment_state"], "SAFE_FROZEN")
        self.assertEqual(frozen.plan["previous_healthy_sha"], "")
        self.assert_unproven(frozen)
        invented = self.call(
            svc,
            "uninstall.plan",
            "idem-invent",
            revision=frozen.revision,
            grant=privileged,
            run_id="run-invent",
            install_kind="first_install",
            previous_healthy_sha=SHA,
            removal_proof=True,
            destructive=True,
        )
        self.assertEqual(invented.code, "EVIDENCE_MISSING")
        self.assertNotEqual(invented.plan.get("previous_healthy_sha"), SHA)
        present = self.inventory / "still.txt"
        present.write_text("still", encoding="utf-8")
        noted = self.call(
            svc,
            "resource.observe",
            "idem-still",
            revision=invented.revision,
            run_id="run-still",
            resources=[{"resource_id": "still", "kind": "file", "owner": APP, "relative": "still.txt"}],
        )
        blocked = self.call(
            svc,
            "uninstall.plan",
            "idem-still-proof",
            revision=noted.revision,
            grant=privileged,
            run_id="run-still-proof",
            install_kind="first_install",
            previous_healthy_sha="",
            removal_proof=True,
            destructive=True,
            resource_ids=["still"],
        )
        self.assertNotEqual(blocked.code, "ROLLED_BACK")
        self.assertTrue(present.exists())
        present.unlink()
        rolled = self.call(
            svc,
            "uninstall.plan",
            "idem-rolled",
            revision=blocked.revision,
            grant=privileged,
            run_id="run-rolled",
            install_kind="first_install",
            previous_healthy_sha="",
            removal_proof=True,
            destructive=True,
            resource_ids=["still"],
        )
        self.assertEqual(rolled.code, "ROLLED_BACK")
        self.assertEqual(rolled.plan["previous_healthy_sha"], "")
        self.assertFalse(rolled.host_side_effect)
        no_change = self.call(
            svc,
            "service.commit",
            "idem-nochange",
            revision=rolled.revision,
            run_id="run-nochange",
            install_kind="first_install",
            action="no_change",
            previous_healthy_sha="",
        )
        self.assertEqual(no_change.code, "FIRST_INSTALL_NO_CHANGE")
        self.assertNotEqual(no_change.current_state["deployment_state"], "COMMIT")

    def test_rebuild_preserves_user_data_and_rejects_overwrite(self):
        svc, _host = self.service()
        root = self.logical()
        (root / "app").mkdir(parents=True)
        (root / "data").mkdir()
        (root / ".postgres").mkdir()
        root.chmod(0o700)
        (root / "app").chmod(0o700)
        (root / "data").chmod(0o700)
        (root / ".postgres").chmod(0o700)
        (root / ".env").write_text("TOKEN=keep\n", encoding="utf-8")
        (root / ".env").chmod(0o600)
        (root / "data" / "notes.txt").write_text("notes", encoding="utf-8")
        (root / ".postgres" / "PG_VERSION").write_text("17\n", encoding="utf-8")
        (root / "app" / "code.py").write_text("old\n", encoding="utf-8")
        blocked = self.call(
            svc,
            "rebuild.apply_synthetic",
            "idem-overwrite",
            install_kind="upgrade",
            previous_healthy_sha=SHA,
            overwrite_user_data=True,
            replacement="new\n",
        )
        self.assertEqual(blocked.code, "USER_DATA_PROTECTED")
        self.assertEqual((root / "app" / "code.py").read_text(encoding="utf-8"), "old\n")
        self.assertEqual((root / ".env").read_text(encoding="utf-8"), "TOKEN=keep\n")
        applied = self.call(
            svc,
            "rebuild.apply_synthetic",
            "idem-rebuild",
            revision=blocked.revision,
            run_id="run-rebuild",
            install_kind="upgrade",
            previous_healthy_sha=SHA,
            overwrite_user_data=False,
            replacement="new\n",
        )
        self.assertEqual(applied.code, "OK")
        self.assertTrue(applied.plan["preserves_user_data"])
        self.assertEqual((root / "app" / "code.py").read_text(encoding="utf-8"), "new\n")
        self.assertEqual((root / ".env").read_text(encoding="utf-8"), "TOKEN=keep\n")
        self.assertEqual((root / "data" / "notes.txt").read_text(encoding="utf-8"), "notes")
        self.assertEqual((root / ".postgres" / "PG_VERSION").read_text(encoding="utf-8"), "17\n")
        self.assertTrue(applied.simulated)
        self.assert_unproven(applied)
        missing = self.call(
            svc,
            "rebuild.plan",
            "idem-nosh",
            revision=applied.revision,
            run_id="run-nosh",
            install_kind="upgrade",
            previous_healthy_sha="",
        )
        self.assertEqual(missing.code, "EVIDENCE_MISSING")

    def test_seed_feedback_and_scheduler_are_not_a_single_success_transaction(self):
        svc, _host = self.service()
        static = self.call(svc, "seed.observe", "idem-static", static_only=True, syntax_ok=True)
        self.assertEqual(static.code, "EVIDENCE_MISSING")
        self.assertFalse(static.plan["static_only_sufficient"])
        absent = self.call(
            svc,
            "seed.observe",
            "idem-scheduler",
            revision=static.revision,
            run_id="run-scheduler",
            static_only=False,
            syntax_ok=True,
            relative_imports_ok=True,
            user_data_preserved=True,
            scheduler_present=False,
            post_observed=False,
            receipt_present=False,
        )
        self.assertEqual(absent.code, "OK")
        self.assertFalse(absent.plan["auto_advance"])
        self.assertFalse(absent.plan["beyond_draft"])
        self.assertEqual(absent.plan["scheduler"], "absent")
        unknown = self.call(
            svc,
            "seed.observe",
            "idem-post",
            revision=absent.revision,
            run_id="run-post",
            static_only=False,
            syntax_ok=True,
            relative_imports_ok=True,
            user_data_preserved=True,
            scheduler_present=True,
            post_observed=True,
            receipt_present=False,
        )
        self.assertEqual(unknown.code, "SIDE_EFFECT_UNKNOWN")
        paired = self.call(
            svc,
            "seed.observe",
            "idem-pair",
            revision=unknown.revision,
            run_id="run-pair",
            static_only=False,
            syntax_ok=True,
            relative_imports_ok=True,
            user_data_preserved=True,
            scheduler_present=True,
            post_observed=True,
            receipt_present=True,
            post_id=7,
            receipt_id=7,
        )
        self.assertEqual(paired.code, "OK")
        self.assertFalse(paired.plan["single_transaction"])
        differ = self.call(
            svc,
            "seed.observe",
            "idem-differ",
            revision=paired.revision,
            run_id="run-differ",
            static_only=False,
            syntax_ok=True,
            relative_imports_ok=True,
            user_data_preserved=True,
            scheduler_present=True,
            post_observed=True,
            receipt_present=True,
            post_id=7,
            receipt_id=8,
        )
        self.assertEqual(differ.code, "SIDE_EFFECT_UNKNOWN")


class PlatformFdAndFenceTests(LifecycleCase):
    def test_linux_and_darwin_tool_paths_differ_and_do_not_execute(self):
        linux, linux_host = self.service(linux_probe())
        darwin, darwin_host = self.service(darwin_probe())
        left = self.call(linux, "service.plan", "idem-linux-svc", action="enable", scope_level="user", generation=0)
        right = self.call(darwin, "service.plan", "idem-darwin-svc", action="enable", scope_level="user", generation=0)
        self.assertEqual(left.plan["adapter"]["service_tool"], "systemctl")
        self.assertEqual(left.plan["adapter"]["package_manager"], "apt")
        self.assertEqual(left.plan["adapter"]["fd_namespace"], "/proc/self/fd")
        self.assertEqual(left.plan["adapter"]["socket_root"], "/run/user")
        self.assertEqual(left.plan["adapter"]["unit_kind"], "systemd-unit")
        self.assertEqual(right.plan["adapter"]["service_tool"], "launchctl")
        self.assertEqual(right.plan["adapter"]["package_manager"], "brew")
        self.assertEqual(right.plan["adapter"]["fd_namespace"], "/dev/fd")
        self.assertEqual(right.plan["adapter"]["socket_root"], "/private/tmp")
        self.assertEqual(right.plan["adapter"]["unit_kind"], "plist")
        self.assertEqual(left.plan["execution"], "denied")
        self.assertEqual(right.plan["execution"], "denied")
        self.assertTrue(left.plan["unit"]["autostart"])
        self.assertEqual(linux_host.calls, [])
        self.assertEqual(darwin_host.calls, [])
        self.assert_unproven(left)
        self.assert_unproven(right)
        system = self.call(
            linux,
            "service.plan",
            "idem-system",
            revision=left.revision,
            run_id="run-system",
            action="enable",
            scope_level="system",
            generation=0,
        )
        self.assertEqual(system.code, "PRIVILEGE_ESCALATION_DENIED")
        self.assertEqual(system.plan["commands"], [])

    def test_w06_proc_self_fd_is_not_a_darwin_proof(self):
        self.assertEqual(fd_namespace("linux-systemd"), "/proc/self/fd")
        self.assertEqual(fd_namespace("darwin-launchd"), "/dev/fd")
        self.assertTrue(w06_proc_self_fd_compatible("linux-systemd"))
        self.assertFalse(w06_proc_self_fd_compatible("darwin-launchd"))
        svc, _host = self.service()
        note = self.ws / "fdroot"
        note.mkdir()
        (note / "note.txt").write_text("inside", encoding="utf-8")
        contained = self.call(
            svc,
            "fd.observe",
            "idem-fd-linux",
            adapter_id="linux-systemd",
            directory=str(note),
            relative="note.txt",
        )
        self.assertEqual(contained.code, "OK")
        self.assertTrue(contained.plan["contained"])
        self.assertEqual(contained.plan["prefix"], "/proc/self/fd")
        self.assertEqual(contained.plan["observation"], "linux-proc-self-fd")
        self.assertFalse(contained.macci_proof)
        self.assertFalse(contained.host_capability_proof)
        forged = self.call(
            svc,
            "fd.observe",
            "idem-fd-forge",
            revision=contained.revision,
            run_id="run-forge",
            adapter_id="darwin-launchd",
            use_w06_proc_path=True,
            directory=str(note),
            relative="note.txt",
        )
        self.assertEqual(forged.code, "W06_PROC_FD_INCOMPATIBLE")
        self.assertFalse(forged.plan["contained"])
        escaped = self.call(
            svc,
            "fd.observe",
            "idem-fd-escape",
            revision=forged.revision,
            run_id="run-escape",
            adapter_id="darwin-launchd",
            synthetic_directory="/synthetic/app",
            synthetic_located="/synthetic/other/note.txt",
        )
        self.assertEqual(escaped.code, "PATH_ESCAPED")
        self.assertFalse(escaped.plan["contained"])
        held = self.call(
            svc,
            "fd.observe",
            "idem-fd-darwin",
            revision=escaped.revision,
            run_id="run-darwin-fd",
            adapter_id="darwin-launchd",
            synthetic_directory="/synthetic/app",
            synthetic_located="/synthetic/app/note.txt",
        )
        self.assertEqual(held.code, "OK")
        self.assertTrue(held.plan["contained"])
        self.assertEqual(held.plan["prefix"], "/dev/fd")
        self.assertEqual(held.plan["observation"], "darwin-dev-fd-synthetic")
        self.assertFalse(held.host_capability_proof)

    def test_linux_receipt_cannot_impersonate_macci_and_health_needs_a_durable_receipt(self):
        svc, _host = self.service()
        forged = self.call(
            svc,
            "service.record_receipt",
            "idem-macci",
            context="macmini/isolated-tests-v1",
            running_sha=SHA,
        )
        self.assertEqual(forged.code, "FORGED_MACCI_CONTEXT")
        self.assertFalse(forged.macci_proof)
        self.assertNotEqual(self.view(svc)["deployment_state"], "COMMIT")
        health = self.call(
            svc,
            "service.plan",
            "idem-health",
            revision=forged.revision,
            run_id="run-health",
            action="health",
            scope_level="user",
            generation=0,
        )
        self.assertEqual(health.code, "RECONCILE")
        self.assertEqual(health.current_state["deployment_state"], "RECONCILE")
        stored = self.call(
            svc,
            "service.record_receipt",
            "idem-receipt",
            revision=health.revision,
            run_id="run-receipt",
            context="linux-host/isolated-tests-v1",
            running_sha=SHA,
        )
        self.assertEqual(stored.code, "OK")
        self.assertEqual(stored.plan["context"], "linux-host/isolated-tests-v1")
        self.assertFalse(stored.macci_proof)
        pending = self.call(
            svc,
            "service.plan",
            "idem-pending",
            revision=stored.revision,
            run_id="run-pending",
            action="health",
            scope_level="user",
            generation=0,
        )
        self.assertEqual(pending.code, "COMMIT_PENDING")
        self.assertEqual(pending.current_state["deployment_state"], "COMMIT_PENDING")
        self.assertNotEqual(pending.current_state["deployment_state"], "COMMIT")
        self.assert_unproven(pending)
        restart = self.call(
            svc,
            "service.plan",
            "idem-restart",
            revision=pending.revision,
            run_id="run-restart",
            action="restart",
            scope_level="user",
            generation=0,
        )
        self.assertEqual(restart.code, "OK")
        stale = self.call(
            svc,
            "service.plan",
            "idem-stale",
            revision=restart.revision,
            run_id="run-stale",
            action="health",
            scope_level="user",
            generation=0,
        )
        self.assertEqual(stale.code, "LEASE_FENCE_REJECTED")
        self.assertEqual(stale.http, 409)
        self.assert_unproven(stale)

    def test_compare_and_swap_under_concurrency(self):
        svc, _host = self.service(linux_probe(ports={PORT: None, PORT + 1: None}))
        grant = self.grant(svc)
        barrier = threading.Barrier(2)
        results = []

        def worker(port, key, run_id):
            barrier.wait()
            results.append(
                svc.call(
                    grant,
                    "port.reserve",
                    self.payload(port=port, run_id=run_id),
                    idempotency_key=key,
                    expected_revision=0,
                )
            )

        threads = [
            threading.Thread(target=worker, args=(PORT, "idem-thread-a", "run-a")),
            threading.Thread(target=worker, args=(PORT + 1, "idem-thread-b", "run-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(result.code for result in results), ["CONFLICT_REVISION", "OK"])
        self.assertEqual(len(self.view(svc)["reservations"]), 1)
        for result in results:
            self.assert_unproven(result)

    def test_journal_fault_does_not_publish_a_partial_record(self):
        svc, _host = self.service(faults={"journal_fsync_fails"})
        failed = self.call(svc, "preflight", "idem-journal-fault")
        self.assertEqual(failed.code, "JOURNAL_FAILED")
        self.assertEqual(self.view(svc)["revision"], 0)
        self.assertFalse((self.ws / "lifecycle-journal.json").exists())
        self.assertEqual(self.view(svc)["idempotency_count"], 0)


def w09_cross_process_reserve(workspace, port, key, run_id, ready, start, out):
    """Child used to show whether two processes really compare-and-swap one journal."""

    import json
    import time
    from pathlib import Path

    from app.control_lifecycle import LifecycleService, SyntheticHost
    from app.control_lifecycle.grant import ServerPolicy, W02AuthorizationPort

    workspace = Path(workspace)
    probe = linux_probe(ports={PORT: None, PORT + 1: None})
    policy = ServerPolicy(
        principal="installer",
        app_id=APP,
        repo=REPO,
        host_id="synthetic-unverified-host",
        scopes=PLAN_SCOPES | frozenset({"lifecycle.uninstall"}),
        run_scope=f"runs:{APP}",
        resource_scope=f"inventory:{APP}",
        enabled=True,
    )
    svc = LifecycleService(workspace, host=SyntheticHost(probe), authorizer=W02AuthorizationPort(policy))
    grant = svc.issue_grant("installer", PLAN_SCOPES, APP)
    Path(ready).write_text("ready", encoding="utf-8")
    start_path = Path(start)
    while not start_path.exists():
        time.sleep(0.02)
    payload = {
        "run_id": run_id,
        "app_id": APP,
        "repo": REPO,
        "domain": DOMAIN,
        "port": port,
        "track": "main",
        "title": "app",
        "foundation_digest": DIGEST,
        "parameters": {"install_deps": False, "github_https": False},
    }
    result = svc.call(grant, "port.reserve", payload, idempotency_key=key, expected_revision=0)
    Path(out).write_text(
        json.dumps({"code": result.code, "ok": result.ok, "http": result.http}),
        encoding="utf-8",
    )


class ReviewCounterexampleTests(LifecycleCase):
    """P0 counterexamples. These describe the required contract and fail on the unfixed tree."""

    def test_issue_grant_rejects_unbound_principal_scope_and_app(self):
        from app.control_lifecycle.errors import LifecycleError

        svc, _host = self.service()
        with self.assertRaises(LifecycleError):
            svc.issue_grant("intruder", PLAN_SCOPES, APP)
        with self.assertRaises(LifecycleError):
            svc.issue_grant("installer", PLAN_SCOPES | frozenset({"lifecycle.godmode"}), APP)
        with self.assertRaises(LifecycleError):
            svc.issue_grant("installer", PLAN_SCOPES, "other--app")

    def test_default_authorizer_and_host_double_are_not_authority(self):
        from app.control_lifecycle.errors import LifecycleError

        root = self.tmp / "deny-ws"
        root.mkdir()
        (root / "home").mkdir()
        (root / "inventory").mkdir()
        host = SyntheticHost(linux_probe())
        svc = LifecycleService(root, host=host)
        with self.assertRaises(LifecycleError) as caught:
            svc.issue_grant("installer", PLAN_SCOPES, APP)
        self.assertEqual(caught.exception.code, "NOT_AUTHORIZED")
        forged = Grant("installer", PLAN_SCOPES, APP, "forged", True)
        denied = svc.call(
            forged,
            "preflight",
            self.payload(),
            idempotency_key="idem-default-deny",
            expected_revision=0,
        )
        self.assertEqual(denied.code, "NOT_AUTHORIZED")
        self.assertFalse(denied.ok)
        self.assertNotEqual(denied.http, 200)
        promoted = LifecycleService(root, host=host, authorizer=host)
        with self.assertRaises(LifecycleError):
            promoted.issue_grant("installer", PLAN_SCOPES, APP)

    def test_inspect_requires_a_server_grant(self):
        from app.control_lifecycle.errors import LifecycleError

        svc, _host = self.service()
        forged = Grant("installer", PLAN_SCOPES, APP, "forged", True)
        with self.assertRaises(LifecycleError):
            svc.inspect(forged)
        seen = svc.inspect(self.grant(svc))
        self.assertIn("revision", seen)

    def test_failed_receipt_replay_is_not_http_200(self):
        svc, _host = self.service()
        forged = self.call(
            svc,
            "service.record_receipt",
            "idem-forged-replay",
            context="macmini/isolated-tests-v1",
            running_sha=SHA,
        )
        self.assertEqual(forged.code, "FORGED_MACCI_CONTEXT")
        self.assertFalse(forged.ok)
        replay = self.call(
            svc,
            "service.record_receipt",
            "idem-forged-replay",
            revision=99,
            context="macmini/isolated-tests-v1",
            running_sha=SHA,
        )
        self.assertFalse(replay.ok)
        self.assertNotEqual(replay.http, 200)
        self.assertEqual(replay.original_result["code"], "FORGED_MACCI_CONTEXT")
        self.assertNotEqual(replay.current_state["deployment_state"], "COMMIT")

    def test_caller_context_and_running_sha_do_not_prove_a_service(self):
        svc, _host = self.service()
        claimed = self.call(
            svc,
            "service.record_receipt",
            "idem-prove",
            context="linux-host/isolated-tests-v1",
            running_sha=SHA,
            proves_service=True,
        )
        self.assertFalse(claimed.ok)
        self.assertFalse(claimed.host_capability_proof)
        self.assertFalse(claimed.macci_proof)
        self.assertNotEqual(claimed.current_state["deployment_state"], "COMMIT")
        mismatched = self.call(
            svc,
            "service.record_receipt",
            "idem-sha",
            revision=claimed.revision,
            run_id="run-sha",
            context="linux-host/isolated-tests-v1",
            running_sha="b" * 40,
        )
        self.assertFalse(mismatched.ok)
        self.assertFalse(mismatched.host_capability_proof)
        self.assertNotEqual(mismatched.current_state["deployment_state"], "COMMIT")

    def test_repo_and_run_scope_are_bound_before_idempotency(self):
        svc, _host = self.service()
        other_repo = self.call(svc, "preflight", "idem-repo", repo="other/repo")
        self.assertEqual(other_repo.code, "POLICY_DENIED")
        self.assertEqual(other_repo.http, 403)
        self.assertEqual(self.view(svc)["idempotency_count"], 0)
        bad_run = self.call(svc, "preflight", "idem-run", run_id="../escape")
        self.assertEqual(bad_run.code, "POLICY_DENIED")
        self.assertEqual(self.view(svc)["idempotency_count"], 0)

    def test_same_key_on_another_host_is_not_a_successful_replay(self):
        svc, host = self.service()
        first = self.call(svc, "preflight", "idem-host-bind")
        self.assertEqual(first.code, "OK")
        other = LifecycleService(
            self.ws,
            host=SyntheticHost(linux_probe()),
            authorizer=W02AuthorizationPort(self.server_policy(host_id="host-b")),
        )
        replay = other.call(
            other.issue_grant("installer", PLAN_SCOPES, APP),
            "preflight",
            self.payload(),
            idempotency_key="idem-host-bind",
            expected_revision=99,
        )
        self.assertEqual(replay.code, "IDEMPOTENCY_PAYLOAD_MISMATCH")
        self.assertFalse(replay.ok)
        self.assertNotEqual(replay.http, 200)
        self.assertEqual(replay.original_result, None)

    def test_cross_instance_compare_and_swap_re_reads(self):
        probe = linux_probe(ports={PORT: None, PORT + 1: None})
        svc, host = self.service(probe)
        other = LifecycleService(self.ws, host=host, authorizer=W02AuthorizationPort(self.policy))
        first = self.call(svc, "port.reserve", "idem-instance-a")
        self.assertEqual(first.code, "OK")
        second = other.call(
            other.issue_grant("installer", PLAN_SCOPES, APP),
            "port.reserve",
            self.payload(port=PORT + 1, run_id="run-instance-b"),
            idempotency_key="idem-instance-b",
            expected_revision=0,
        )
        self.assertEqual(second.code, "CONFLICT_REVISION")
        self.assertFalse(second.ok)
        journal = __import__("json").loads((self.ws / "lifecycle-journal.json").read_text(encoding="utf-8"))
        self.assertEqual(journal["revision"], first.revision)
        self.assertEqual(set(journal["reservations"]), {str(PORT)})
        self.assertEqual(journal["reservations"][str(PORT)]["owner"], APP)

    def test_cross_process_compare_and_swap_holds(self):
        import multiprocessing

        svc, _host = self.service(linux_probe(ports={PORT: None, PORT + 1: None}))
        self.assertIsNotNone(svc)
        ctx = multiprocessing.get_context("fork")
        ready_a = self.ws / "ready-a"
        ready_b = self.ws / "ready-b"
        start = self.ws / "start"
        out_a = self.ws / "out-a.json"
        out_b = self.ws / "out-b.json"
        procs = [
            ctx.Process(
                target=w09_cross_process_reserve,
                args=(str(self.ws), PORT, "idem-proc-a", "run-proc-a", str(ready_a), str(start), str(out_a)),
            ),
            ctx.Process(
                target=w09_cross_process_reserve,
                args=(str(self.ws), PORT, "idem-proc-b", "run-proc-b", str(ready_b), str(start), str(out_b)),
            ),
        ]
        for proc in procs:
            proc.start()
        deadline = __import__("time").time() + 10
        while __import__("time").time() < deadline and not (ready_a.exists() and ready_b.exists()):
            __import__("time").sleep(0.02)
        start.write_text("go", encoding="utf-8")
        for proc in procs:
            proc.join(10)
        self.assertTrue(out_a.exists(), "process A produced no result")
        self.assertTrue(out_b.exists(), "process B produced no result")
        import json

        results = [json.loads(out_a.read_text(encoding="utf-8")), json.loads(out_b.read_text(encoding="utf-8"))]
        self.assertEqual(sorted(item["code"] for item in results), ["CONFLICT_REVISION", "OK"])
        self.assertEqual(sorted(item["ok"] for item in results), [False, True])
        journal = json.loads((self.ws / "lifecycle-journal.json").read_text(encoding="utf-8"))
        self.assertEqual(journal["revision"], 1)
        self.assertEqual(list(journal["reservations"]), [str(PORT)])
        self.assertEqual(journal["reservations"][str(PORT)]["owner"], APP)
        for proc in procs:
            self.assertEqual(proc.exitcode, 0)

    def test_port_reserve_does_not_overwrite_another_app(self):
        svc, _host = self.service(linux_probe(ports={PORT: None}))
        first = self.call(svc, "port.reserve", "idem-app-a")
        self.assertEqual(first.code, "OK")
        self.assertEqual(first.current_state["reservations"][str(PORT)]["owner"], APP)
        try:
            other = svc.issue_grant("installer", PLAN_SCOPES, "other--app")
        except Exception as exc:
            from app.control_lifecycle.errors import LifecycleError

            self.assertIsInstance(exc, LifecycleError)
            self.assertEqual(first.current_state["reservations"][str(PORT)]["owner"], APP)
            return
        second = svc.call(
            other,
            "port.reserve",
            self.payload(app_id="other--app", run_id="run-other-app"),
            idempotency_key="idem-app-b",
            expected_revision=first.revision,
        )
        self.assertEqual(second.code, "PORT_CONFLICT")
        self.assertFalse(second.ok)
        self.assertEqual(second.current_state["reservations"][str(PORT)]["owner"], APP)

    def test_illegal_port_generation_and_release_are_exact(self):
        svc, _host = self.service(linux_probe(ports={PORT: None, PORT + 1: None}))
        illegal = self.call(svc, "port.reserve", "idem-zero", port=0)
        self.assertEqual(illegal.code, "PORT_ILLEGAL")
        self.assertFalse(illegal.ok)
        self.assertNotEqual(illegal.http, 200)
        self.assertNotIn("0", illegal.current_state["reservations"])
        huge = self.call(
            svc,
            "port.reserve",
            "idem-huge",
            revision=illegal.revision,
            run_id="run-huge",
            port=70000,
        )
        self.assertEqual(huge.code, "PORT_ILLEGAL")
        held = self.call(
            svc,
            "port.reserve",
            "idem-gen-hold",
            revision=huge.revision,
            run_id="run-gen-hold",
            reservation_generation=1,
        )
        self.assertEqual(held.code, "OK")
        self.assertEqual(held.current_state["reservations"][str(PORT)]["generation"], 1)
        mismatch = self.call(
            svc,
            "port.reserve",
            "idem-gen-mismatch",
            revision=held.revision,
            run_id="run-gen-mismatch",
            reservation_generation=2,
        )
        self.assertEqual(mismatch.code, "PORT_CONFLICT")
        self.assertEqual(mismatch.current_state["reservations"][str(PORT)]["owner"], APP)
        self.assertEqual(mismatch.current_state["reservations"][str(PORT)]["generation"], 1)
        wrong = self.call(
            svc,
            "port.release",
            "idem-release-wrong",
            revision=mismatch.revision,
            run_id="run-release-wrong",
            reservation_generation=2,
        )
        self.assertEqual(wrong.code, "PORT_CONFLICT")
        self.assertTrue(wrong.current_state["reservations"][str(PORT)]["held"])
        released = self.call(
            svc,
            "port.release",
            "idem-release",
            revision=wrong.revision,
            run_id="run-release",
            reservation_generation=1,
        )
        self.assertEqual(released.code, "OK")
        self.assertNotIn(str(PORT), released.current_state["reservations"])

    def test_cleanup_crash_persists_intent_and_unknown_is_not_replayed(self):
        svc, host = self.service()
        owned = self.inventory / "owned.txt"
        keeper = self.inventory / "keeper.txt"
        owned.write_text("owned", encoding="utf-8")
        keeper.write_text("keep", encoding="utf-8")
        observed = self.call(
            svc,
            "resource.observe",
            "idem-crash-observe",
            resources=[{"resource_id": "owned", "kind": "file", "owner": APP, "relative": "owned.txt"}],
        )
        self.assertEqual(observed.code, "OK")
        host.faults.add("crash_after_intent")
        crashed = self.call(
            svc,
            "cleanup.synthetic",
            "idem-crash-clean",
            revision=observed.revision,
            run_id="run-crash-clean",
            resource_ids=["owned"],
        )
        self.assertFalse(crashed.ok)
        self.assertNotEqual(crashed.http, 200)
        self.assertEqual(owned.read_text(encoding="utf-8"), "owned")
        journal = (self.ws / "lifecycle-journal.json").read_text(encoding="utf-8")
        self.assertIn("intent", journal)
        self.assertIn("owned.txt", journal)
        replay = self.call(
            svc,
            "cleanup.synthetic",
            "idem-crash-clean",
            revision=99,
            run_id="run-crash-clean",
            resource_ids=["owned"],
        )
        self.assertFalse(replay.ok)
        self.assertNotEqual(replay.http, 200)
        self.assertEqual(owned.read_text(encoding="utf-8"), "owned")
        self.assertNotEqual(replay.code, "OK")

    def test_rebuild_crash_does_not_write_before_a_durable_intent(self):
        svc, host = self.service()
        root = self.logical()
        (root / "app").mkdir(parents=True)
        code = root / "app" / "code.py"
        code.write_text("old\n", encoding="utf-8")
        host.faults.add("crash_after_intent")
        crashed = self.call(
            svc,
            "rebuild.apply_synthetic",
            "idem-crash-rebuild",
            install_kind="upgrade",
            previous_healthy_sha=SHA,
            overwrite_user_data=False,
            replacement="new\n",
        )
        self.assertFalse(crashed.ok)
        self.assertNotEqual(crashed.http, 200)
        self.assertEqual(code.read_text(encoding="utf-8"), "old\n")
        journal = (self.ws / "lifecycle-journal.json").read_text(encoding="utf-8")
        self.assertIn("intent", journal)

    def test_symlink_journal_is_not_followed(self):
        import json

        svc, host = self.service()
        self.assertIsNotNone(svc)
        outside = self.tmp / "outside-journal.json"
        outside.write_text(json.dumps({"revision": 0, "secret": "outside-marker"}), encoding="utf-8")
        link = self.ws / "lifecycle-journal.json"
        link.symlink_to(outside)
        restarted = LifecycleService(self.ws, host=host, authorizer=W02AuthorizationPort(self.policy))
        result = restarted.call(
            restarted.issue_grant("installer", PLAN_SCOPES, APP),
            "preflight",
            self.payload(),
            idempotency_key="idem-symlink",
            expected_revision=0,
        )
        self.assertFalse(result.ok)
        self.assertNotEqual(result.http, 200)
        self.assertTrue(link.is_symlink())
        self.assertIn("outside-marker", outside.read_text(encoding="utf-8"))

    def test_hardlink_and_corrupt_journal_are_rejected(self):
        import json
        import os

        svc, host = self.service()
        first = self.call(svc, "preflight", "idem-hard-seed")
        self.assertEqual(first.code, "OK")
        journal = self.ws / "lifecycle-journal.json"
        alias = self.ws / "journal-alias.json"
        os.link(journal, alias)
        linked = self.call(
            svc,
            "path.resolve",
            "idem-hard-use",
            revision=first.revision,
            run_id="run-hard",
        )
        self.assertFalse(linked.ok)
        self.assertNotEqual(linked.http, 200)
        self.assertEqual(os.lstat(journal).st_ino, os.lstat(alias).st_ino)
        os.unlink(alias)
        journal.write_text("{", encoding="utf-8")
        restarted = LifecycleService(self.ws, host=host, authorizer=W02AuthorizationPort(self.policy))
        corrupt = restarted.call(
            restarted.issue_grant("installer", PLAN_SCOPES, APP),
            "preflight",
            self.payload(run_id="run-corrupt"),
            idempotency_key="idem-corrupt",
            expected_revision=0,
        )
        self.assertFalse(corrupt.ok)
        self.assertEqual(corrupt.code, "CORRUPT_JOURNAL")
        self.assertNotEqual(corrupt.http, 200)
        self.assertEqual(journal.read_text(encoding="utf-8"), "{")

    def test_dev_fd_string_map_is_not_safe_resolution(self):
        svc, _host = self.service()
        mapped = self.call(
            svc,
            "fd.observe",
            "idem-devfd-map",
            adapter_id="darwin-launchd",
            synthetic_directory="/dev/fd",
            synthetic_located="/dev/fd/3",
            resolve_dev_fd=True,
            dev_fd_map={"3": "/etc/passwd"},
        )
        self.assertEqual(mapped.code, "DEV_FD_STRING_MAP_UNSAFE")
        self.assertFalse(mapped.ok)
        self.assertFalse(mapped.plan["contained"])
        self.assertFalse(mapped.plan["safe_path_resolution"])
        self.assertFalse(mapped.host_capability_proof)

    def test_cleanup_refuses_a_symlink_swap_before_unlink(self):
        svc, host = self.service()
        owned = self.inventory / "owned.txt"
        keeper = self.inventory / "keeper.txt"
        owned.write_text("owned", encoding="utf-8")
        keeper.write_text("keep", encoding="utf-8")
        observed = self.call(
            svc,
            "resource.observe",
            "idem-swap-observe",
            resources=[{"resource_id": "owned", "kind": "file", "owner": APP, "relative": "owned.txt"}],
        )
        host.faults.add("replace_with_symlink_before_unlink")
        swapped = self.call(
            svc,
            "cleanup.synthetic",
            "idem-swap-clean",
            revision=observed.revision,
            run_id="run-swap",
            resource_ids=["owned"],
        )
        self.assertNotEqual(swapped.code, "OK")
        self.assertFalse(swapped.ok)
        self.assertEqual(keeper.read_text(encoding="utf-8"), "keep")
        self.assertFalse(swapped.host_side_effect)

    def test_authorized_cross_app_port_conflict_blocks(self):
        from app.control_lifecycle.grant import ServerPolicy, W02AuthorizationPort

        probe = linux_probe(ports={PORT: None})
        root = self.tmp / "two-app"
        root.mkdir()
        (root / "home").mkdir()
        (root / "inventory").mkdir()
        host = SyntheticHost(probe)
        policy_a = ServerPolicy(
            principal="installer",
            app_id=APP,
            repo=REPO,
            host_id="host-a",
            scopes=PLAN_SCOPES | frozenset({"lifecycle.uninstall"}),
            run_scope=f"runs:{APP}",
            resource_scope=f"inventory:{APP}",
            enabled=True,
        )
        policy_b = ServerPolicy(
            principal="installer",
            app_id="other--app",
            repo="other/app",
            host_id="host-a",
            scopes=PLAN_SCOPES,
            run_scope="runs:other--app",
            resource_scope="inventory:other--app",
            enabled=True,
        )
        left = LifecycleService(root, host=host, authorizer=W02AuthorizationPort(policy_a))
        right = LifecycleService(root, host=host, authorizer=W02AuthorizationPort(policy_b))
        grant_a = left.issue_grant("installer", PLAN_SCOPES, APP)
        first = left.call(
            grant_a,
            "port.reserve",
            self.payload(),
            idempotency_key="idem-two-a",
            expected_revision=0,
        )
        self.assertEqual(first.code, "OK")
        grant_b = right.issue_grant("installer", PLAN_SCOPES, "other--app")
        second = right.call(
            grant_b,
            "port.reserve",
            self.payload(app_id="other--app", repo="other/app", run_id="run-two-b"),
            idempotency_key="idem-two-b",
            expected_revision=first.revision,
        )
        self.assertEqual(second.code, "PORT_CONFLICT")
        self.assertFalse(second.ok)
        self.assertEqual(second.current_state["reservations"][str(PORT)]["owner"], APP)

    def test_residuals_stay_unclaimed(self):
        from app.control_lifecycle import flags

        self.assertFalse(flags.W06_IN_TREE)
        self.assertFalse(flags.JOINT_INTEGRATION_READY)
        self.assertFalse(flags.HOST_MUTATION)
        self.assertFalse(flags.MACCI_PROOF)
        self.assertFalse(flags.REAL_DEPLOY)
        self.assertFalse(flags.ACCEPTANCE_ENABLED)
        text = " ".join(flags.RESIDUALS)
        self.assertIn("w06", text)
        self.assertIn("two-platform", text)
        self.assertIn("joint", text)
        self.assertIn("macci", text)


if __name__ == "__main__":
    unittest.main()
