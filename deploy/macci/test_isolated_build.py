"""Isolated checks for the W03 phase-1 build constructor.

These tests run in a temporary directory. They do not read host credentials,
open a production database, or treat a passing result as a host or database ACL.
"""

import ast
import copy
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import isolated_build

HEAD = "a" * 40
BASE = "b" * 40
MERGE = "c" * 40
TREE = "d" * 40
TOKEN = "FIXTURE_HOST_TOKEN_9f3c"
MARKER = "FIXTURE_SCRIPT_MARKER_9f3c"
REPO = "layho/web-app-init-base-env"


def candidate(app_id="app_alpha"):
    return isolated_build.Candidate(REPO, "tsk_w03_isolated", app_id, HEAD, BASE, MERGE, TREE)


def actor(app_id="app_alpha", kind="test", proof="test-proof://w03/fixture"):
    return isolated_build.Actor(kind, "fixture", proof, app_id)


def artifacts(*items):
    if not items:
        items = (("readme.txt", b"alpha\n", 0o644),)
    return tuple(isolated_build.InputArtifact(path, content, mode) for path, content, mode in items)


def build_request(
    *,
    app_id="app_alpha",
    artifact_items=None,
    plan=None,
    script=None,
    allowlist=None,
    permissions=None,
    database_mode="denied",
    database_target="",
    env_values=(),
    run_id="run-w03-a",
    key="idem-w03-alpha",
    revision=1,
    kind="test",
    actor_app=None,
    production=False,
):
    artifact_items = artifacts() if artifact_items is None else artifact_items
    if plan is None:
        plan = (isolated_build.BuildStep("write_output", "output/build-sha.txt", b"ok\n"),)
    if permissions is None:
        permissions = isolated_build.PermissionProfile(database_mode=database_mode)
    return isolated_build.BuildRequest(
        actor=actor(app_id if actor_app is None else actor_app, kind=kind),
        candidate=candidate(app_id),
        artifacts=artifact_items,
        declared_digest=isolated_build.artifact_digest(artifact_items),
        env_allowlist=isolated_build.DEFAULT_ENV_ALLOWLIST if allowlist is None else allowlist,
        permissions=permissions,
        database_mode=database_mode,
        run_id=run_id,
        idempotency_key=key,
        expected_revision=revision,
        env_values=env_values,
        script_text=script,
        plan=plan,
        database_target=database_target,
        production_verification_requested=production,
    )


class IsolatedBuildTests(unittest.TestCase):
    def test_digest_and_candidate_id_follow_the_frozen_encoding(self):
        items = artifacts(("readme.txt", b"alpha\n", 0o644), ("notes/a.txt", b"beta", 0o755))
        digest = hashlib.sha256(b"alpha\n").hexdigest()
        other = hashlib.sha256(b"beta").hexdigest()
        manifest = f"0755 {other} notes/a.txt\n0644 {digest} readme.txt\n".encode()
        self.assertEqual("sha256:" + hashlib.sha256(manifest).hexdigest(), isolated_build.artifact_digest(items))
        changed = artifacts(("readme.txt", b"alpha\nX", 0o644))
        self.assertNotEqual(isolated_build.artifact_digest(items[:1]), isolated_build.artifact_digest(changed))
        text = "\n".join(("v1", REPO, "tsk_w03_isolated", HEAD, BASE, MERGE, TREE))
        self.assertEqual(text.split("\n"), ["v1", REPO, "tsk_w03_isolated", HEAD, BASE, MERGE, TREE])
        self.assertEqual(len(text.split("\n")), 7)
        self.assertFalse(text.endswith("\n"))
        self.assertIn("seven lines", isolated_build.candidate_id.__doc__)
        contract = Path(__file__).resolve().parents[2] / "docs/v060/02-architecture-contract.md"
        contract_text = contract.read_text(encoding="utf-8")
        self.assertIn("七行依次为", contract_text)
        self.assertNotIn("六行依次为", contract_text)
        self.assertEqual(
            "cand_" + hashlib.sha256(text.encode()).hexdigest(),
            isolated_build.candidate_id(candidate()),
        )

    def test_success_scrubs_host_token_and_uses_fixed_env(self):
        request = build_request()
        with tempfile.TemporaryDirectory(prefix="w03-build-") as folder:
            parent = Path(folder)
            env = isolated_build.build_environment(parent, isolated_build.DEFAULT_ENV_ALLOWLIST)
            self.assertEqual(env["PATH"], "/usr/bin:/bin")
            self.assertEqual(env["HOME"], str(parent))
            self.assertNotEqual(env["HOME"], str(Path.home()))
            self.assertNotIn("APP_GITHUB_TOKEN", env)
            calls = {"remover": 0}

            def remover(path):
                calls["remover"] += 1
                shutil.rmtree(path)

            receipt = isolated_build.construct(
                isolated_build.IsolationSession(),
                request,
                parent=parent,
                host_environ={
                    "APP_GITHUB_TOKEN": TOKEN,
                    "APP_GITHUB_PR_TOKEN": TOKEN,
                    "HOME": str(Path.home()),
                    "PATH": "/opt/host/bin",
                },
                remover=remover,
            )
            self.assertEqual(receipt["state"], "SUCCEEDED")
            self.assertEqual(receipt["commands_executed"], 0)
            self.assertEqual(receipt["env_path"], "/usr/bin:/bin")
            self.assertFalse(receipt["env_home_is_host_home"])
            self.assertNotIn("APP_GITHUB_TOKEN", receipt["env_names"])
            self.assertEqual(receipt["cleanup_state"], "removed")
            self.assertEqual(calls["remover"], 1)
            self.assertFalse(receipt["host_acl_effective"])
            self.assertFalse(receipt["db_acl_effective"])
            self.assertFalse(receipt["simulated_verification_is_host_acl_proof"])
            self.assertEqual(receipt["w02_alignment_status"], "pending_final_review")
            self.assertEqual(receipt["w02_baseline_head"], isolated_build.W02_BASELINE_HEAD)
            self.assertFalse(receipt["w02_bundled"])
            self.assertFalse(receipt["production_verification_enabled"])
            self.assertNotIn(TOKEN, json.dumps(receipt))
            self.assertEqual(sorted(path.name for path in parent.iterdir()), [".w03-claims"])

    def test_credential_allowlist_and_caller_values_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="w03-build-") as folder:
            parent = Path(folder)
            token_allow = build_request(allowlist=("PATH", "APP_GITHUB_TOKEN"), key="idem-w03-allow-token")
            receipt = isolated_build.construct(isolated_build.IsolationSession(), token_allow, parent=parent)
            self.assertEqual(receipt["code"], "POLICY_DENIED")
            self.assertEqual(receipt["detail_code"], "HOST_TOKEN_INHERITANCE_DENIED")
            self.assertEqual(receipt["idempotency"], "skipped")
            unknown = build_request(allowlist=("PATH", "COLORTERM"), key="idem-w03-allow-color")
            receipt = isolated_build.construct(isolated_build.IsolationSession(), unknown, parent=parent)
            self.assertEqual(receipt["detail_code"], "ENV_NOT_ALLOWLISTED")
            supplied = build_request(env_values=(("LANG", "en_US.UTF-8"),), key="idem-w03-env-value")
            receipt = isolated_build.construct(isolated_build.IsolationSession(), supplied, parent=parent)
            self.assertEqual(receipt["detail_code"], "ENV_VALUES_REJECTED")
            smuggled = build_request(
                env_values=(("APP_GITHUB_TOKEN", TOKEN),),
                key="idem-w03-env-token",
            )
            receipt = isolated_build.construct(isolated_build.IsolationSession(), smuggled, parent=parent)
            self.assertEqual(receipt["detail_code"], "HOST_TOKEN_INHERITANCE_DENIED")
            self.assertNotIn(TOKEN, json.dumps(receipt))
            embedded = build_request(
                artifact_items=artifacts(("readme.txt", TOKEN.encode(), 0o644)),
                key="idem-w03-embedded",
            )
            receipt = isolated_build.construct(
                isolated_build.IsolationSession(),
                embedded,
                parent=parent,
                host_environ={"APP_GITHUB_TOKEN": TOKEN},
            )
            self.assertEqual(receipt["detail_code"], "HOST_TOKEN_INHERITANCE_DENIED")
            self.assertNotIn(TOKEN, json.dumps(receipt))
            self.assertEqual(list(parent.iterdir()), [])

    def test_identity_failure_does_not_consume_idempotency(self):
        with tempfile.TemporaryDirectory(prefix="w03-build-") as folder:
            parent = Path(folder)
            session = isolated_build.IsolationSession()
            denied = build_request(kind="agent")
            receipt = isolated_build.construct(session, denied, parent=parent)
            self.assertEqual(receipt["code"], "NOT_AUTHORIZED")
            self.assertEqual(receipt["detail_code"], "IDENTITY_REJECTED")
            self.assertEqual(receipt["idempotency"], "skipped")
            self.assertEqual(receipt["commands_executed"], 0)
            accepted = isolated_build.construct(session, build_request(), parent=parent)
            self.assertEqual(accepted["state"], "SUCCEEDED")
            self.assertEqual(accepted["idempotency"], "stored")
            replay = isolated_build.construct(session, build_request(), parent=parent)
            self.assertEqual(replay["code"], "IDEMPOTENCY_REPLAY")
            self.assertEqual(replay["original_result"], "SUCCEEDED")
            self.assertEqual(replay["current_state"], "SUCCEEDED")
            self.assertNotIn("workspace_path", replay)

    def test_cross_app_actor_plan_and_mock_database_are_denied(self):
        with tempfile.TemporaryDirectory(prefix="w03-build-") as folder:
            parent = Path(folder)
            session = isolated_build.IsolationSession()
            crossed = build_request(actor_app="app_beta", key="idem-w03-cross-actor")
            receipt = isolated_build.construct(session, crossed, parent=parent)
            self.assertEqual(receipt["code"], "POLICY_DENIED")
            self.assertEqual(receipt["detail_code"], "CROSS_APP_DENIED")
            self.assertEqual(receipt["idempotency"], "skipped")
            plan = (
                isolated_build.BuildStep("write_output", "output/build-sha.txt", b"ok\n"),
                isolated_build.BuildStep("read_input", "inputs/readme.txt", target_app_id="app_beta"),
            )
            stepped = build_request(plan=plan, key="idem-w03-cross-step", run_id="run-w03-cross")
            receipt = isolated_build.construct(session, stepped, parent=parent)
            self.assertEqual(receipt["detail_code"], "CROSS_APP_DENIED")
            later = isolated_build.construct(
                session,
                build_request(database_mode="mock", key="idem-w03-mock", run_id="run-w03-mock"),
                parent=parent,
            )
            self.assertEqual(later["state"], "SUCCEEDED")
            self.assertEqual(later["database_probe"]["engine"], "mock")
            self.assertEqual(session.live_database_connects, 0)
            with self.assertRaises(isolated_build.IsolationError) as raised:
                session.mock_db.get("app_beta", "app_alpha", "probe")
            self.assertEqual(raised.exception.detail, "CROSS_APP_DENIED")
            self.assertEqual(session.mock_db.get("app_alpha", "app_alpha", "probe"), "app_alpha")

    def test_malicious_script_is_not_executed_on_the_host(self):
        with tempfile.TemporaryDirectory(prefix="w03-build-") as folder:
            parent = Path(folder)
            sentinel = parent / "sentinel-outside"
            sentinel.write_bytes(b"keep")
            before = sentinel.stat().st_ino
            leaked = build_request(
                script=f"echo {TOKEN}",
                key="idem-w03-script-token",
                run_id="run-w03-script-token",
            )
            receipt = isolated_build.construct(
                isolated_build.IsolationSession(),
                leaked,
                parent=parent,
                host_environ={"APP_GITHUB_TOKEN": TOKEN},
            )
            self.assertEqual(receipt["detail_code"], "HOST_TOKEN_INHERITANCE_DENIED")
            self.assertEqual(receipt["commands_executed"], 0)
            self.assertNotIn(TOKEN, json.dumps(receipt))
            hostile = f"cat {sentinel.as_posix()}; echo {MARKER}"
            receipt = isolated_build.construct(
                isolated_build.IsolationSession(),
                build_request(script=hostile, key="idem-w03-script-host", run_id="run-w03-script-host"),
                parent=parent,
                host_environ={"APP_GITHUB_TOKEN": TOKEN, "HOME": str(Path.home())},
            )
            self.assertEqual(sentinel.read_bytes(), b"keep")
            self.assertEqual(sentinel.stat().st_ino, before)
            self.assertNotIn(TOKEN, json.dumps(receipt))
            self.assertNotIn(MARKER, json.dumps(receipt))
            self.assertFalse(receipt["host_acl_effective"])
            if receipt["commands_executed"]:
                self.assertEqual(receipt["isolation_status"], "available")
                self.assertTrue(receipt["probe_evidence"]["external_file_inaccessible"])
                self.assertNotEqual(receipt["exit_code"], None)
            else:
                self.assertEqual(receipt["detail_code"], "ISOLATION_BLOCKED")
                self.assertIsNone(receipt["exit_code"])

    def test_workspace_escape_symlink_and_parent_guards(self):
        with tempfile.TemporaryDirectory(prefix="w03-build-") as folder:
            parent = Path(folder)
            workspace = parent / "ws"
            workspace.mkdir(mode=0o700)
            (workspace / "sub").mkdir()
            outside = parent / "secret"
            outside.write_bytes(b"keep")
            os.symlink(outside, workspace / "sub" / "link")
            with self.assertRaises(isolated_build.IsolationError) as raised:
                isolated_build.resolve_contained(workspace, "sub/link")
            self.assertEqual(raised.exception.detail, "WORKSPACE_ESCAPE_DENIED")
            self.assertEqual(outside.read_bytes(), b"keep")
            escaped = build_request(
                plan=(isolated_build.BuildStep("write_output", "output/../../secret", b"no\n"),),
                key="idem-w03-escape",
                run_id="run-w03-escape",
            )
            receipt = isolated_build.construct(isolated_build.IsolationSession(), escaped, parent=parent)
            self.assertEqual(receipt["detail_code"], "WORKSPACE_ESCAPE_DENIED")
            self.assertEqual(outside.read_bytes(), b"keep")
            hidden_items = artifacts((".env", b"APP_GITHUB_TOKEN=value\n", 0o644))
            hidden = isolated_build.BuildRequest(
                actor=actor(),
                candidate=candidate(),
                artifacts=hidden_items,
                declared_digest="sha256:" + ("11" * 32),
                env_allowlist=isolated_build.DEFAULT_ENV_ALLOWLIST,
                permissions=isolated_build.PermissionProfile(),
                database_mode="denied",
                run_id="run-w03-dotenv",
                idempotency_key="idem-w03-dotenv",
                plan=(isolated_build.BuildStep("write_output", "output/build-sha.txt", b"ok\n"),),
            )
            receipt = isolated_build.construct(isolated_build.IsolationSession(), hidden, parent=parent)
            self.assertEqual(receipt["detail_code"], "CREDENTIAL_ARTIFACT_DENIED")
            receipt = isolated_build.construct(
                isolated_build.IsolationSession(),
                build_request(key="idem-w03-repo", run_id="run-w03-repo"),
                parent=Path(__file__).resolve().parents[2],
            )
            self.assertEqual(receipt["detail_code"], "WORKSPACE_ESCAPE_DENIED")
            receipt = isolated_build.construct(
                isolated_build.IsolationSession(),
                build_request(key="idem-w03-home", run_id="run-w03-home"),
                parent=Path.home(),
            )
            self.assertEqual(receipt["detail_code"], "HOST_TOKEN_INHERITANCE_DENIED")
            self.assertFalse(outside.is_symlink())

    def test_database_is_denied_except_mock_or_temporary(self):
        production = "postgresql+psycopg://app_owner@/postgres?host=/private/tmp/label"
        with tempfile.TemporaryDirectory(prefix="w03-build-") as folder:
            parent = Path(folder)
            session = isolated_build.IsolationSession()
            denied = isolated_build.construct(
                session,
                build_request(database_target=production, key="idem-w03-pg", run_id="run-w03-pg"),
                parent=parent,
            )
            self.assertEqual(denied["code"], "PRODUCTION_DB_FORBIDDEN")
            self.assertEqual(denied["detail_code"], "DATABASE_DENIED")
            self.assertEqual(session.live_database_connects, 0)
            self.assertEqual(denied["idempotency"], "skipped")
            outside = parent / "not-created.sqlite3"
            other = isolated_build.construct(
                session,
                build_request(
                    database_target="sqlite:///" + outside.as_posix(),
                    key="idem-w03-sqlite-out",
                    run_id="run-w03-sqlite-out",
                ),
                parent=parent,
            )
            self.assertEqual(other["code"], "POLICY_DENIED")
            self.assertEqual(other["detail_code"], "DATABASE_DENIED")
            self.assertFalse(outside.exists())
            workspace = parent / "db-workspace"
            workspace.mkdir(mode=0o700)
            database = isolated_build.create_temporary_database(workspace, "app_alpha")
            self.assertTrue(str(database).startswith(str(workspace)))
            self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(database.parent.stat().st_mode), 0o700)
            temporary = isolated_build.construct(
                session,
                build_request(database_mode="temporary", key="idem-w03-tempdb", run_id="run-w03-tempdb"),
                parent=parent,
            )
            self.assertEqual(temporary["state"], "SUCCEEDED")
            self.assertEqual(temporary["database_probe"], {"engine": "sqlite", "contained": True, "same_app": True})
            self.assertNotIn(str(workspace), json.dumps(temporary))
            self.assertEqual(session.live_database_connects, 0)
            plain = isolated_build.construct(
                isolated_build.IsolationSession(),
                build_request(key="idem-w03-nodb", run_id="run-w03-nodb"),
                parent=parent,
            )
            self.assertEqual(plain["database_probe"]["engine"], "none")
            self.assertNotIn("DATABASE_URL", plain["env_names"])

    def test_same_app_concurrency_conflicts_and_other_apps_proceed(self):
        with tempfile.TemporaryDirectory(prefix="w03-build-") as folder:
            parent = Path(folder)
            paused = threading.Event()
            hold = threading.Event()
            winner_box = []

            def winner():
                winner_box.append(isolated_build.construct(
                    isolated_build.IsolationSession(),
                    build_request(run_id="run-w03-hold", key="idem-w03-hold"),
                    parent=parent,
                    paused=paused,
                    hold=hold,
                ))

            thread = threading.Thread(target=winner)
            thread.start()
            try:
                self.assertTrue(paused.wait(5))
                self.assertTrue((parent / ".reservation-app_alpha").exists())
                loser = isolated_build.construct(
                    isolated_build.IsolationSession(),
                    build_request(run_id="run-w03-wait", key="idem-w03-wait"),
                    parent=parent,
                )
            finally:
                hold.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(loser["code"], "RESERVATION_CONFLICT")
            self.assertEqual(loser["detail_code"], "CONCURRENCY_CONFLICT")
            self.assertEqual(loser["http"], 409)
            self.assertEqual(winner_box[0]["state"], "SUCCEEDED")
            self.assertEqual(sorted(path.name for path in parent.iterdir()), [".w03-claims"])

            barrier = threading.Barrier(2)
            boxes = {"app_alpha": [], "app_beta": []}

            labels = {"app_alpha": "alpha", "app_beta": "beta"}

            def run(app_id):
                barrier.wait(5)
                label = labels[app_id]
                boxes[app_id].append(isolated_build.construct(
                    isolated_build.IsolationSession(),
                    build_request(app_id=app_id, run_id=f"run-w03-{label}", key=f"idem-w03-{label}"),
                    parent=parent,
                ))

            threads = [threading.Thread(target=run, args=(app_id,)) for app_id in ("app_alpha", "app_beta")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive())
            self.assertEqual(boxes["app_alpha"][0]["state"], "SUCCEEDED")
            self.assertEqual(boxes["app_beta"][0]["state"], "SUCCEEDED")
            self.assertEqual(boxes["app_alpha"][0]["app_id"], "app_alpha")
            self.assertEqual(boxes["app_beta"][0]["app_id"], "app_beta")

    def test_cleanup_failure_is_a_receipt_and_releases_the_reservation(self):
        with tempfile.TemporaryDirectory(prefix="w03-build-") as folder:
            parent = Path(folder)
            session = isolated_build.IsolationSession()

            def fail_cleanup(path):
                raise OSError(errno.EIO, "simulated")

            receipt = isolated_build.construct(
                session,
                build_request(key="idem-w03-cleanup", run_id="run-w03-cleanup"),
                parent=parent,
                remover=fail_cleanup,
            )
            self.assertEqual(receipt["state"], "FAILED")
            self.assertEqual(receipt["code"], "SIDE_EFFECT_UNKNOWN")
            self.assertEqual(receipt["detail_code"], "CLEANUP_FAILED")
            self.assertEqual(receipt["cleanup_state"], "failed")
            self.assertFalse(receipt["host_acl_effective"])
            residue = Path(receipt["workspace_path"])
            self.assertTrue(residue.is_dir())
            replay = isolated_build.construct(
                session,
                build_request(key="idem-w03-cleanup", run_id="run-w03-cleanup"),
                parent=parent,
                remover=fail_cleanup,
            )
            self.assertEqual(replay["code"], "IDEMPOTENCY_REPLAY")
            self.assertEqual(replay["current_state"], "FAILED")
            self.assertNotIn("workspace_path", replay)
            shutil.rmtree(residue)

            def fail_again(path):
                raise OSError(errno.EBUSY, "simulated")

            plan = (
                isolated_build.BuildStep("write_output", "output/build-sha.txt", b"ok\n"),
                isolated_build.BuildStep("read_input", "inputs/readme.txt", target_app_id="app_beta"),
            )
            denied = isolated_build.construct(
                isolated_build.IsolationSession(),
                build_request(plan=plan, key="idem-w03-cleanup-deny", run_id="run-w03-cleanup-deny"),
                parent=parent,
                remover=fail_again,
            )
            self.assertEqual(denied["code"], "POLICY_DENIED")
            self.assertEqual(denied["detail_code"], "CROSS_APP_DENIED")
            self.assertEqual(denied["cleanup_state"], "failed")
            self.assertEqual(denied["cleanup_detail_code"], "CLEANUP_FAILED")
            shutil.rmtree(denied["workspace_path"])
            recovered = isolated_build.construct(
                isolated_build.IsolationSession(),
                build_request(key="idem-w03-cleanup-deny", run_id="run-w03-recovered"),
                parent=parent,
            )
            self.assertEqual(recovered["state"], "SUCCEEDED")

    def test_payload_mismatch_and_revision_do_not_replace_a_stored_result(self):
        with tempfile.TemporaryDirectory(prefix="w03-build-") as folder:
            parent = Path(folder)
            session = isolated_build.IsolationSession()
            calls = {"count": 0}

            def remover(path):
                calls["count"] += 1
                shutil.rmtree(path)

            first = build_request(key="idem-w03-payload")
            stored = isolated_build.construct(session, first, parent=parent, remover=remover)
            self.assertEqual(stored["state"], "SUCCEEDED")
            second_items = artifacts(("readme.txt", b"beta\n", 0o644))
            second = build_request(artifact_items=second_items, key="idem-w03-payload", run_id="run-w03-b")
            mismatch = isolated_build.construct(session, second, parent=parent, remover=remover)
            self.assertEqual(mismatch["code"], "IDEMPOTENCY_PAYLOAD_MISMATCH")
            self.assertEqual(mismatch["http"], 409)
            self.assertEqual(mismatch["original_result"], "SUCCEEDED")
            self.assertEqual(calls["count"], 1)
            replay = isolated_build.construct(session, first, parent=parent, remover=remover)
            self.assertEqual(replay["code"], "IDEMPOTENCY_REPLAY")
            self.assertEqual(calls["count"], 1)
            revised = isolated_build.construct(
                session,
                build_request(revision=2, key="idem-w03-revision", run_id="run-w03-rev"),
                parent=parent,
            )
            self.assertEqual(revised["code"], "CONFLICT_REVISION")
            self.assertEqual(revised["detail_code"], "REVISION_CONFLICT")
            self.assertEqual(revised["idempotency"], "skipped")

    def test_permissions_and_digest_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="w03-build-") as folder:
            parent = Path(folder)
            network = build_request(
                permissions=isolated_build.PermissionProfile(network=True),
                key="idem-w03-net",
                run_id="run-w03-net",
            )
            receipt = isolated_build.construct(isolated_build.IsolationSession(), network, parent=parent)
            self.assertEqual(receipt["detail_code"], "NETWORK_DENIED")
            lifecycle = build_request(
                permissions=isolated_build.PermissionProfile(lifecycle_scripts=True),
                key="idem-w03-life",
                run_id="run-w03-life",
            )
            receipt = isolated_build.construct(isolated_build.IsolationSession(), lifecycle, parent=parent)
            self.assertEqual(receipt["detail_code"], "LIFECYCLE_SCRIPT_DENIED")
            home = build_request(
                permissions=isolated_build.PermissionProfile(host_home_access=True),
                key="idem-w03-home-perm",
                run_id="run-w03-home-perm",
            )
            receipt = isolated_build.construct(isolated_build.IsolationSession(), home, parent=parent)
            self.assertEqual(receipt["detail_code"], "HOST_TOKEN_INHERITANCE_DENIED")
            bad_mode = build_request(
                artifact_items=artifacts(("readme.txt", b"alpha\n", 0o777)),
                key="idem-w03-mode",
                run_id="run-w03-mode",
            )
            receipt = isolated_build.construct(isolated_build.IsolationSession(), bad_mode, parent=parent)
            self.assertEqual(receipt["detail_code"], "PERMISSION_DENIED")
            items = artifacts()
            request = build_request()
            request = isolated_build.BuildRequest(
                actor=request.actor,
                candidate=request.candidate,
                artifacts=items,
                declared_digest="sha256:" + ("ab" * 32),
                env_allowlist=request.env_allowlist,
                permissions=request.permissions,
                database_mode=request.database_mode,
                run_id="run-w03-digest",
                idempotency_key="idem-w03-digest",
                plan=request.plan,
            )
            receipt = isolated_build.construct(isolated_build.IsolationSession(), request, parent=parent)
            self.assertEqual(receipt["code"], "EVIDENCE_MISSING")
            self.assertEqual(receipt["detail_code"], "DIGEST_MISMATCH")
            self.assertEqual(list(parent.iterdir()), [])

    def test_verification_report_does_not_claim_host_or_database_acls(self):
        report = isolated_build.run_isolation_verification()
        self.assertTrue(report["simulated"])
        self.assertFalse(report["runtime_measured"])
        self.assertFalse(report["host_acl_effective"])
        self.assertFalse(report["db_acl_effective"])
        self.assertFalse(report["simulated_verification_is_host_acl_proof"])
        self.assertFalse(report["simulated_verification_is_db_acl_proof"])
        self.assertEqual(report["exercises"]["failed"], 0)
        self.assertGreaterEqual(report["exercises"]["passed"], 4)
        self.assertEqual(report["baseline_sha"], isolated_build.BASELINE_SHA)
        self.assertFalse(report["w02_alignment"]["bundled"])
        self.assertEqual(report["w02_alignment"]["approved_isolated_baseline_head"], isolated_build.W02_BASELINE_HEAD)
        self.assertIn("app/control_identity", " ".join(report["w02_alignment"]["pending"]))
        for item, phrases in {
            "A04": ("launchd", "same uid"),
            "A05": ("trust", "production"),
            "A06": ("load_env", "ignore-scripts", "manage.sh"),
        }.items():
            evidence = report["real_machine_evidence_required"][item]
            self.assertGreaterEqual(len(evidence), 4)
            blob = " ".join(evidence).lower()
            for phrase in phrases:
                self.assertIn(phrase, blob)
            self.assertFalse(report["items"][item]["host_acl_effective"])
            self.assertFalse(report["items"][item]["db_acl_effective"])
            self.assertTrue(report["items"][item]["findings"])
        claimed = copy.deepcopy(isolated_build.BASELINE_OBSERVATIONS)
        claimed["A04"]["host_acl_effective"] = True
        claimed["A04"]["same_uid_directory_mode_is_isolation"] = True
        claimed["A05"]["db_acl_effective"] = True
        claimed["A06"]["leak_measured_on_host"] = True
        rejected = isolated_build.run_isolation_verification(claimed)
        self.assertFalse(rejected["host_acl_effective"])
        self.assertFalse(rejected["db_acl_effective"])
        for item in ("A04", "A05", "A06"):
            codes = [finding["code"] for finding in rejected["items"][item]["findings"]]
            self.assertIn("CLAIM_REJECTED", codes)

    def test_cli_verification_ignores_a_host_token_in_the_process_environment(self):
        script = Path(__file__).with_name("isolated_build.py")
        env = {
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "APP_GITHUB_TOKEN": TOKEN,
            "HOME": str(Path.home()),
        }
        completed = subprocess.run(
            [sys.executable, str(script), "--verify"],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn(TOKEN, completed.stdout)
        self.assertNotIn(TOKEN, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["host_acl_effective"])
        self.assertFalse(report["db_acl_effective"])
        self.assertEqual(report["exercises"]["failed"], 0)
        usage = subprocess.run(
            [sys.executable, str(script)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(usage.returncode, 2)

    def test_module_does_not_import_a_host_runner_or_database_driver(self):
        source = Path(__file__).with_name("isolated_build.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        module_imports = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                module_imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                module_imports.add(node.module.split(".")[0])
        self.assertTrue(module_imports.isdisjoint({
            "socket", "runner", "psycopg", "sqlalchemy", "manage",
        }))
        self.assertIn("subprocess", module_imports)
        self.assertIn("sqlite3", module_imports)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("def authorize_request", source)
        self.assertNotIn("def authorize(", source)

    def test_session_lookup_does_not_execute_or_write_the_store(self):
        with tempfile.TemporaryDirectory(prefix="w03-build-") as folder:
            parent = Path(folder)
            events = []
            session = isolated_build.IsolationSession()
            store = isolated_build.DurableStore(parent / ".w03-claims")
            session.lookup = self._trace(session.lookup, events, "lookup")
            store.begin = self._trace(store.begin, events, "begin")
            store.commit = self._trace(store.commit, events, "commit")
            first = isolated_build.construct(session, build_request(key="idem-w03-order"), parent=parent, store=store)
            self.assertEqual(first["state"], "SUCCEEDED")
            self.assertEqual(events, ["lookup", "begin", "commit"])
            events.clear()
            replay = isolated_build.construct(session, build_request(key="idem-w03-order"), parent=parent, store=store)
            self.assertEqual(replay["code"], "IDEMPOTENCY_REPLAY")
            self.assertEqual(events, ["lookup"])
            self.assertFalse(hasattr(session, "store"))

    def test_same_business_key_replays_for_a_different_run_and_process(self):
        with tempfile.TemporaryDirectory(prefix="w03-build-") as folder:
            parent = Path(folder)
            child = r"""
import sys
from pathlib import Path
import isolated_build
parent = Path(sys.argv[1])
artifacts = (isolated_build.InputArtifact("readme.txt", b"alpha\n", 0o644),)
request = isolated_build.BuildRequest(
    actor=isolated_build.Actor("test", "fixture", "test-proof://w03/fixture", "app_alpha"),
    candidate=isolated_build.Candidate(
        "layho/web-app-init-base-env", "tsk_w03_isolated", "app_alpha",
        "a" * 40, "b" * 40, "c" * 40, "d" * 40,
    ),
    artifacts=artifacts,
    declared_digest=isolated_build.artifact_digest(artifacts),
    env_allowlist=isolated_build.DEFAULT_ENV_ALLOWLIST,
    permissions=isolated_build.PermissionProfile(),
    database_mode="denied",
    run_id="run-w03-child",
    idempotency_key="idem-w03-cross-process",
    plan=(isolated_build.BuildStep("write_output", "output/build-sha.txt", b"ok\n"),),
)
receipt = isolated_build.construct(isolated_build.IsolationSession(), request, parent=parent)
if receipt["state"] != "SUCCEEDED":
    raise SystemExit(receipt.get("detail_code") or "failed")
"""
            completed = subprocess.run(
                [sys.executable, "-B", "-c", child, str(parent)],
                check=False,
                capture_output=True,
                text=True,
                cwd=Path(__file__).resolve().parent,
                env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(Path.home())},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            calls = {"count": 0}

            def remover(path):
                calls["count"] += 1
                shutil.rmtree(path)

            receipt = isolated_build.construct(
                isolated_build.IsolationSession(),
                build_request(key="idem-w03-cross-process", run_id="run-w03-parent"),
                parent=parent,
                remover=remover,
            )
            self.assertEqual(receipt["code"], "IDEMPOTENCY_REPLAY")
            self.assertEqual(receipt["commands_executed"], 0)
            self.assertEqual(calls["count"], 0)
            self.assertEqual(receipt["candidate_tuple"]["head"], HEAD)
            self.assertEqual(receipt["candidate_tuple"]["tree"], TREE)

    def test_unknown_result_is_durable_and_does_not_rerun(self):
        with tempfile.TemporaryDirectory(prefix="w03-build-") as folder:
            parent = Path(folder)
            sentinel = parent / "sentinel-unknown"
            sentinel.write_bytes(b"keep")
            request = build_request(
                key="idem-w03-unknown",
                run_id="run-w03-unknown",
                script=f"rm -f {sentinel.as_posix()}",
            )
            child = r"""
import sys
from pathlib import Path
import isolated_build
parent = Path(sys.argv[1])
artifacts = (isolated_build.InputArtifact("readme.txt", b"alpha\n", 0o644),)
request = isolated_build.BuildRequest(
    actor=isolated_build.Actor("test", "fixture", "test-proof://w03/fixture", "app_alpha"),
    candidate=isolated_build.Candidate(
        "layho/web-app-init-base-env", "tsk_w03_isolated", "app_alpha",
        "a" * 40, "b" * 40, "c" * 40, "d" * 40,
    ),
    artifacts=artifacts,
    declared_digest=isolated_build.artifact_digest(artifacts),
    env_allowlist=isolated_build.DEFAULT_ENV_ALLOWLIST,
    permissions=isolated_build.PermissionProfile(),
    database_mode="denied",
    run_id="run-w03-crashed",
    idempotency_key="idem-w03-unknown",
    script_text="rm -f " + sys.argv[2],
    plan=(isolated_build.BuildStep("write_output", "output/build-sha.txt", b"ok\n"),),
)
isolated_build.leave_unfinished_claim(parent, request)
"""
            completed = subprocess.run(
                [sys.executable, "-B", "-c", child, str(parent), str(sentinel)],
                check=False,
                capture_output=True,
                text=True,
                cwd=Path(__file__).resolve().parent,
                env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            receipt = isolated_build.construct(isolated_build.IsolationSession(), request, parent=parent)
            self.assertEqual(receipt["code"], "SIDE_EFFECT_UNKNOWN")
            self.assertEqual(receipt["detail_code"], "UNKNOWN_RESULT")
            self.assertEqual(receipt["idempotency"], "unknown")
            self.assertEqual(receipt["commands_executed"], 0)
            self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_concurrent_same_key_does_not_execute_twice(self):
        with tempfile.TemporaryDirectory(prefix="w03-build-") as folder:
            parent = Path(folder)
            paused = threading.Event()
            hold = threading.Event()
            winner_box = []

            def winner():
                winner_box.append(isolated_build.construct(
                    isolated_build.IsolationSession(),
                    build_request(run_id="run-w03-same-a", key="idem-w03-same"),
                    parent=parent,
                    paused=paused,
                    hold=hold,
                ))

            thread = threading.Thread(target=winner)
            thread.start()
            try:
                self.assertTrue(paused.wait(5))
                loser = isolated_build.construct(
                    isolated_build.IsolationSession(),
                    build_request(run_id="run-w03-same-b", key="idem-w03-same"),
                    parent=parent,
                )
            finally:
                hold.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(loser["code"], "RESERVATION_CONFLICT")
            self.assertEqual(loser["detail_code"], "CONCURRENCY_CONFLICT")
            self.assertEqual(loser["commands_executed"], 0)
            self.assertEqual(winner_box[0]["state"], "SUCCEEDED")
            replay = isolated_build.construct(
                isolated_build.IsolationSession(),
                build_request(run_id="run-w03-same-c", key="idem-w03-same"),
                parent=parent,
            )
            self.assertEqual(replay["code"], "IDEMPOTENCY_REPLAY")

    def test_cleanup_failure_is_durable_across_sessions(self):
        with tempfile.TemporaryDirectory(prefix="w03-build-") as folder:
            parent = Path(folder)

            def fail_cleanup(path):
                raise OSError(errno.EIO, "simulated")

            request = build_request(key="idem-w03-cleanup-disk", run_id="run-w03-cleanup-disk")
            receipt = isolated_build.construct(
                isolated_build.IsolationSession(),
                request,
                parent=parent,
                remover=fail_cleanup,
            )
            self.assertEqual(receipt["detail_code"], "CLEANUP_FAILED")
            record = isolated_build.DurableStore(parent / ".w03-claims").cleanup_record(
                isolated_build.business_key_for(request),
            )
            self.assertEqual(record["detail_code"], "CLEANUP_FAILED")
            self.assertEqual(record["error_class"], "EIO")
            self.assertNotIn(TOKEN, json.dumps(record))
            replay = isolated_build.construct(
                isolated_build.IsolationSession(),
                build_request(key="idem-w03-cleanup-disk", run_id="run-w03-cleanup-other"),
                parent=parent,
            )
            self.assertEqual(replay["code"], "IDEMPOTENCY_REPLAY")
            self.assertEqual(replay["current_state"], "FAILED")
            self.assertNotIn("workspace_path", replay)
            shutil.rmtree(receipt["workspace_path"])

    def test_open_uses_nofollow_on_the_exclusive_workspace(self):
        with tempfile.TemporaryDirectory(prefix="w03-build-") as folder:
            parent = Path(folder)
            workspace = parent / "ws"
            workspace.mkdir(mode=0o700)
            outside = parent / "secret"
            outside.write_bytes(b"keep")
            (workspace / "sub").mkdir()
            os.symlink(outside, workspace / "sub" / "link")
            with self.assertRaises(isolated_build.IsolationError) as raised:
                isolated_build.resolve_contained(workspace, "sub/link")
            self.assertEqual(raised.exception.detail, "WORKSPACE_ESCAPE_DENIED")
            self.assertEqual(outside.read_bytes(), b"keep")
            root = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                with self.assertRaises(OSError):
                    os.open("sub/link", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root)
            finally:
                os.close(root)
            self.assertIn("Same-UID", isolated_build.SAME_UID_THREAT_BOUNDARY)

    def test_execution_binding_covers_the_candidate_tuple_executor_and_probe(self):
        with tempfile.TemporaryDirectory(prefix="w03-build-") as folder:
            parent = Path(folder)
            receipt = isolated_build.construct(
                isolated_build.IsolationSession(),
                build_request(key="idem-w03-binding", run_id="run-w03-binding"),
                parent=parent,
            )
            binding = receipt["execution_binding"]
            self.assertEqual(binding["executor"], "Grok")
            self.assertEqual(binding["exit_code"], None)
            self.assertEqual(
                [binding["candidate_tuple"][name] for name in ("head", "base", "merge", "tree")],
                [HEAD, BASE, MERGE, TREE],
            )
            self.assertEqual(binding["output_digest"], receipt["output_digest"])
            self.assertEqual(binding["input_digest"], receipt["artifact_digest"])
            evidence = json.dumps(receipt["probe_evidence"], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            preimage = "\n".join((
                "w03-binding-v1",
                receipt["artifact_digest"],
                receipt["output_digest"],
                HEAD,
                BASE,
                MERGE,
                TREE,
                "Grok",
                "-",
                evidence,
            ))
            self.assertEqual(binding["digest"], "sha256:" + hashlib.sha256(preimage.encode()).hexdigest())
            other = isolated_build.bind_execution_digest(
                output_digest=receipt["output_digest"],
                input_digest=receipt["artifact_digest"],
                candidate=isolated_build.Candidate(REPO, "tsk_w03_isolated", "app_alpha", HEAD, BASE, MERGE, "e" * 40),
                executor="Grok",
                exit_code=None,
                probe_evidence=receipt["probe_evidence"],
            )
            self.assertNotEqual(other["digest"], binding["digest"])
            self.assertIn("authorize_request", " ".join(isolated_build.W02_ALIGNMENT_PENDING))
            self.assertIn("Same-UID", receipt["same_uid_threat_boundary"])
            self.assertFalse(receipt["host_acl_effective"])
            self.assertFalse(receipt["production_verification_enabled"])

    def test_blocked_adapter_does_not_execute_a_candidate(self):
        with tempfile.TemporaryDirectory(prefix="w03-build-") as folder:
            parent = Path(folder)
            sentinel = parent / "blocked-sentinel"
            sentinel.write_bytes(b"keep")

            def blocked():
                return isolated_build._blocked("test forced")

            def refuse_subprocess(*args, **kwargs):
                raise AssertionError("candidate subprocess")

            original = isolated_build.subprocess.run
            isolated_build.subprocess.run = refuse_subprocess
            try:
                receipt = isolated_build.construct(
                    isolated_build.IsolationSession(),
                    build_request(
                        script=f"rm -f {sentinel.as_posix()}",
                        key="idem-w03-blocked",
                        run_id="run-w03-blocked",
                    ),
                    parent=parent,
                    probe=blocked,
                )
            finally:
                isolated_build.subprocess.run = original
            self.assertEqual(receipt["detail_code"], "ISOLATION_BLOCKED")
            self.assertEqual(receipt["isolation_status"], "BLOCKED")
            self.assertEqual(receipt["commands_executed"], 0)
            self.assertIsNone(receipt["exit_code"])
            self.assertFalse(receipt["probe_evidence"]["candidate_executed"])
            self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_real_probe_counterexamples_or_explicit_block(self):
        worked = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--mount", "--net", "--pid", "--fork", "--kill-child", "true"],
            check=False,
            capture_output=True,
            env={"PATH": "/usr/bin:/bin"},
        ).returncode == 0
        previous = os.environ.get("APP_GITHUB_TOKEN")
        os.environ["APP_GITHUB_TOKEN"] = TOKEN
        try:
            capability = isolated_build.probe_isolation()
        finally:
            if previous is None:
                os.environ.pop("APP_GITHUB_TOKEN", None)
            else:
                os.environ["APP_GITHUB_TOKEN"] = previous
        self.assertFalse(capability.evidence["host_acl_effective"])
        self.assertFalse(capability.evidence["db_acl_effective"])
        self.assertEqual(capability.evidence["macci_evidence"], "pending")
        self.assertFalse(capability.evidence["candidate_executed"])
        self.assertIn("Same-UID", capability.evidence["same_uid_threat_boundary"])
        blob = json.dumps(capability.evidence)
        self.assertNotIn(TOKEN, blob)
        if worked:
            self.assertEqual(capability.status, "available", capability.reason)
            for key in (
                "home_inaccessible", "environment_inaccessible", "external_file_inaccessible",
                "network_inaccessible", "cross_app_inaccessible", "unprivileged",
            ):
                self.assertTrue(capability.evidence[key], key)
            self.assertEqual(capability.evidence["sandbox_loopback"], "ENETUNREACH")
            self.assertNotEqual(capability.evidence["host_loopback"], "ENETUNREACH")
        else:
            self.assertEqual(capability.status, "BLOCKED")
            self.assertFalse(capability.evidence["executed_probe"] and capability.status == "available")

    def test_sandbox_launch_on_the_host_is_refused(self):
        script = Path(__file__).with_name("isolated_build.py")
        completed = subprocess.run(
            [sys.executable, "-I", "-B", str(script), "--sandbox-launch"],
            check=False,
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "APP_GITHUB_TOKEN": TOKEN,
                "W03_MODE": "exec",
                "W03_WORKSPACE": "/tmp",
                "HOME": str(Path.home()),
            },
        )
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn(TOKEN, completed.stdout)
        self.assertNotIn(TOKEN, completed.stderr)
        self.assertTrue(Path("/etc/passwd").is_file())

    def _trace(self, fn, events, name):
        def wrapped(*args, **kwargs):
            events.append(name)
            return fn(*args, **kwargs)
        return wrapped


if __name__ == "__main__":
    unittest.main()
