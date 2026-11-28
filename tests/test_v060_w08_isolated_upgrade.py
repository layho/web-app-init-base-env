"""Isolated V0.6.0 W08 upgrade checks. These tests are not an upgrade.

They do not mount routes, merge, deploy, enroll a scheduler, or contact a
database. Port doubles are synthetic and are not runtime proof.
"""

import ast
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.control_upgrade import (
    CAPABILITIES,
    Grant,
    ServerClock,
    UpgradeService,
    candidate_id,
)
from app.control_upgrade.flags import (
    ACCEPTANCE_ENABLED,
    AUTO_RESOLVE_AMBIGUOUS,
    GITHUB_EXECUTION,
    JOINT_INTEGRATION_READY,
    MACCI_PROOF,
    MOUNTED_IN_APP_MAIN,
    OVERWRITE_APP_CONFIG,
    OVERWRITE_APP_DATA,
    OVERWRITE_CUSTOM_CODE,
    PRODUCTION_DB_ACCESS,
    PRODUCTION_ENABLEMENT,
    PRODUCTION_VERIFICATION_ENABLED,
    REAL_DEPLOY,
    REAL_MERGE,
    REAL_UPGRADE,
    ROUTE_MODULE,
)
from app.control_upgrade.ports import (
    FixtureBuild,
    FixtureDelivery,
    FixtureDeploy,
    FixtureDiagnostics,
    FixtureSchedule,
)

ROOT = Path(__file__).resolve().parents[1]
REPO = "layho/web-app-init-base-env"
TASK = "tsk_w08"
APP = "panel"
OTHER = "ledger"
A = "a" * 40
B = "b" * 40
C = "c" * 40
D = "d" * 40
E = "e" * 40
F = "f" * 40
G = "1" * 40
HEAD, BASE, MERGE, TREE = B, C, D, E
WHEN = datetime(2026, 11, 28, 3, 49, tzinfo=timezone.utc)
ACTOR = {"kind": "test", "id": "fixture", "proof_ref": "test-proof://w08/fixture"}
SCOPE = {"repo": REPO, "task_id": TASK, "app_id": APP}


def expected_candidate(head=HEAD, base=BASE, merge=MERGE, tree=TREE, repo=REPO, task=TASK):
    raw = "\n".join(["v1", repo, task, head, base, merge, tree]).encode("utf-8")
    return "cand_" + hashlib.sha256(raw).hexdigest()


CANDIDATE = expected_candidate()


def clock():
    return ServerClock(WHEN)


def grants(*actions, app_id=APP, principal="fixture"):
    return [Grant(principal, app_id, action) for action in actions]


def all_grants(app_id=APP):
    return grants("upgrade.read", "upgrade.plan", "upgrade.reconcile", "upgrade.recover", "upgrade.apply", app_id=app_id)


def candidate_body(head=HEAD, base=BASE, merge=MERGE, tree=TREE):
    return {"head": head, "base": base, "merge": merge, "tree": tree}


def build_report(**overrides):
    report = {
        "evidence_state": "present",
        "candidate_id": CANDIDATE,
        "exit_code": 0,
        "frontend_failed": False,
        "mergeable": True,
        "target_sha": A,
        "artifact_digest": "ab" * 32,
        "db_acl_effective": False,
        "host_acl_effective": False,
        "runtime_measured": False,
        "same_process_sdk": True,
        "modules_empty": True,
    }
    report.update(overrides)
    return report


def delivery_report(**overrides):
    report = {
        "evidence_state": "present",
        "candidate_id": CANDIDATE,
        "head": HEAD,
        "base": BASE,
        "merge": MERGE,
        "tree": TREE,
        "mergeable": True,
        "macci_state": "success",
        "macci_sha": D,
    }
    report.update(overrides)
    return report


def schedule_report(**overrides):
    report = {
        "evidence_state": "present",
        "present": True,
        "symlink": False,
        "owner_matches": True,
        "mode_exclusive": True,
        "sha": E,
        "registered": False,
        "mode": None,
    }
    report.update(overrides)
    return report


def diagnostics_report(**overrides):
    report = {"evidence_state": "missing", "running_sha": None, "panel_pr_state": None, "severity": None}
    report.update(overrides)
    return report


def deploy_report(**overrides):
    report = {
        "evidence_state": "present",
        "state": None,
        "effect": "not_occurred",
        "running_sha": F,
        "previous_healthy_sha": G,
        "launchd_label": "com.example.panel",
        "launchd_sha": "2" * 40,
        "install_kind": "upgrade",
    }
    report.update(overrides)
    return report


def foundation_change(path="app/foundation_version.py", old="v1\n", new="v2\n", local="v1\n", op="modify"):
    return {
        "path": path,
        "op": op,
        "owner": "foundation",
        "old": old,
        "new": new,
        "local": local,
        "symlink": False,
    }


def service(root=None, build=None, schedule=None, delivery=None, diagnostics=None, deploy=None, grant_rows=None, now=None):
    return UpgradeService(
        root,
        clock=clock() if now is None else now,
        build=FixtureBuild(build_report() if build is None else build),
        schedule=FixtureSchedule(schedule_report() if schedule is None else schedule),
        delivery=FixtureDelivery(delivery_report() if delivery is None else delivery),
        diagnostics=FixtureDiagnostics(diagnostics_report() if diagnostics is None else diagnostics),
        deploy=FixtureDeploy(deploy_report() if deploy is None else deploy),
        grants=all_grants() if grant_rows is None else grant_rows,
    )


def plan_request(**overrides):
    body = {
        "schema_version": 2,
        "actor": dict(ACTOR),
        "scope": dict(SCOPE),
        "app_id": APP,
        "expected_revision": 1,
        "idempotency_key": "idem-w08-plan",
        "target_sha": A,
        "candidate_id": CANDIDATE,
        "candidate": candidate_body(),
        "install_kind": "upgrade",
        "copy_mode": "allowlist",
        "foundation_allowlist": ["app/foundation_version.py"],
        "changes": [foundation_change()],
        "observed_files": [
            ".app-init.json",
            "app/project_info.json",
            "data/app.db",
            "frontend/src/app-modules/widget.tsx",
        ],
        "declared": {"schema": 1, "repository": REPO, "version": "0.6.0", "sha": C},
        "peers": [
            {"app_id": APP, "data_root": "/apps/panel/data", "table_prefix": "mod_panel__"},
            {"app_id": OTHER, "data_root": "/apps/ledger/data", "table_prefix": "mod_ledger__"},
        ],
    }
    body.update(overrides)
    return body


def read_request(**overrides):
    body = {
        "schema_version": 2,
        "actor": dict(ACTOR),
        "scope": dict(SCOPE),
        "app_id": APP,
        "declared": {"schema": 1, "repository": REPO, "version": "0.6.0", "sha": C},
        "candidate": candidate_body(),
        "occurred_at": "2099-01-01T00:00:00+00:00",
    }
    body.update(overrides)
    return body


class IsolatedUpgradeTests(unittest.TestCase):
    def test_capabilities_and_flags_stay_false(self):
        self.assertEqual(CAPABILITIES, ("U01", "U03", "U04", "U05", "U06", "U07", "U08"))
        self.assertFalse(MOUNTED_IN_APP_MAIN)
        self.assertFalse(PRODUCTION_ENABLEMENT)
        self.assertFalse(PRODUCTION_VERIFICATION_ENABLED)
        self.assertFalse(PRODUCTION_DB_ACCESS)
        self.assertFalse(REAL_MERGE)
        self.assertFalse(REAL_DEPLOY)
        self.assertFalse(REAL_UPGRADE)
        self.assertFalse(ACCEPTANCE_ENABLED)
        self.assertFalse(GITHUB_EXECUTION)
        self.assertFalse(MACCI_PROOF)
        self.assertFalse(JOINT_INTEGRATION_READY)
        self.assertFalse(AUTO_RESOLVE_AMBIGUOUS)
        self.assertFalse(OVERWRITE_APP_DATA)
        self.assertFalse(OVERWRITE_APP_CONFIG)
        self.assertFalse(OVERWRITE_CUSTOM_CODE)
        self.assertEqual(ROUTE_MODULE, "app.control_routes_w08")
        flags = service().flags()
        for key in (
            "mounted",
            "production_enabled",
            "production_verification_enabled",
            "production_db_access",
            "real_merge",
            "real_deploy",
            "real_upgrade",
            "acceptance_enabled",
            "upgrade_apply_enabled",
            "auto_resolve_ambiguous",
        ):
            self.assertFalse(flags[key])
        denied = service().enable_production()
        self.assertEqual(denied["http"], 403)
        self.assertEqual(denied["code"], "POLICY_DENIED")
        self.assertFalse(denied["production_enabled"])
        self.assertFalse(service().flags()["production_enabled"])

    def test_candidate_encoding_matches_w00(self):
        self.assertEqual(candidate_id(REPO, TASK, HEAD, BASE, MERGE, TREE), CANDIDATE)
        other = candidate_id(REPO, "tsk_other", HEAD, BASE, MERGE, TREE)
        self.assertNotEqual(other, CANDIDATE)
        self.assertTrue(CANDIDATE.startswith("cand_"))
        self.assertNotIn("\n", CANDIDATE)

    def test_package_does_not_import_live_upgrade_or_neighbor_trees(self):
        package = ROOT / "app" / "control_upgrade"
        modules = list(package.glob("*.py"))
        self.assertGreaterEqual(len(modules), 5)
        forbidden = {
            "app.main",
            "app.foundation_version",
            "app.module_sdk",
            "app.control_scheduler",
            "app.control_delivery",
            "app.control_diagnostics",
            "app.control_deploy",
            "app_init",
            "upgrade_plan",
            "upgrade_pr",
            "upgrade_scheduler",
            "upgrade_auto_merge",
            "upgrade_runtime_auto",
            "isolated_build",
            "ownership_preflight",
        }
        called = set()
        for path in modules:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertFalse(any(alias.name == name or alias.name.startswith(name + ".") for name in forbidden))
                elif isinstance(node, ast.ImportFrom):
                    name = node.module or ""
                    self.assertFalse(any(name == item or name.startswith(item + ".") for item in forbidden))
                elif isinstance(node, ast.Call):
                    func = node.func
                    label = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                    called.add(label)
        for name in ("urlopen", "Popen", "subprocess", "system"):
            self.assertNotIn(name, called)

    def test_shared_entry_points_are_not_edited(self):
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("control_upgrade", main)
        self.assertNotIn("control_routes_w08", main)
        self.assertFalse((ROOT / "app" / "control_routes_w08.py").exists())

    def test_u01_build_failure_is_not_compatibility(self):
        engine = service(build=build_report(frontend_failed=True, exit_code=1, mergeable=True))
        result = engine.plan(plan_request())
        self.assertEqual(result["http"], 200)
        self.assertEqual(result["state"], "BLOCKED")
        self.assertFalse(result["compatible"])
        self.assertFalse(result["mergeable_is_compatibility"])
        self.assertIn("build_failed_not_compatible", result["reasons"])
        self.assertEqual(result["write_set"], [])
        self.assertFalse(result["upgrade_executed"])
        self.assertFalse(result["applicable"])
        self.assertEqual(engine.deploy.calls, [])
        preview = engine.diagnose(plan_request(idempotency_key="idem-w08-diagnose"))
        self.assertEqual(preview["state"], "BLOCKED")
        self.assertFalse(preview["journal_recorded"])
        self.assertEqual(engine.list_plans(read_request()), {"app_id": APP, "plan_ids": [result["plan_id"]], "side_effect": False})

    def test_u01_missing_panel_observation_stays_unproven(self):
        report = service().inspect(read_request(paths=["app/foundation_version.py", "data/app.db"]))
        self.assertEqual(report["panel_pr_state"], "unproven")
        self.assertFalse(report["runtime_proof"])
        self.assertFalse(report["side_effect"])
        claimed = service(diagnostics=diagnostics_report(evidence_state="present", panel_pr_state="frontend-failed"))
        seen = claimed.inspect(read_request())
        self.assertEqual(seen["panel_pr_state"], "unproven")
        self.assertEqual(seen["diagnostic_claim"], "frontend-failed")
        self.assertFalse(seen["runtime_proof"])

    def test_u01_diagnostic_claim_cannot_override_build_failure(self):
        engine = service(
            build=build_report(frontend_failed=True, exit_code=1, mergeable=True),
            diagnostics=diagnostics_report(evidence_state="present", panel_pr_state="compatible", running_sha=A),
        )
        result = engine.plan(plan_request())
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("build_failed_not_compatible", result["reasons"])
        self.assertFalse(result["compatible"])

    def test_u03_scheduler_identification_does_not_enroll(self):
        identified = service().status(read_request())
        self.assertTrue(identified["components"]["scheduler"]["identified"])
        self.assertFalse(identified["components"]["scheduler"]["enrolled"])
        self.assertEqual(identified["components"]["scheduler"]["sha"], E)
        identified_engine = service()
        identified_engine.status(read_request())
        enrolled = identified_engine.enroll(plan_request(idempotency_key="idem-w08-enroll"))
        self.assertNotIn("register", [call["method"] for call in identified_engine.schedule.calls])
        missing = service(schedule=schedule_report(present=False, sha=None, owner_matches=False, mode_exclusive=False))
        self.assertFalse(missing.status(read_request())["components"]["scheduler"]["identified"])
        unsafe = service(schedule=schedule_report(symlink=True))
        self.assertFalse(unsafe.status(read_request())["components"]["scheduler"]["identified"])
        shared = service(schedule=schedule_report(mode_exclusive=False))
        self.assertFalse(shared.status(read_request())["components"]["scheduler"]["identified"])
        foreign = service(schedule=schedule_report(owner_matches=False))
        self.assertFalse(foreign.status(read_request())["components"]["scheduler"]["identified"])
        bad_pin = service()
        unknown = bad_pin.status(read_request(declared={"schema": 1, "repository": REPO, "version": "nope", "sha": C}))
        self.assertIsNone(unknown["components"]["repository"]["sha"])
        self.assertEqual(unknown["components"]["repository"]["state"], "unidentified")
        self.assertEqual(enrolled["code"], "POLICY_DENIED")
        self.assertEqual(enrolled["http"], 403)
        self.assertFalse(enrolled["enrolled"])
        self.assertEqual(enrolled["side_effect"], False)

    def test_u04_status_cache_is_persistent_and_does_not_fetch(self):
        with tempfile.TemporaryDirectory() as folder:
            now = clock()
            first = service(root=folder, now=now)
            seen = first.status(read_request(running_sha="deadbeef" * 5))
            self.assertEqual(seen["components"]["running"]["sha"], F)
            self.assertTrue(seen["caller_claim_ignored"])
            self.assertFalse(seen["fetched"])
            self.assertFalse(seen["network_under_lock"])
            self.assertEqual(seen["components"]["repository"]["sha"], C)
            self.assertNotEqual(seen["stored_at"], "2099-01-01T00:00:00+00:00")
            self.assertEqual((Path(folder) / "state.json").stat().st_mode & 0o077, 0)
            second = UpgradeService(
                folder,
                clock=now,
                build=FixtureBuild({"status": "BLOCKED", "evidence_state": "missing"}),
                schedule=FixtureSchedule({"status": "BLOCKED", "evidence_state": "missing"}),
                delivery=FixtureDelivery({"status": "BLOCKED", "evidence_state": "missing"}),
                diagnostics=FixtureDiagnostics({"status": "BLOCKED", "evidence_state": "missing"}),
                deploy=FixtureDeploy({"status": "BLOCKED", "evidence_state": "missing"}),
                grants=all_grants(),
            )
            cached = second.status(read_request(declared=None, occurred_at="1999-01-01T00:00:00+00:00"))
            self.assertEqual(cached["components"]["running"]["sha"], F)
            self.assertEqual(cached["components"]["scheduler"]["sha"], E)
            self.assertEqual(cached["components"]["repository"]["sha"], C)
            self.assertEqual(cached["components"]["running"]["source"], "cache")
            self.assertFalse(cached["components"]["running"]["stale"])
            self.assertFalse(cached["fetched"])
            now.advance(301)
            stale = second.status(read_request(declared=None))
            self.assertTrue(stale["components"]["running"]["stale"])
            self.assertEqual(stale["components"]["running"]["sha"], F)
            self.assertFalse(stale["fetched"])

    def test_u05_preserves_app_data_config_and_custom_code(self):
        changes = [
            foundation_change(),
            foundation_change(path="data/app.db", old=None, new="x", local=None),
            foundation_change(path=".app-init.json", old="{}", new="[]", local="{}"),
            foundation_change(path="frontend/src/app-modules/widget.tsx", old="a\n", new="b\n", local="a\n"),
            foundation_change(path="../secrets.env", old="a", new="b", local="a"),
            {"path": "app/main.py", "op": "rename", "owner": "foundation", "old": "a", "new": "b", "local": "a", "symlink": False, "app_id": APP},
            {"path": "app/link.py", "op": "modify", "owner": "foundation", "old": "a", "new": "b", "local": "a", "symlink": True, "app_id": APP},
        ]
        engine = service()
        result = engine.plan(plan_request(changes=changes, foundation_allowlist=["app/foundation_version.py", "app/main.py", "data/app.db"]))
        self.assertEqual(result["state"], "BLOCKED")
        self.assertEqual(result["write_set"], [])
        preserved = set(result["preserved"])
        self.assertIn("data/app.db", preserved)
        self.assertIn(".app-init.json", preserved)
        self.assertIn("frontend/src/app-modules/widget.tsx", preserved)
        self.assertIn("app/project_info.json", preserved)
        self.assertTrue(result["auto_resolved"] is False)
        reasons = set(result["reasons"])
        self.assertIn("app_data", reasons)
        self.assertIn("app_config", reasons)
        self.assertIn("app_custom", reasons)
        self.assertIn("path_rejected", reasons)
        self.assertIn("rename_or_symlink", reasons)
        blanket = engine.plan(plan_request(idempotency_key="idem-w08-blanket", copy_mode="blanket_frontend_src"))
        self.assertEqual(blanket["state"], "BLOCKED")
        self.assertIn("blanket_frontend_src_copy", blanket["reasons"])
        self.assertEqual(blanket["write_set"], [])
        broad = engine.plan(plan_request(idempotency_key="idem-w08-broad", foundation_allowlist=["frontend/src/"]))
        self.assertEqual(broad["state"], "BLOCKED")
        self.assertIn("blanket_ownership", broad["reasons"])

    def test_u05_case_collision_and_ambiguous_owner_are_not_overwritten(self):
        engine = service()
        changes = [
            foundation_change(path="app/main.py"),
            foundation_change(path="App/Main.py"),
        ]
        result = engine.plan(plan_request(changes=changes, foundation_allowlist=["app/main.py", "App/Main.py"], idempotency_key="idem-w08-case"))
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("case_collision", result["reasons"])
        self.assertEqual(result["write_set"], [])
        ambiguous = engine.plan(plan_request(
            idempotency_key="idem-w08-ambiguous-owner",
            changes=[{**foundation_change(path="app/main.py"), "owner": "app"}],
            foundation_allowlist=["app/main.py"],
        ))
        self.assertEqual(ambiguous["state"], "BLOCKED")
        self.assertIn("ambiguous_owner", ambiguous["reasons"])
        self.assertFalse(ambiguous["auto_resolved"])
        denied = engine.resolve(plan_request(idempotency_key="idem-w08-resolve", plan_id=ambiguous["plan_id"]))
        self.assertEqual(denied["code"], "POLICY_DENIED")
        self.assertEqual(engine.journal.get_plan(ambiguous["plan_id"])["state"], "BLOCKED")
        self.assertIn(ambiguous["plan_id"], engine.list_plans(read_request())["plan_ids"])

    def test_u06_apps_stay_isolated(self):
        collided = service().plan(plan_request(
            idempotency_key="idem-w08-collision",
            peers=[
                {"app_id": APP, "data_root": "/apps/shared/data", "table_prefix": "mod_panel__"},
                {"app_id": OTHER, "data_root": "/apps/shared/data", "table_prefix": "mod_ledger__"},
            ],
        ))
        self.assertEqual(collided["state"], "BLOCKED")
        self.assertIn("isolation_collision", collided["reasons"])
        self.assertFalse(collided["isolation_proven"])
        view = service(build=build_report(db_acl_effective=True, runtime_measured=True, modules_empty=True)).inspect(read_request())
        self.assertFalse(view["sandbox"])
        self.assertTrue(view["same_process_trusted_sdk"])
        self.assertFalse(view["modules_empty_is_acl_proof"])
        self.assertFalse(view["db_acl_effective"])
        self.assertFalse(view["runtime_proof"])
        self.assertFalse(view["isolation_proven"])
        reader = service(grant_rows=grants("upgrade.read"))
        denied = reader.inspect(read_request(app_id=OTHER, scope={"repo": REPO, "task_id": TASK, "app_id": OTHER}))
        self.assertEqual(denied["code"], "POLICY_DENIED")
        self.assertEqual(reader.diagnostics.calls, [])
        panel = service(grant_rows=all_grants(APP) + all_grants(OTHER))
        crossed = panel.plan(plan_request(
            idempotency_key="idem-w08-cross-app",
            changes=[{**foundation_change(), "app_id": OTHER}],
        ))
        self.assertEqual(crossed["state"], "BLOCKED")
        self.assertIn("isolation_cross_app", crossed["reasons"])
        panel.plan(plan_request())
        listed = panel.list_plans(read_request(app_id=OTHER, scope={"repo": REPO, "task_id": TASK, "app_id": OTHER}))
        self.assertEqual(listed["plan_ids"], [])

    def test_u07_ambiguous_conflict_is_not_resolved_and_merge_stays_off(self):
        old, new, local = "a\nb\nc\n", "a\nB\nc\n", "a\nX\nc\n"
        engine = service()
        result = engine.plan(plan_request(changes=[foundation_change(old=old, new=new, local=local)]))
        self.assertEqual(result["state"], "BLOCKED")
        self.assertEqual(result["decisions"][0]["classification"], "ambiguous")
        self.assertIsNone(result["decisions"][0]["result"])
        self.assertFalse(result["decisions"][0]["auto_resolved"])
        self.assertFalse(result["auto_resolved"])
        self.assertEqual(result["write_set"], [])
        self.assertEqual(engine.resolve(plan_request(plan_id=result["plan_id"], idempotency_key="idem-w08-resolve-2"))["code"], "POLICY_DENIED")
        self.assertEqual(engine.promote(plan_request(plan_id=result["plan_id"], idempotency_key="idem-w08-promote"))["code"], "POLICY_DENIED")
        self.assertFalse(engine.promote(plan_request(plan_id=result["plan_id"]))["real_merge"])
        self.assertEqual([call["method"] for call in engine.delivery.calls], ["read"])

    def test_u07_separable_text_is_not_applied(self):
        old, new, local = "a\nb\nc\n", "a\nB\nc\n", "A\nb\nc\n"
        result = service().plan(plan_request(changes=[foundation_change(old=old, new=new, local=local)]))
        self.assertEqual(result["state"], "PLANNED")
        self.assertEqual(result["decisions"][0]["classification"], "text_separable")
        self.assertEqual(result["decisions"][0]["action"], "review")
        self.assertIsNone(result["decisions"][0]["result"])
        self.assertFalse(result["decisions"][0]["applied"])
        self.assertFalse(result["applicable"])
        self.assertEqual(result["write_set"], [])
        self.assertIn("data/app.db", result["preserved"])
        self.assertIn("frontend/src/app-modules/widget.tsx", result["preserved"])

    def test_u07_macci_failure_blocks_even_when_mergeable(self):
        result = service(delivery=delivery_report(macci_state="failed", mergeable=True)).plan(plan_request())
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("macci_not_success", result["reasons"])
        self.assertFalse(result["mergeable_is_compatibility"])
        self.assertFalse(result["real_merge"])

    def test_u07_migration_and_host_script_are_not_executed(self):
        migration = service().plan(plan_request(
            idempotency_key="idem-w08-sql",
            foundation_allowlist=["migrations/004_app.sql"],
            changes=[foundation_change(path="migrations/004_app.sql", old="select 1;\n", new="select 2;\n", local="select 1;\n")],
        ))
        self.assertEqual(migration["state"], "BLOCKED")
        self.assertIn("migration_requires_review", migration["reasons"])
        self.assertEqual(migration["write_set"], [])
        self.assertFalse(migration["production_db_access"])
        host = service().plan(plan_request(
            idempotency_key="idem-w08-host",
            foundation_allowlist=["deploy/macmini/manage.sh"],
            changes=[foundation_change(path="deploy/macmini/manage.sh")],
        ))
        self.assertIsNone(host["decisions"][0]["result"])
        self.assertFalse(host["decisions"][0]["auto_resolved"])
        self.assertEqual(host["write_set"], [])
        self.assertFalse(host["applicable"])

    def test_u08_component_shas_stay_separate(self):
        seen = service().status(read_request())
        components = seen["components"]
        self.assertEqual(components["repository"]["sha"], C)
        self.assertEqual(components["running"]["sha"], F)
        self.assertEqual(components["scheduler"]["sha"], E)
        self.assertEqual(components["macci"]["sha"], D)
        self.assertIsNone(components["hostops"]["sha"])
        self.assertEqual(components["hostops"]["state"], "unknown")
        self.assertEqual(components["launchd"]["running_sha"], "2" * 40)
        self.assertFalse(components["running"]["matches_repository"])
        self.assertFalse(seen["collapsed"])
        self.assertNotIn("current", seen)
        self.assertFalse(seen["runtime_proof"])
        self.assertNotEqual(
            {components["repository"]["sha"], components["running"]["sha"], components["scheduler"]["sha"], components["macci"]["sha"]},
            {C},
        )

    def test_read_only_checks_do_not_call_actions(self):
        engine = service()
        engine.inspect(read_request(paths=["app/foundation_version.py"]))
        engine.status(read_request())
        engine.diagnose(plan_request())
        planned = engine.plan(plan_request())
        engine.reconcile(plan_request(idempotency_key="idem-w08-reconcile", plan_id=planned["plan_id"], expected_revision=1))
        for port in (engine.build, engine.schedule, engine.delivery, engine.diagnostics, engine.deploy):
            self.assertTrue(port.calls)
            self.assertTrue(all(call["method"] == "read" for call in port.calls))

    def test_actions_stay_disabled_without_idempotency_or_state_change(self):
        engine = service()
        planned = engine.plan(plan_request())
        before = engine.journal.idem_reads
        for method in (engine.apply, engine.enroll, engine.resolve, engine.promote, engine.overwrite):
            result = method(plan_request(plan_id=planned["plan_id"], idempotency_key="idem-w08-action"))
            self.assertEqual(result["http"], 403)
            self.assertEqual(result["code"], "POLICY_DENIED")
            self.assertFalse(result["upgrade_executed"])
            self.assertFalse(result["applicable"])
            self.assertEqual(result["write_set"], [])
        self.assertEqual(engine.journal.idem_reads, before)
        self.assertEqual(engine.journal.get_plan(planned["plan_id"])["state"], "PLANNED")
        self.assertEqual(len(engine.journal.plans_for(APP)), 1)

    def test_identity_runs_before_idempotency(self):
        engine = service(grant_rows=grants("upgrade.read"))
        bad = engine.plan(plan_request(actor={"kind": "user_acceptance", "id": "owner", "proof_ref": "test-proof://w08/fixture"}))
        self.assertEqual(bad["code"], "NOT_AUTHORIZED")
        self.assertEqual(engine.journal.idem_reads, 0)
        denied = engine.plan(plan_request())
        self.assertEqual(denied["code"], "POLICY_DENIED")
        self.assertEqual(engine.journal.idem_reads, 0)
        stale_schema = engine.plan(plan_request(schema_version=1))
        self.assertEqual(stale_schema["code"], "SCHEMA_UNSUPPORTED")
        self.assertEqual(engine.journal.idem_reads, 0)
        stamped = engine.plan(plan_request(received_at="2026-11-28T00:00:00+00:00"))
        self.assertEqual(stamped["code"], "SCHEMA_UNSUPPORTED")
        self.assertEqual(engine.journal.idem_reads, 0)
        leaked = engine.plan(plan_request(changes=[foundation_change(new="github_pat_secretvalue\n")]))
        self.assertEqual(leaked["code"], "SCHEMA_UNSUPPORTED")
        self.assertNotIn("github_pat_secretvalue", leaked["message"])
        self.assertEqual(engine.journal.idem_reads, 0)
        self.assertEqual(engine.journal.plans_for(APP), [])
        allowed = service()
        database = allowed.plan(plan_request(production_database=True))
        self.assertEqual(database["code"], "PRODUCTION_DB_FORBIDDEN")
        self.assertEqual(database["http"], 403)
        self.assertEqual(allowed.journal.idem_reads, 0)
        self.assertEqual(allowed.journal.plans_for(APP), [])

    def test_reentry_replays_original_plan_and_rejects_payload_mismatch(self):
        engine = service()
        first = engine.plan(plan_request())
        second = engine.plan(plan_request())
        self.assertEqual(second["code"], "IDEMPOTENCY_REPLAY")
        self.assertEqual(second["http"], 200)
        self.assertTrue(second["replayed"])
        self.assertIsNone(second["lease_token"])
        self.assertEqual(second["original_result"]["state"], "PLANNED")
        self.assertEqual(second["current_state"], "PLANNED")
        self.assertEqual(second["plan_id"], first["plan_id"])
        self.assertEqual(len(engine.journal.plans_for(APP)), 1)
        mismatch = engine.plan(plan_request(target_sha=B, changes=[foundation_change(new="other\n")]))
        self.assertEqual(mismatch["code"], "IDEMPOTENCY_PAYLOAD_MISMATCH")
        self.assertEqual(mismatch["http"], 409)
        self.assertEqual(engine.journal.get_plan(first["plan_id"])["state"], "PLANNED")
        self.assertEqual(len(engine.journal.plans_for(APP)), 1)
        other = service(grant_rows=all_grants(APP) + all_grants(OTHER))
        other.plan(plan_request())
        ledger = other.plan(plan_request(app_id=OTHER, scope={"repo": REPO, "task_id": TASK, "app_id": OTHER}))
        self.assertEqual(ledger["state"], "PLANNED")
        self.assertNotEqual(ledger["plan_id"], other.journal.plans_for(APP)[0]["plan_id"])

    def test_version_mismatch_does_not_reuse_evidence(self):
        stale = service().plan(plan_request(candidate_id="cand_not_the_tuple"))
        self.assertEqual(stale["code"], "CANDIDATE_STALE")
        self.assertEqual(stale["http"], 409)
        self.assertNotIn("plan_id", stale)
        mismatched = service(delivery=delivery_report(head=A)).plan(plan_request(idempotency_key="idem-w08-tuple"))
        self.assertEqual(mismatched["code"], "CANDIDATE_STALE")
        foreign = service(build=build_report(candidate_id="cand_other")).plan(plan_request(idempotency_key="idem-w08-build"))
        self.assertEqual(foreign["code"], "CANDIDATE_STALE")
        version = service(build=build_report(target_sha=B)).plan(plan_request(idempotency_key="idem-w08-target"))
        self.assertEqual(version["state"], "BLOCKED")
        self.assertIn("version_mismatch", version["reasons"])
        self.assertFalse(version["evidence_reused"])
        self.assertEqual(version["write_set"], [])

    def test_failure_recovery_never_commits_or_invents_a_sha(self):
        engine = service(deploy=deploy_report(effect="unknown", state="RECONCILE", previous_healthy_sha=None))
        planned = engine.plan(plan_request())
        self.assertEqual(planned["state"], "PLANNED")
        reconciled = engine.reconcile(plan_request(idempotency_key="idem-w08-reconcile", plan_id=planned["plan_id"], expected_revision=1))
        self.assertEqual(reconciled["http"], 200)
        self.assertEqual(reconciled["state"], "RECONCILE")
        self.assertNotEqual(reconciled["state"], "COMMIT")
        self.assertFalse(reconciled["upgrade_executed"])
        self.assertEqual(engine.journal.get_plan(planned["plan_id"])["revision"], 2)
        replay = engine.plan(plan_request())
        self.assertEqual(replay["code"], "IDEMPOTENCY_REPLAY")
        self.assertEqual(replay["original_result"]["state"], "PLANNED")
        self.assertEqual(replay["current_state"], "RECONCILE")
        self.assertEqual(len(engine.journal.plans_for(APP)), 1)
        wrong = engine.reconcile(plan_request(idempotency_key="idem-w08-reconcile-2", plan_id=planned["plan_id"], expected_revision=1))
        self.assertEqual(wrong["code"], "CONFLICT_REVISION")
        self.assertEqual(engine.journal.get_plan(planned["plan_id"])["revision"], 2)
        recovered = engine.recover(plan_request(
            idempotency_key="idem-w08-recover",
            plan_id=planned["plan_id"],
            expected_revision=2,
            previous_healthy_sha=B,
        ))
        self.assertEqual(recovered["state"], "BLOCKED")
        self.assertIsNone(recovered["previous_healthy_sha"])
        self.assertFalse(recovered["invented_previous_healthy_sha"])
        self.assertTrue(recovered["caller_sha_ignored"])
        self.assertNotEqual(recovered["plan_id"], planned["plan_id"])
        self.assertEqual(recovered["recovers_plan_id"], planned["plan_id"])
        self.assertEqual(recovered["write_set"], [])
        self.assertFalse(recovered["applicable"])
        self.assertEqual(engine.journal.get_plan(planned["plan_id"])["state"], "RECONCILE")
        self.assertTrue(all(call["method"] == "read" for call in engine.deploy.calls))
        denied = engine.apply(plan_request(plan_id=recovered["plan_id"], idempotency_key="idem-w08-apply"))
        self.assertEqual(denied["code"], "POLICY_DENIED")
        self.assertNotEqual(engine.journal.get_plan(recovered["plan_id"])["state"], "COMMIT")

    def test_known_previous_sha_is_recorded_without_rollback(self):
        engine = service(deploy=deploy_report(effect="failed", state="FAILED", previous_healthy_sha=G))
        planned = engine.plan(plan_request())
        engine.reconcile(plan_request(idempotency_key="idem-w08-reconcile", plan_id=planned["plan_id"], expected_revision=1))
        self.assertEqual(engine.journal.get_plan(planned["plan_id"])["state"], "FAILED")
        recovered = engine.recover(plan_request(
            idempotency_key="idem-w08-recover",
            plan_id=planned["plan_id"],
            expected_revision=2,
            previous_healthy_sha=B,
        ))
        self.assertEqual(recovered["previous_healthy_sha"], G)
        self.assertTrue(recovered["caller_sha_ignored"])
        self.assertFalse(recovered["runtime_proof"])
        self.assertFalse(recovered["upgrade_executed"])
        self.assertEqual(recovered["write_set"], [])
        self.assertTrue(all(call["method"] == "read" for call in engine.deploy.calls))

    def test_not_occurred_and_frozen_have_no_commit_exit(self):
        quiet = service(deploy=deploy_report(effect="not_occurred", state=None))
        planned = quiet.plan(plan_request())
        stayed = quiet.reconcile(plan_request(idempotency_key="idem-w08-reconcile", plan_id=planned["plan_id"], expected_revision=1))
        self.assertEqual(stayed["state"], "PLANNED")
        self.assertNotEqual(stayed["state"], "COMMIT")
        frozen = service(deploy=deploy_report(effect="unknown", state="SAFE_FROZEN"))
        planned = frozen.plan(plan_request())
        seen = frozen.reconcile(plan_request(idempotency_key="idem-w08-reconcile", plan_id=planned["plan_id"], expected_revision=1))
        self.assertEqual(seen["state"], "SAFE_FROZEN")
        before = frozen.journal.idem_reads
        recovered = frozen.recover(plan_request(idempotency_key="idem-w08-recover", plan_id=planned["plan_id"], expected_revision=2))
        self.assertEqual(recovered["code"], "POLICY_DENIED")
        self.assertEqual(recovered["reason"], "safe_frozen_no_exit")
        self.assertEqual(frozen.journal.get_plan(planned["plan_id"])["state"], "SAFE_FROZEN")
        self.assertEqual(frozen.journal.idem_reads, before)
        self.assertEqual(len(frozen.journal.plans_for(APP)), 1)

    def test_first_install_rejects_invented_previous_sha(self):
        result = service().plan(plan_request(install_kind="first_install", previous_healthy_sha=G, idempotency_key="idem-w08-first"))
        self.assertEqual(result["code"], "EVIDENCE_MISSING")
        self.assertEqual(result["http"], 409)
        self.assertEqual(service().journal.plans_for(APP), [])

    def test_clean_plan_matches_contract_shape_and_stays_inapplicable(self):
        result = service().plan(plan_request())
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["http"], 200)
        self.assertEqual(result["state"], "PLANNED")
        self.assertFalse(result["side_effect"])
        self.assertFalse(result["upgrade_executed"])
        self.assertFalse(result["applicable"])
        self.assertFalse(result["runtime_proof"])
        self.assertFalse(result["production_enabled"])
        self.assertFalse(result["mounted"])
        self.assertEqual(result["write_set"], [])
        self.assertIn("data/app.db", result["preserved"])
        self.assertTrue(result["journal_recorded"])
        self.assertEqual(result["candidate_id"], CANDIDATE)
        engine = service(deploy=deploy_report(effect="unknown", state="RECONCILE"))
        created = engine.plan(plan_request(idempotency_key="idem-w08-clean"))
        outcome = engine.reconcile(plan_request(idempotency_key="idem-w08-reconcile", plan_id=created["plan_id"], expected_revision=1))
        self.assertEqual(outcome["schema_version"], 2)
        self.assertEqual(outcome["http"], 200)
        self.assertEqual(outcome["state"], "RECONCILE")
        self.assertFalse(outcome["side_effect"])
        self.assertNotEqual(result["plan_id"], created["plan_id"])

    def test_reentry_after_block_uses_a_new_plan_id(self):
        engine = service(build=build_report(frontend_failed=True, exit_code=1, mergeable=True))
        blocked = engine.plan(plan_request())
        self.assertEqual(blocked["state"], "BLOCKED")
        engine.build = FixtureBuild(build_report())
        recovered = engine.recover(plan_request(idempotency_key="idem-w08-recover", plan_id=blocked["plan_id"], expected_revision=1))
        self.assertEqual(recovered["state"], "PLANNED")
        self.assertNotEqual(recovered["plan_id"], blocked["plan_id"])
        self.assertEqual(recovered["recovers_plan_id"], blocked["plan_id"])
        self.assertFalse(recovered["applicable"])
        self.assertEqual(engine.journal.get_plan(blocked["plan_id"])["state"], "BLOCKED")
        self.assertEqual(engine.apply(plan_request(plan_id=recovered["plan_id"]))["code"], "POLICY_DENIED")

    def test_blocked_dependency_does_not_store_a_plan(self):
        engine = service(
            build={"status": "BLOCKED", "evidence_state": "missing"},
            delivery={"status": "BLOCKED", "evidence_state": "missing"},
        )
        result = engine.plan(plan_request())
        self.assertEqual(result["code"], "DEPENDENCY_UNSATISFIED")
        self.assertEqual(result["http"], 409)
        self.assertEqual(engine.journal.plans_for(APP), [])

    def test_port_success_is_not_a_commit(self):
        engine = service(deploy=deploy_report(effect="ok", state="COMMIT"))
        planned = engine.plan(plan_request())
        outcome = engine.reconcile(plan_request(idempotency_key="idem-w08-reconcile", plan_id=planned["plan_id"], expected_revision=1))
        self.assertEqual(outcome["state"], "RECONCILE")
        self.assertFalse(outcome["accepted_commit"])
        self.assertTrue(outcome["port_claims_commit"])
        self.assertFalse(outcome["runtime_proof"])
        for plan in engine.journal.plans_for(APP):
            self.assertNotEqual(plan["state"], "COMMIT")


if __name__ == "__main__":
    unittest.main()
