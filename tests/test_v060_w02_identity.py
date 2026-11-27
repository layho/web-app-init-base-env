"""Isolated V0.6.0 W02 identity acceptance.

These tests run against the real Ed25519 verifier, the file-backed store, and
the isolated routes. They do not enable production verification.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from fastapi.testclient import TestClient

from app.control_identity.boundary import response_acceptable
from app.control_identity.branch_evidence import observe, summarize
from app.control_identity.canonical import DOMAIN, PROOF_FIELDS, candidate_id_for, canonical_bytes, parse_canonical, proof_message
from app.control_identity.errors import ControlError
from app.control_identity.service import ControlIdentityService
from app.control_identity.proof import sign_request
from app.control_routes_w02 import build_app

ROOT = Path(__file__).resolve().parents[1]
OBS = ROOT / "tests" / "fixtures" / "v060" / "w02" / "branch_protection_observation.json"
REPO = "layho/web-app-init-base-env"
TASK = "task-w02"
HEAD = "a" * 40
BASE = "b" * 40
MERGE = "c" * 40
TREE = "d" * 40
HEAD_B = "e" * 40
GATES = {"macci_passed": True, "same_repo": True, "queue_allowed": True, "no_conflict": True, "frozen": False}


class Clock:
    def __init__(self):
        self.t = 1_700_000_000

    def __call__(self):
        return self.t


class World:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = Clock()
        self.svc = ControlIdentityService(self.root, isolated=True, clock=self.clock)
        self.client = TestClient(build_app(self.svc, route_class="isolated"), base_url="http://isolated.example")
        self.keys = {}
        self.key_ids = {}
        self.private_raw = {}
        self.n = 0

    def register(self, principal_id, kind, *, app_id="", scopes=None):
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        key_id = principal_id + "-key"
        self.svc.register_principal(
            principal_id=principal_id,
            kind=kind,
            key_id=key_id,
            public_key_hex=public,
            test_identity=True,
            app_id=app_id,
            scopes=scopes or [],
        )
        self.keys[principal_id] = key
        self.key_ids[principal_id] = key_id
        self.private_raw[principal_id] = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
        return key

    def sign(self, principal_id, operation, **fields):
        self.n += 1
        body = {
            "repo_id": REPO,
            "task_id": TASK,
            "object_id": fields.pop("object_id", TASK),
            "policy_revision": fields.pop("policy_revision", 1),
            "credential_scope": fields.pop("credential_scope", ""),
            "credential_generation": fields.pop("credential_generation", 1),
            "idempotency_key": fields.pop("idempotency_key", f"idem-w02-{self.n}"),
        }
        body.update(fields)
        declared_id = body.pop("declared_id", principal_id)
        declared_kind = body.pop("declared_kind", "test")
        return sign_request(
            self.keys[principal_id],
            signer_id=principal_id,
            key_id=self.key_ids[principal_id],
            operation=operation,
            body=body,
            declared_kind=declared_kind,
            declared_id=declared_id,
            declared_proof_ref=f"test-proof://w02/{principal_id}",
            issued_at=self.clock.t,
            expires_at=self.clock.t + 500,
            nonce=body.pop("nonce", secrets.token_hex(16)),
        )

    def path(self, operation, body):
        task_id = body.get("task_id", TASK)
        run_id = body.get("run_id", "")
        return {
            "policy.evaluate": "/api/v1/control/policy:evaluate",
            "run.claim": f"/api/v1/control/tasks/{task_id}:claim",
            "run.renew": f"/api/v1/control/tasks/{task_id}:renew",
            "run.start": f"/api/v1/control/runs/{run_id}:start",
            "run.complete": f"/api/v1/control/runs/{run_id}:complete",
            "run.release": f"/api/v1/control/runs/{run_id}:release",
            "run.reconcile": f"/api/v1/control/runs/{run_id}:reconcile",
            "proxy.authorize": "/api/v1/control/identity/proxy:authorize",
            "proxy.rotate": "/api/v1/control/identity/proxy:rotate",
            "ci.status.write": "/api/v1/control/ci:status",
            "release.accept": "/api/v1/control/releases:accept",
            "release.publish": "/api/v1/control/releases:publish",
            "feedback.write": "/api/v1/control/feedback:write",
            "pull.read": "/api/v1/control/pull:read",
            "preview.merge": "/api/v1/control/preview:merge",
            "diagnostic.read": "/api/v1/control/diagnostics:read",
            "session.issue": "/api/v1/control/sessions:issue",
            "session.revoke": "/api/v1/control/sessions:revoke",
            "session.check": "/api/v1/control/sessions:check",
            "limit.consume": "/api/v1/control/limits:consume",
            "github.effect": "/api/v1/control/github-effects:submit",
            "host.privilege": "/api/v1/control/host:privilege",
            "signer.revoke": "/api/v1/control/identity/signers:revoke",
        }[operation]

    def post(self, operation, principal_id, **fields):
        body = self.sign(principal_id, operation, **fields)
        return self.client.post(self.path(operation, body), json=body), body

    def reopen(self):
        self.client.close()
        self.svc = ControlIdentityService(self.root, isolated=True, clock=self.clock)
        self.client = TestClient(build_app(self.svc, route_class="isolated"), base_url="http://isolated.example")

    def run(self, run_id):
        return self.svc.store.read()["runs"][run_id]


def standard_world():
    world = World()
    world.register("agent-1", "agent", scopes=["feedback"])
    world.register("agent-2", "agent", scopes=["feedback"])
    world.register("host-1", "host", scopes=["pull", "merge", "publish"])
    world.register("macci-1", "macci", scopes=["ci"])
    world.register("publisher-1", "publisher", scopes=["publish"])
    world.register("accept-1", "user_acceptance", scopes=["accept"])
    world.register("candidate-1", "candidate", scopes=["publish"])
    world.register("service-a", "app_service", app_id="app-a", scopes=["read"])
    world.register("intern-1", "intern")
    return world


class CanonicalTests(unittest.TestCase):
    def test_domain_separators_and_full_payload_hash(self):
        world = standard_world()
        response, body = world.post("feedback.write", "agent-1", credential_scope="feedback", credential_generation=1)
        self.assertEqual(response.status_code, 200, response.text)
        proof = body["proof"]
        self.assertEqual(len(proof["payload_hash"]), 64)
        message = proof_message({name: proof[name] for name in PROOF_FIELDS})
        parsed = parse_canonical(message)
        self.assertEqual(parsed[0], ("domain", DOMAIN))
        fields = dict(parsed)
        for name in PROOF_FIELDS:
            self.assertIn(name, fields)
        self.assertEqual(fields["payload_hash"], proof["payload_hash"])
        self.assertNotEqual(
            parse_canonical(canonical_bytes([("a", "bc")], domain=DOMAIN)),
            parse_canonical(canonical_bytes([("ab", "c")], domain=DOMAIN)),
        )
        other = proof_message({name: proof[name] for name in PROOF_FIELDS}, domain="other.domain")
        swapped = dict(body)
        swapped["proof"] = dict(proof)
        swapped["proof"]["signature"] = world.keys["agent-1"].sign(other).hex()
        rejected = world.client.post(world.path("feedback.write", swapped), json=swapped)
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(rejected.json()["reason"], "signature")


class ProofTests(unittest.TestCase):
    def test_tamper_expiry_revocation_replay_and_tuple(self):
        world = standard_world()
        response, body = world.post(
            "run.claim", "agent-1", run_id="run-proof", credential_scope="run", phase="develop"
        )
        self.assertEqual(response.status_code, 200, response.text)
        token = response.json()["lease_token"]
        self.assertTrue(token)
        self.assertNotIn(token, world.svc.store.text())
        self.assertNotIn(world.private_raw["agent-1"], world.svc.store.text())

        replay = world.client.post(world.path("run.claim", body), json=body)
        self.assertEqual(replay.status_code, 403)
        self.assertEqual(replay.json()["reason"], "replay")
        self.assertNotIn(token, replay.text)
        self.assertEqual(world.run("run-proof")["state"], "LEASED")

        again, _ = world.post(
            "run.claim", "agent-1", run_id="run-proof", credential_scope="run",
            phase="develop", idempotency_key=body["idempotency_key"],
        )
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["code"], "IDEMPOTENCY_REPLAY")
        self.assertIsNone(again.json()["lease_token"])
        self.assertNotIn(token, again.text)
        self.assertNotIn("lease_token", again.json()["original_result"])

        mismatched, _ = world.post(
            "run.claim", "agent-1", run_id="run-proof", credential_scope="run",
            phase="review", idempotency_key=body["idempotency_key"],
        )
        self.assertEqual(mismatched.status_code, 409)
        self.assertEqual(mismatched.json()["code"], "IDEMPOTENCY_PAYLOAD_MISMATCH")
        self.assertEqual(world.run("run-proof")["generation"], 1)

        for name in ("nonce", "payload_hash", "signature", "expires_at", "scope", "operation"):
            forged = json.loads(json.dumps(body))
            forged["proof"][name] = forged["proof"][name][:-1] + ("0" if forged["proof"][name][-1] != "0" else "1")
            outcome = world.client.post(world.path("run.claim", forged), json=forged)
            self.assertEqual(outcome.status_code, 403, name)
            self.assertIn(outcome.json()["code"], {"PROOF_INVALID", "PROOF_EXPIRED", "SCHEMA_UNSUPPORTED"})
            self.assertNotIn(token, outcome.text)

        shifted = json.loads(json.dumps(body))
        shifted["phase"] = "review"
        outcome = world.client.post(world.path("run.claim", shifted), json=shifted)
        self.assertEqual(outcome.json()["reason"], "payload_hash")

        fresh = world.sign("agent-1", "feedback.write", credential_scope="feedback", credential_generation=1)
        world.clock.t += 500
        expired = world.client.post(world.path("feedback.write", fresh), json=fresh)
        self.assertEqual(expired.json()["code"], "PROOF_EXPIRED")
        world.clock.t -= 500

        world.svc.revoke_signer("agent-1")
        revoked, _ = world.post("feedback.write", "agent-1", credential_scope="feedback", credential_generation=1)
        self.assertEqual(revoked.json()["code"], "PROOF_REVOKED")

        candidate = world.svc.register_candidate(repo=REPO, task=TASK, head=HEAD, base=BASE, merge=MERGE, tree=TREE)
        other = candidate_id_for(REPO, TASK, HEAD_B, BASE, MERGE, TREE)
        stale, _ = world.post(
            "ci.status.write", "macci-1", credential_scope="ci", candidate_id=other,
            head=HEAD_B, base=BASE, merge=MERGE, tree=TREE, object_id=other,
        )
        self.assertEqual(stale.json()["code"], "CANDIDATE_STALE")
        self.assertEqual(world.svc.store.read()["statuses"], {})
        recorded, _ = world.post(
            "ci.status.write", "macci-1", credential_scope="ci", candidate_id=candidate,
            head=HEAD, base=BASE, merge=MERGE, tree=TREE, object_id=candidate,
        )
        self.assertEqual(recorded.status_code, 200, recorded.text)
        self.assertEqual(recorded.json()["server_principal"]["kind"], "macci")


class ChannelTests(unittest.TestCase):
    def _candidate(self, world):
        return world.svc.register_candidate(repo=REPO, task=TASK, head=HEAD, base=BASE, merge=MERGE, tree=TREE)

    def test_channels_do_not_substitute_and_production_publish_stays_closed(self):
        world = standard_world()
        candidate = self._candidate(world)
        fields = dict(candidate_id=candidate, head=HEAD, base=BASE, merge=MERGE, tree=TREE, object_id=candidate, credential_scope="ci")
        denied, _ = world.post("release.accept", "macci-1", **fields)
        self.assertEqual(denied.json()["reason"], "role")
        denied, _ = world.post("ci.status.write", "accept-1", **{**fields, "credential_scope": "accept"})
        self.assertEqual(denied.json()["reason"], "role")
        denied, _ = world.post("release.accept", "publisher-1", **{**fields, "credential_scope": "publish"})
        self.assertEqual(denied.json()["reason"], "role")
        accepted, _ = world.post("release.accept", "accept-1", **{**fields, "credential_scope": "accept"})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(world.svc.store.read()["acceptances"][candidate]["signer_kind"], "user_acceptance")

        status_only = world.svc.store.read()
        self.assertNotIn(candidate, status_only["published"])
        publish, _ = world.post(
            "release.publish", "publisher-1", credential_scope="publish", credential_generation=1,
            candidate_id=candidate, head=HEAD, base=BASE, merge=MERGE, tree=TREE, object_id=candidate,
        )
        self.assertEqual(publish.status_code, 403)
        self.assertEqual(publish.json()["code"], "PRODUCTION_VERIFICATION_DISABLED")
        self.assertFalse(publish.json()["production_verification_enabled"])
        self.assertFalse(world.svc.production_verification_enabled)
        self.assertEqual(world.svc.store.read()["published"], [])
        self.assertFalse(any("release.publish" in key for key in world.svc.store.read()["idempotency"]))

        user_publish, _ = world.post(
            "release.publish", "accept-1", credential_scope="publish", credential_generation=1,
            candidate_id=candidate, head=HEAD, base=BASE, merge=MERGE, tree=TREE, object_id=candidate,
        )
        self.assertEqual(user_publish.json()["reason"], "role")
        lied = world.sign(
            "macci-1", "release.accept", declared_id="accept-1", credential_scope="accept",
            candidate_id=candidate, head=HEAD, base=BASE, merge=MERGE, tree=TREE, object_id=candidate,
        )
        mismatch = world.client.post(world.path("release.accept", lied), json=lied)
        self.assertEqual(mismatch.json()["reason"], "identity_mismatch")
        self.assertEqual(world.svc.store.read()["acceptances"][candidate]["signer_id"], "accept-1")

    def test_test_identity_is_rejected_on_real_routes(self):
        world = standard_world()
        body = world.sign("agent-1", "feedback.write", credential_scope="feedback", credential_generation=1)
        real = TestClient(build_app(world.svc, route_class="real"), base_url="http://isolated.example")
        rejected = real.post(world.path("feedback.write", body), json=body)
        self.assertEqual(rejected.json()["reason"], "test_identity_rejected")
        closed = ControlIdentityService(world.root, isolated=False, clock=world.clock)
        with self.assertRaises(ControlError) as raised:
            closed.register_principal(
                principal_id="other", kind="agent", key_id="other-key", public_key_hex="ab" * 32,
                test_identity=True,
            )
        self.assertEqual(raised.exception.reason, "production_registration_disabled")
        real_service = TestClient(build_app(closed, route_class="real"), base_url="http://isolated.example")
        rejected_again = real_service.post(world.path("feedback.write", body), json=body)
        self.assertEqual(rejected_again.json()["reason"], "test_identity_rejected")
        accepted = world.client.post(world.path("feedback.write", body), json=body)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        real.close()
        real_service.close()


class ProxyTests(unittest.TestCase):
    def test_rotation_cross_scope_and_no_secret_storage(self):
        sentinel = "github_pat_w02shouldnotbestored"
        os.environ["APP_GITHUB_TOKEN"] = sentinel
        os.environ["APP_GITHUB_PR_TOKEN"] = "ghp_w02shouldnotbestored"
        os.environ["APP_GITHUB_FEEDBACK_TOKEN"] = "github_pat_feedbackshouldnotbestored"
        world = standard_world()
        allowed, _ = world.post(
            "proxy.authorize", "agent-1", credential_scope="feedback", credential_generation=1,
            requested_operation="feedback.write",
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertTrue(allowed.json()["allow"])
        crossed, _ = world.post(
            "proxy.authorize", "agent-1", credential_scope="feedback", credential_generation=1,
            requested_operation="preview.merge",
        )
        self.assertEqual(crossed.json()["reason"], "cross_scope")
        before = dict(world.svc.store.read()["idempotency"])
        merge, _ = world.post(
            "preview.merge", "host-1", credential_scope="feedback", credential_generation=1, gates=GATES,
        )
        self.assertEqual(merge.json()["reason"], "cross_scope")
        self.assertEqual(world.svc.store.read()["idempotency"], before)
        self.assertEqual(world.svc.store.read()["merges"], [])

        good, _ = world.post(
            "preview.merge", "host-1", credential_scope="merge", credential_generation=1, gates=GATES,
        )
        self.assertEqual(good.status_code, 200, good.text)
        self.assertFalse(good.json()["production_enabled"])

        rotated, _ = world.post("proxy.rotate", "host-1", scope_to_rotate="feedback", credential_scope="merge")
        self.assertEqual(rotated.json()["generation"], 2)
        stale, _ = world.post("feedback.write", "agent-1", credential_scope="feedback", credential_generation=1)
        self.assertEqual(stale.json()["reason"], "stale_generation")
        current, _ = world.post("feedback.write", "agent-1", credential_scope="feedback", credential_generation=2)
        self.assertEqual(current.status_code, 200, current.text)
        pull, _ = world.post("pull.read", "host-1", credential_scope="pull", credential_generation=1)
        self.assertEqual(pull.status_code, 200, pull.text)
        world.post("proxy.rotate", "host-1", scope_to_rotate="pull", credential_scope="merge")
        swapped, _ = world.post("pull.read", "host-1", credential_scope="feedback", credential_generation=2)
        self.assertEqual(swapped.json()["reason"], "cross_scope")
        still_merge, _ = world.post(
            "preview.merge", "host-1", credential_scope="merge", credential_generation=1, gates=GATES,
            idempotency_key="idem-merge-again",
        )
        self.assertEqual(still_merge.status_code, 200, still_merge.text)

        text = world.svc.store.text()
        self.assertNotIn(sentinel, text)
        self.assertNotIn("ghp_w02shouldnotbestored", text)
        self.assertNotIn("BEGIN PRIVATE KEY", text)
        for principal, raw in world.private_raw.items():
            self.assertNotIn(raw, text, principal)
        proxy = world.svc.store.read()["proxy"]
        self.assertTrue(all(isinstance(value, int) for value in proxy.values()))
        package = ROOT / "app" / "control_identity"
        for path in package.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("load_env", source)
            self.assertNotIn("os.environ", source)
        routes = (ROOT / "app" / "control_routes_w02.py").read_text(encoding="utf-8")
        self.assertNotIn("load_env", routes)

        generations = dict(world.svc.store.read()["proxy"])
        scopes = list(world.svc.store.read()["principals"]["agent-1"]["scopes"])
        new_key = Ed25519PrivateKey.generate()
        public = new_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        world.svc.rotate_signer_key("agent-1", key_id="agent-1-key-2", public_key_hex=public)
        self.assertEqual(world.svc.store.read()["proxy"], generations)
        self.assertEqual(world.svc.store.read()["principals"]["agent-1"]["scopes"], scopes)
        old, _ = world.post("feedback.write", "agent-1", credential_scope="feedback", credential_generation=2)
        self.assertEqual(old.json()["reason"], "key_rotated")
        world.keys["agent-1"] = new_key
        world.key_ids["agent-1"] = "agent-1-key-2"
        renewed, _ = world.post("feedback.write", "agent-1", credential_scope="feedback", credential_generation=2)
        self.assertEqual(renewed.status_code, 200, renewed.text)


class LeaseTests(unittest.TestCase):
    def _lease(self, world, run_id="run-lease"):
        response, body = world.post("run.claim", "agent-1", run_id=run_id, credential_scope="run")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["lease_token"], body

    def test_stale_generation_does_not_change_the_newer_run(self):
        world = standard_world()
        token, _ = self._lease(world)
        renewed, _ = world.post(
            "run.renew", "agent-1", run_id="run-lease", credential_scope="run",
            lease_token=token, lease_generation=1,
        )
        self.assertEqual(renewed.status_code, 200, renewed.text)
        current = renewed.json()["lease_token"]
        self.assertNotEqual(current, token)
        self.assertEqual(renewed.json()["lease_generation"], 2)
        started, _ = world.post(
            "run.start", "agent-1", run_id="run-lease", credential_scope="run",
            lease_token=current, lease_generation=2,
        )
        self.assertEqual(started.json()["state"], "RUNNING")
        before = world.run("run-lease")
        stale, _ = world.post(
            "run.complete", "agent-1", run_id="run-lease", credential_scope="run",
            lease_token=token, lease_generation=1, result="SUCCEEDED",
        )
        self.assertEqual(stale.json()["code"], "LEASE_FENCE_REJECTED")
        self.assertFalse(stale.json()["changed"])
        self.assertEqual(stale.json()["state"], "RUNNING")
        after = world.run("run-lease")
        self.assertEqual(after["state"], before["state"])
        self.assertEqual(after["generation"], 2)
        self.assertEqual(after["token_hash"], before["token_hash"])
        self.assertNotEqual(after["state"], "UNKNOWN")
        done, _ = world.post(
            "run.complete", "agent-1", run_id="run-lease", credential_scope="run",
            lease_token=current, lease_generation=2, result="SUCCEEDED",
        )
        self.assertEqual(done.status_code, 200, done.text)
        bare = world.sign(
            "agent-1", "run.complete", run_id="run-other", credential_scope="run",
            lease_generation=1, result="SUCCEEDED",
        )
        world.post("run.claim", "agent-1", run_id="run-other", credential_scope="run")
        bare["lease_token"] = "1"
        bare["run_id"] = "run-other"
        rejected = world.client.post(world.path("run.complete", bare), json=bare)
        self.assertEqual(rejected.json()["code"], "LEASE_FENCE_REJECTED")

    def test_timeout_does_not_reclaim_or_mark_unknown_without_the_proxy(self):
        world = standard_world()
        token, _ = self._lease(world, "run-timeout")
        world.post("run.start", "agent-1", run_id="run-timeout", credential_scope="run", lease_token=token, lease_generation=1)
        world.clock.t += 60
        expired, _ = world.post(
            "run.complete", "agent-1", run_id="run-timeout", credential_scope="run",
            lease_token=token, lease_generation=1, result="SUCCEEDED",
        )
        self.assertEqual(expired.json()["code"], "LEASE_FENCE_REJECTED")
        self.assertEqual(expired.json()["reason"], "expired")
        self.assertEqual(world.run("run-timeout")["state"], "RUNNING")
        reclaim, _ = world.post(
            "run.claim", "agent-1", run_id="run-timeout", credential_scope="run", recovery="timeout",
        )
        self.assertEqual(reclaim.json()["code"], "RECLAIM_DENIED")
        self.assertEqual(world.run("run-timeout")["state"], "RUNNING")
        client, _ = world.post(
            "run.reconcile", "agent-1", run_id="run-timeout", credential_scope="run", actor_role="trusted_proxy",
        )
        self.assertEqual(client.json()["reason"], "client_cannot_fence")
        self.assertEqual(world.run("run-timeout")["state"], "RUNNING")
        with self.assertRaises(ControlError):
            world.svc.trusted_fence("run-timeout", fenced=False, effect_known=False)
        with self.assertRaises(ControlError):
            world.svc.trusted_fence("run-timeout", fenced=True, effect_known=True)
        self.assertEqual(world.run("run-timeout")["state"], "RUNNING")
        fenced = world.svc.trusted_fence("run-timeout", fenced=True, effect_known=False)
        self.assertEqual(fenced["state"], "UNKNOWN")
        again, _ = world.post("run.claim", "agent-1", run_id="run-timeout", credential_scope="run", recovery="timeout")
        self.assertEqual(again.json()["code"], "RECLAIM_DENIED")
        self.assertEqual(world.run("run-timeout")["state"], "UNKNOWN")
        with self.assertRaises(ControlError):
            world.svc.trusted_classify("run-timeout", fenced=False, classification="succeeded")
        self.assertEqual(world.run("run-timeout")["state"], "UNKNOWN")
        classified = world.svc.trusted_classify("run-timeout", fenced=True, classification="succeeded")
        self.assertEqual(classified["state"], "SUCCEEDED")
        fresh, _ = world.post("run.claim", "agent-1", run_id="run-timeout-2", credential_scope="run")
        self.assertEqual(fresh.status_code, 200, fresh.text)
        self.assertTrue(fresh.json()["lease_token"])

    def test_github_effects_are_serial_and_a_stale_generation_does_not_apply(self):
        world = standard_world()
        token, _ = self._lease(world, "run-effect")
        renewed, _ = world.post(
            "run.renew", "agent-1", run_id="run-effect", credential_scope="run",
            lease_token=token, lease_generation=1,
        )
        current = renewed.json()["lease_token"]
        world.post("run.start", "agent-1", run_id="run-effect", credential_scope="run", lease_token=current, lease_generation=2)
        stale, _ = world.post(
            "github.effect", "agent-1", run_id="run-effect", credential_scope="run",
            lease_token=token, lease_generation=1, effect_id="effect-old",
        )
        self.assertEqual(stale.json()["code"], "LEASE_FENCE_REJECTED")
        self.assertEqual(world.svc.store.read()["effects"], [])
        self.assertEqual(world.run("run-effect")["generation"], 2)
        world.svc.effect_hold = lambda: time.sleep(0.05)
        first = world.sign(
            "agent-1", "github.effect", run_id="run-effect", credential_scope="run",
            lease_token=current, lease_generation=2, effect_id="effect-a",
        )
        second = world.sign(
            "agent-1", "github.effect", run_id="run-effect", credential_scope="run",
            lease_token=current, lease_generation=2, effect_id="effect-b",
        )
        errors = []

        def submit(body):
            try:
                world.svc.execute("github.effect", body, route_class="isolated")
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=submit, args=(body,)) for body in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        parallel = sorted((span for span in world.svc.effects.spans if span[0] in {"effect-a", "effect-b"}), key=lambda item: item[1])
        self.assertEqual(len(parallel), 2)
        self.assertLessEqual(parallel[0][2], parallel[1][1] + 0.001)
        self.assertEqual({item["id"] for item in world.svc.store.read()["effects"]}, {"effect-a", "effect-b"})


class PolicyAndAuthzTests(unittest.TestCase):
    def test_candidate_publish_is_denied_without_idempotency_or_candidate_policy(self):
        world = standard_world()
        candidate_dir = world.root / "candidate-checkout"
        candidate_dir.mkdir()
        (candidate_dir / "policy.json").write_text(
            '{"high_risk_allow":["host.privilege"],"ordinary_allow":["release.publish"]}',
            encoding="utf-8",
        )
        before = world.svc.store.text()
        denied, _ = world.post(
            "policy.evaluate", "candidate-1", action="release.publish",
            credential_scope="publish", credential_generation=1, idempotency_key="idem-w02-policy-deny",
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["code"], "POLICY_DENIED")
        self.assertFalse(denied.json()["allow"])
        self.assertFalse(denied.json()["side_effect"])
        self.assertEqual(world.svc.store.text(), before)
        self.assertEqual(world.svc.store.read()["idempotency"], {})
        route, _ = world.post(
            "release.publish", "candidate-1", credential_scope="publish", credential_generation=1, action="release.publish",
        )
        self.assertEqual(route.json()["code"], "POLICY_DENIED")
        self.assertEqual(world.svc.store.read()["published"], [])
        self.assertFalse(any(key.endswith("|release.publish|idem-w02-policy-deny") or "|release.publish|" in key
                             for key in world.svc.store.read()["idempotency"]))

        privilege, _ = world.post("host.privilege", "host-1")
        self.assertEqual(privilege.json()["code"], "POLICY_DENIED")
        self.assertEqual(privilege.json()["scope_class"], "high_risk")
        self.assertEqual(world.svc.store.read()["policy"]["high_risk_allow"], [])
        allowed, _ = world.post("preview.merge", "host-1", credential_scope="merge", credential_generation=1, gates=GATES)
        self.assertEqual(allowed.status_code, 200, allowed.text)
        missing, _ = world.post("preview.merge", "host-1", credential_scope="merge", credential_generation=1)
        self.assertEqual(missing.json()["code"], "POLICY_DENIED")
        self.assertEqual(missing.json()["scope_class"], "ordinary")

    def test_default_deny_and_cross_app(self):
        world = standard_world()
        intern, _ = world.post("diagnostic.read", "intern-1", credential_scope="read", credential_generation=1, target_app_id="app-a")
        self.assertEqual(intern.json()["reason"], "role")
        shell_body = world.sign("agent-1", "shell.exec")
        with self.assertRaises(ControlError) as raised:
            world.svc.execute("shell.exec", shell_body, route_class="isolated")
        self.assertEqual(raised.exception.reason, "default_deny")
        cross, _ = world.post(
            "diagnostic.read", "service-a", credential_scope="read", credential_generation=1, target_app_id="app-b",
        )
        self.assertEqual(cross.json()["reason"], "cross_app")
        same, _ = world.post(
            "diagnostic.read", "service-a", credential_scope="read", credential_generation=1, target_app_id="app-a",
        )
        self.assertEqual(same.status_code, 200, same.text)
        self.assertEqual(same.json()["server_principal"]["kind"], "app_service")
        self.assertEqual(same.json()["declared"]["kind"], "test")


class DurableAndBoundaryTests(unittest.TestCase):
    def test_rate_limit_and_session_revocation_survive_restart(self):
        world = standard_world()
        for _ in range(3):
            response, _ = world.post("limit.consume", "agent-1", limit_key="login")
            self.assertEqual(response.status_code, 200, response.text)
        limited, _ = world.post("limit.consume", "agent-1", limit_key="login")
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json()["code"], "RATE_LIMITED")
        self.assertIn("script-src", limited.headers["content-security-policy"])
        world.reopen()
        still, _ = world.post("limit.consume", "agent-1", limit_key="login")
        self.assertEqual(still.status_code, 429)
        self.assertGreaterEqual(world.svc.store.read()["limits"]["login"]["count"], 4)

        issued, _ = world.post("session.issue", "agent-1")
        raw = issued.json()["session_id"]
        self.assertNotIn(raw, world.svc.store.text())
        revoked, _ = world.post("session.revoke", "host-1", session_id=raw)
        self.assertEqual(revoked.status_code, 200, revoked.text)
        world.reopen()
        checked, _ = world.post("session.check", "agent-1", session_id=raw)
        self.assertEqual(checked.status_code, 401)
        self.assertEqual(checked.json()["reason"], "session_revoked")
        self.assertIn("script-src", checked.headers["content-security-policy"])

    def test_script_src_hsts_and_error_redaction(self):
        world = standard_world()
        secret = "github_pat_supersecretvalue"
        body = {
            "schema_version": 2,
            "actor": {"kind": "test", "id": secret, "proof_ref": "test-proof://w02/secret"},
        }
        response = world.client.post("/api/v1/control/feedback:write", json=body)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(secret, response.text)
        self.assertNotIn("BEGIN PRIVATE KEY", response.text)
        self.assertIn("[redacted]", response.text)
        self.assertIn("script-src", response.headers["content-security-policy"])
        self.assertNotIn("strict-transport-security", response.headers)
        self.assertTrue(response_acceptable(
            response.headers["content-security-policy"], response.text, https=False, hsts=""
        ))
        self.assertFalse(response_acceptable("default-src 'self'", "ok", https=False, hsts=""))
        self.assertFalse(response_acceptable("script-src 'self'", "github_pat_example", https=False, hsts=""))

        https = TestClient(build_app(world.svc, route_class="isolated"), base_url="https://isolated.example")
        secure = https.get("/api/v1/control/branch-protection?overall=proven")
        self.assertEqual(secure.json()["overall"], "unproven")
        self.assertFalse(secure.json()["rules_modified"])
        self.assertIn("script-src", secure.headers["content-security-policy"])
        self.assertIn("max-age", secure.headers["strict-transport-security"])
        self.assertTrue(response_acceptable(
            secure.headers["content-security-policy"],
            secure.text,
            https=True,
            hsts=secure.headers["strict-transport-security"],
        ))
        https.close()


class BranchEvidenceTests(unittest.TestCase):
    def test_live_readonly_observation_stays_unproven(self):
        document = json.loads(OBS.read_text(encoding="utf-8"))
        observations = [
            observe(name=item["name"], method=item["method"], http_status=item["http_status"], body=item["body"])
            for item in document["requests"]
        ]
        summary = summarize(observations)
        self.assertEqual(summary["overall"], "unproven")
        self.assertFalse(summary["rules_modified"])
        self.assertEqual(summary["bypass"], "not_queried")
        self.assertIn("bypass", summary["missing"])
        reasons = {item["reason"] for item in summary["observations"]}
        self.assertIn("credential_cannot_read", reasons)
        self.assertIn("empty_ruleset_not_proof", reasons)
        self.assertIn("not_visible", reasons)
        self.assertTrue(all(item["classification"] == "unproven" for item in summary["observations"]))
        enabled = observe(name="main_protection", method="GET", http_status=200, body={"enabled": True})
        self.assertEqual(enabled["classification"], "unproven")
        with self.assertRaises(ControlError):
            observe(name="main_protection", method="PUT", http_status=200, body={})
        source = (ROOT / "app" / "control_identity" / "branch_evidence.py").read_text(encoding="utf-8")
        self.assertNotIn("api.github.com", source)
        self.assertNotIn("requests", source)
        self.assertNotIn(secret_marker(), json.dumps(document))


def secret_marker():
    return "github_pat_"


class ProductionBoundaryTests(unittest.TestCase):
    def test_existing_auth_and_main_are_not_this_package(self):
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        auth = (ROOT / "app" / "auth.py").read_text(encoding="utf-8")
        self.assertNotIn("control_routes_w02", main)
        self.assertNotIn("control_identity", main)
        self.assertIn("pbkdf2_sha256", auth)
        self.assertIn("csrf", auth.lower())
        self.assertIn("script-src", (ROOT / "app" / "control_identity" / "boundary.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
