"""Isolated V0.6.0 W02 identity acceptance.

These tests run against the real Ed25519 verifier, the file-backed store, and
the isolated routes. They do not enable production verification.
"""

from __future__ import annotations

import base64
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

from app.control_authz import (
    MATRIX,
    REQUIRED_SCOPES,
    SOURCE_BY_KIND,
    AuthzError,
    AuthzService,
    Principal,
    PrincipalRegistry,
    ResourceBinding,
    authorize,
    trusted_source_for,
)
from app.control_authz.canonical import (
    DOMAIN,
    PAYLOAD_DOMAIN,
    PROOF_DOMAIN,
    PROOF_FIELDS,
    UNIT,
    candidate_id_for,
    canonical_bytes,
    canonical_query,
    idempotency_digest,
    parse_canonical,
    proof_message,
)
from app.control_authz.service import READ_REPLAY_POLICY, WRITE_REPLAY_POLICY
from app.control_identity.boundary import response_acceptable
from app.control_identity.branch_evidence import observe, summarize
from app.control_identity.effects import DurableEffectProxy
from app.control_identity.errors import ControlError
from app.control_identity.lease import assert_lease_scope, phase_subject
from app.control_identity.service import ControlIdentityService
from app.control_identity.proof import sign_request
from app.control_authz.routes import route_path_for
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

    def register(self, principal_id, kind, *, app_id="", scopes=None, admin=False, task_ids=None):
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
            repo_id=REPO,
            producer_id=principal_id,
            stream_id=f"{principal_id}:task",
            task_ids=task_ids or [TASK],
            admin=admin,
        )
        self.keys[principal_id] = key
        self.key_ids[principal_id] = key_id
        self.private_raw[principal_id] = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
        return key

    def sign(self, principal_id, operation, **fields):
        self.n += 1
        nonce = fields.pop("nonce", secrets.token_hex(16))
        declared_id = fields.pop("declared_id", principal_id)
        declared_kind = fields.pop("declared_kind", "test")
        repo = fields.pop("repo_id", REPO)
        task = fields.pop("task_id", TASK)
        object_id = fields.pop("object_id", task)
        phase = fields.pop("phase", "")
        credential_scope = fields.pop("credential_scope", "")
        app_id = fields.pop("app_id", "")
        body = {
            "operation": operation,
            "authorizer": fields.pop("authorizer", principal_id),
            "repo_id": repo,
            "task_id": task,
            "app_id": app_id,
            "target_app_id": fields.pop("target_app_id", ""),
            "producer_id": fields.pop("producer_id", ""),
            "stream_id": fields.pop("stream_id", ""),
            "object_id": object_id,
            "phase": phase,
            "action": fields.pop("action", ""),
            "result": fields.pop("result", ""),
            "candidate_id": fields.pop("candidate_id", ""),
            "head": fields.pop("head", ""),
            "base": fields.pop("base", ""),
            "merge": fields.pop("merge", ""),
            "tree": fields.pop("tree", ""),
            "policy_revision": fields.pop("policy_revision", 1),
            "credential_scope": credential_scope,
            "credential_generation": fields.pop("credential_generation", 1),
            "idempotency_key": fields.pop("idempotency_key", f"idem-w02-{self.n}"),
            "expected_revision": fields.pop("expected_revision", 0),
            "run_id": fields.pop("run_id", ""),
            "lease_generation": fields.pop("lease_generation", 0),
            "lease_token": fields.pop("lease_token", ""),
            "session_id": fields.pop("session_id", ""),
            "effect_id": fields.pop("effect_id", ""),
            "requested_operation": fields.pop("requested_operation", ""),
            "scope_to_rotate": fields.pop("scope_to_rotate", ""),
            "target_signer": fields.pop("target_signer", ""),
            "recovery": fields.pop("recovery", ""),
            "release_version": fields.pop("release_version", "0.6.0" if operation.startswith("release.") else ""),
            "method": fields.pop("method", "GET" if operation == "branch.read" else "POST"),
            "query": fields.pop("query", ""),
            "scope": {
                "repo": repo,
                "task_id": task,
                "app_id": app_id,
                "credential_scope": credential_scope,
                "phase": phase,
                "object_id": object_id,
            },
        }
        body["path"] = fields.pop("path", route_path_for(operation, body))
        body.update(fields)
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
            nonce=nonce,
        )

    def path(self, operation, body):
        return route_path_for(operation, body)

    def inject_gates(self, **overrides):
        candidate = overrides.pop("candidate_id", None)
        head = overrides.pop("head", HEAD)
        base = overrides.pop("base", BASE)
        merge = overrides.pop("merge", MERGE)
        tree = overrides.pop("tree", TREE)
        repo = overrides.pop("repo_id", REPO)
        task = overrides.pop("task_id", TASK)
        if candidate is None:
            candidate = self.svc.register_candidate(repo=repo, task=task, head=head, base=base, merge=merge, tree=tree)
        evidence = {
            "macci_passed": True,
            "same_repo": True,
            "queue_allowed": True,
            "no_conflict": True,
            "frozen": False,
            "candidate_exact": True,
            "test_passed": True,
            "repo_id": repo,
            "task_id": task,
            "candidate_id": candidate,
            "head": head,
            "base": base,
            "merge": merge,
            "tree": tree,
            "not_before": self.clock.t,
            "not_after": self.clock.t + 500,
        }
        evidence.update(overrides)
        self._evidence = evidence
        self.svc.inject_gate_evidence(evidence)
        return candidate

    def evidence_fields(self):
        evidence = self._evidence
        return {
            "candidate_id": evidence["candidate_id"],
            "head": evidence["head"],
            "base": evidence["base"],
            "merge": evidence["merge"],
            "tree": evidence["tree"],
            "repo_id": evidence["repo_id"],
            "task_id": evidence["task_id"],
        }

    def _principal_binding(self, principal_id):
        record = self.svc.store.read()["principals"][principal_id]
        return {
            "app_id": record["app_id"],
            "producer_id": record["producer_id"],
            "stream_id": record["stream_id"],
        }

    def lease_fields(self, run_id):
        run = self.svc.store.read()["runs"][run_id]
        return {
            "phase": run["phase"],
            "candidate_id": run["candidate_id"],
            "head": run["head"],
            "base": run["base"],
            "merge": run["merge"],
            "tree": run["tree"],
            "app_id": run["app_id"],
            "producer_id": run["producer_id"],
            "stream_id": run["stream_id"],
        }

    def _fill_claim(self, principal_id, fields):
        for key, value in self._principal_binding(principal_id).items():
            fields.setdefault(key, value)
        fields.setdefault("phase", "develop")
        if "candidate_id" not in fields:
            repo = fields.get("repo_id", REPO)
            task = fields.get("task_id", TASK)
            head = fields.get("head", HEAD)
            base = fields.get("base", BASE)
            merge = fields.get("merge", MERGE)
            tree = fields.get("tree", TREE)
            fields["candidate_id"] = self.svc.register_candidate(
                repo=repo, task=task, head=head, base=base, merge=merge, tree=tree,
            )
            fields.setdefault("head", head)
            fields.setdefault("base", base)
            fields.setdefault("merge", merge)
            fields.setdefault("tree", tree)

    def _fill_follow(self, fields):
        run = self.svc.store.read()["runs"].get(fields.get("run_id") or "")
        if not run:
            return
        for key in ("phase", "candidate_id", "head", "base", "merge", "tree", "app_id", "producer_id", "stream_id"):
            fields.setdefault(key, run.get(key) or "")

    def post(self, operation, principal_id, **fields):
        if operation == "run.claim":
            self._fill_claim(principal_id, fields)
        elif operation in {"run.renew", "run.start", "run.complete", "run.release", "run.heartbeat", "github.effect"} and fields.get("run_id"):
            self._fill_follow(fields)
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
        message_fields = {}
        for name in PROOF_FIELDS:
            value = proof[name]
            message_fields[name] = str(value)
        message = proof_message(message_fields)
        parsed = parse_canonical(message)
        self.assertEqual(parsed[0], ("version", "v1"))
        self.assertEqual(parsed[1], ("domain", DOMAIN))
        self.assertEqual(DOMAIN, PROOF_DOMAIN)
        fields = dict(parsed)
        for name in PROOF_FIELDS:
            self.assertIn(name, fields)
        self.assertEqual(fields["payload_hash"], proof["payload_hash"])
        self.assertIn("target_signer", body)
        self.assertIn("lease_token", body)
        self.assertIsInstance(body["scope"], dict)
        vector = canonical_bytes(
            [("payload_hash", "ab" * 32), ("signer_id", "signer-w00"), ("key_id", "key-w00"), ("issued_at", "1700000000")],
            domain=PROOF_DOMAIN,
        )
        expected = UNIT.join(["v1", PROOF_DOMAIN, "payload_hash", "ab" * 32, "signer_id", "signer-w00", "key_id", "key-w00", "issued_at", "1700000000"]).encode()
        self.assertEqual(vector, expected)
        self.assertNotEqual(
            parse_canonical(canonical_bytes([("a", "bc")], domain=DOMAIN)),
            parse_canonical(canonical_bytes([("ab", "c")], domain=DOMAIN)),
        )
        self.assertNotEqual(
            idempotency_digest(principal_id="a|b", kind="agent", repo_id="c", app_id="", producer_id="p", stream_id="s", task_id="t", credential_scope="", phase="", object_id="t", operation="op", idempotency_key="k", target_app_id=""),
            idempotency_digest(principal_id="a", kind="agent", repo_id="b|c", app_id="", producer_id="p", stream_id="s", task_id="t", credential_scope="", phase="", object_id="t", operation="op", idempotency_key="k", target_app_id=""),
        )
        self.assertEqual("|".join(["a|b", "c", "op", "k"]), "|".join(["a", "b|c", "op", "k"]))
        other = proof_message(message_fields, domain="other.domain")
        swapped = json.loads(json.dumps(body))
        swapped["proof"]["signature"] = world.keys["agent-1"].sign(other).hex()
        rejected = world.client.post(world.path("feedback.write", swapped), json=swapped)
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(rejected.json()["reason"], "signature")
        self.assertNotEqual(PAYLOAD_DOMAIN, PROOF_DOMAIN)
        from app.control_authz.canonical import payload_values
        secret_body = world.sign(
            "agent-1", "run.complete", credential_scope="run", run_id="run_example",
            lease_token="lease-secret-token-value", lease_generation=1, expected_revision=1, result="SUCCEEDED",
        )
        covered = canonical_bytes(payload_values("run.complete", secret_body), domain=PAYLOAD_DOMAIN)
        self.assertNotIn(b"lease-secret-token-value", covered)
        self.assertIn(b"lease_token", covered)


class ProofTests(unittest.TestCase):
    def test_tamper_expiry_revocation_replay_and_tuple(self):
        world = standard_world()
        response, body = world.post(
            "run.claim", "agent-1", credential_scope="run", phase="develop"
        )
        self.assertEqual(response.status_code, 200, response.text)
        token = response.json()["lease_token"]
        run_id = response.json()["run_id"]
        self.assertTrue(token)
        self.assertTrue(run_id.startswith("run_"))
        self.assertNotIn(token, world.svc.store.text())
        self.assertNotIn(world.private_raw["agent-1"], world.svc.store.text())

        replay = world.client.post(world.path("run.claim", body), json=body)
        self.assertEqual(replay.status_code, 403)
        self.assertEqual(replay.json()["reason"], "replay")
        self.assertNotIn(token, replay.text)
        self.assertEqual(world.run(run_id)["state"], "LEASED")

        again, _ = world.post(
            "run.claim", "agent-1", credential_scope="run",
            phase="develop", idempotency_key=body["idempotency_key"],
        )
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["code"], "IDEMPOTENCY_REPLAY")
        self.assertIsNone(again.json()["lease_token"])
        self.assertNotIn(token, again.text)
        self.assertNotIn("lease_token", again.json()["original_result"])

        mismatched, _ = world.post(
            "run.claim", "agent-1", credential_scope="run",
            phase="develop", action="other", idempotency_key=body["idempotency_key"],
        )
        self.assertEqual(mismatched.status_code, 409)
        self.assertEqual(mismatched.json()["code"], "IDEMPOTENCY_PAYLOAD_MISMATCH")
        self.assertEqual(world.run(run_id)["generation"], 1)

        for name in ("nonce", "payload_hash", "signature", "expires_at", "scope", "operation"):
            forged = json.loads(json.dumps(body))
            current = str(forged["proof"][name])
            forged["proof"][name] = current[:-1] + ("0" if current[-1] != "0" else "1")
            outcome = world.client.post(world.path("run.claim", forged), json=forged)
            self.assertEqual(outcome.status_code, 403, name)
            self.assertIn(outcome.json()["code"], {"PROOF_INVALID", "PROOF_EXPIRED", "SCHEMA_UNSUPPORTED"})
            self.assertNotIn(token, outcome.text)

        shifted = json.loads(json.dumps(body))
        shifted["phase"] = "review"
        shifted["scope"]["phase"] = "review"
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
            "preview.merge", "host-1", credential_scope="feedback", credential_generation=1,
        )
        self.assertEqual(merge.json()["reason"], "cross_scope")
        self.assertEqual(world.svc.store.read()["idempotency"], before)
        self.assertEqual(world.svc.store.read()["merges"], [])

        world.inject_gates()
        good, _ = world.post(
            "preview.merge", "host-1", credential_scope="merge", credential_generation=1,
            **world.evidence_fields(),
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
            "preview.merge", "host-1", credential_scope="merge", credential_generation=1,
            idempotency_key="idem-merge-again", **world.evidence_fields(),
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
    def _lease(self, world):
        response, body = world.post("run.claim", "agent-1", credential_scope="run")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["attempt"], 1)
        self.assertEqual(payload["revision"], 1)
        return payload["lease_token"], payload["run_id"], body

    def test_stale_generation_does_not_change_the_newer_run(self):
        world = standard_world()
        token, run_id, _ = self._lease(world)
        renewed, _ = world.post(
            "run.renew", "agent-1", run_id=run_id, credential_scope="run",
            lease_token=token, lease_generation=1, expected_revision=1,
        )
        self.assertEqual(renewed.status_code, 200, renewed.text)
        current = renewed.json()["lease_token"]
        self.assertNotEqual(current, token)
        self.assertEqual(renewed.json()["lease_generation"], 2)
        self.assertEqual(renewed.json()["revision"], 2)
        started, _ = world.post(
            "run.start", "agent-1", run_id=run_id, credential_scope="run",
            lease_token=current, lease_generation=2, expected_revision=2,
        )
        self.assertEqual(started.json()["state"], "RUNNING")
        self.assertEqual(started.json()["revision"], 3)
        before = world.run(run_id)
        stale, _ = world.post(
            "run.complete", "agent-1", run_id=run_id, credential_scope="run",
            lease_token=token, lease_generation=1, result="SUCCEEDED", expected_revision=3,
        )
        self.assertEqual(stale.json()["code"], "LEASE_FENCE_REJECTED")
        self.assertFalse(stale.json()["changed"])
        self.assertEqual(stale.json()["state"], "RUNNING")
        after = world.run(run_id)
        self.assertEqual(after["state"], before["state"])
        self.assertEqual(after["generation"], 2)
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["token_hash"], before["token_hash"])
        self.assertNotEqual(after["state"], "UNKNOWN")
        done, _ = world.post(
            "run.complete", "agent-1", run_id=run_id, credential_scope="run",
            lease_token=current, lease_generation=2, result="SUCCEEDED", expected_revision=3,
        )
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(world.run(run_id)["task_id"], TASK)
        nxt, _ = world.post("run.claim", "agent-1", credential_scope="run")
        self.assertEqual(nxt.status_code, 200, nxt.text)
        bare, _ = world.post(
            "run.complete", "agent-1", run_id=nxt.json()["run_id"], credential_scope="run",
            lease_token="1", lease_generation=1, result="SUCCEEDED", expected_revision=1,
        )
        self.assertEqual(bare.json()["code"], "LEASE_FENCE_REJECTED")

    def test_timeout_does_not_reclaim_or_mark_unknown_without_the_proxy(self):
        world = standard_world()
        token, run_id, _ = self._lease(world)
        world.post(
            "run.start", "agent-1", run_id=run_id, credential_scope="run",
            lease_token=token, lease_generation=1, expected_revision=1,
        )
        world.clock.t += 60
        expired, _ = world.post(
            "run.complete", "agent-1", run_id=run_id, credential_scope="run",
            lease_token=token, lease_generation=1, result="SUCCEEDED", expected_revision=2,
        )
        self.assertEqual(expired.json()["code"], "LEASE_FENCE_REJECTED")
        self.assertEqual(expired.json()["reason"], "expired")
        self.assertEqual(world.run(run_id)["state"], "RUNNING")
        self.assertEqual(world.run(run_id)["revision"], 2)
        reclaim, _ = world.post("run.claim", "agent-1", credential_scope="run", recovery="timeout")
        self.assertEqual(reclaim.json()["code"], "RECLAIM_DENIED")
        self.assertEqual(world.run(run_id)["state"], "RUNNING")
        client, _ = world.post("run.reconcile", "agent-1", run_id=run_id, credential_scope="run")
        self.assertEqual(client.json()["reason"], "client_cannot_fence")
        self.assertEqual(world.run(run_id)["state"], "RUNNING")
        with self.assertRaises(ControlError):
            world.svc.trusted_fence(run_id, fenced=False, effect_known=False)
        with self.assertRaises(ControlError):
            world.svc.trusted_fence(run_id, fenced=True, effect_known=True)
        self.assertEqual(world.run(run_id)["state"], "RUNNING")
        fenced = world.svc.trusted_fence(run_id, fenced=True, effect_known=False)
        self.assertEqual(fenced["state"], "UNKNOWN")
        self.assertFalse(fenced["github_fencing"])
        bypass, _ = world.post("run.claim", "agent-1", credential_scope="run")
        self.assertEqual(bypass.json()["code"], "RECLAIM_DENIED")
        self.assertEqual(bypass.json()["reason"], "unknown_unreconciled")
        self.assertEqual(world.run(run_id)["state"], "UNKNOWN")
        with self.assertRaises(ControlError):
            world.svc.trusted_classify(run_id, fenced=False, classification="succeeded")
        self.assertEqual(world.run(run_id)["state"], "UNKNOWN")
        classified = world.svc.trusted_classify(run_id, fenced=True, classification="succeeded")
        self.assertEqual(classified["state"], "SUCCEEDED")
        fresh, _ = world.post("run.claim", "agent-1", credential_scope="run")
        self.assertEqual(fresh.status_code, 200, fresh.text)
        self.assertNotEqual(fresh.json()["run_id"], run_id)
        self.assertEqual(fresh.json()["attempt"], 2)
        self.assertTrue(fresh.json()["lease_token"])

    def test_github_effects_are_serial_and_a_stale_generation_does_not_apply(self):
        world = standard_world()
        token, run_id, _ = self._lease(world)
        renewed, _ = world.post(
            "run.renew", "agent-1", run_id=run_id, credential_scope="run",
            lease_token=token, lease_generation=1, expected_revision=1,
        )
        current = renewed.json()["lease_token"]
        world.post(
            "run.start", "agent-1", run_id=run_id, credential_scope="run",
            lease_token=current, lease_generation=2, expected_revision=2,
        )
        stale, _ = world.post(
            "github.effect", "agent-1", run_id=run_id, credential_scope="run",
            lease_token=token, lease_generation=1, effect_id="effect-old", expected_revision=3,
        )
        self.assertEqual(stale.json()["code"], "LEASE_FENCE_REJECTED")
        self.assertEqual(world.svc.store.read()["effects"], [])
        self.assertEqual(world.run(run_id)["generation"], 2)
        world.svc.effect_hold = lambda: time.sleep(0.05)
        binding = world.lease_fields(run_id)
        first = world.sign(
            "agent-1", "github.effect", run_id=run_id, credential_scope="run",
            lease_token=current, lease_generation=2, effect_id="effect-a", expected_revision=3,
            **binding,
        )
        second = world.sign(
            "agent-1", "github.effect", run_id=run_id, credential_scope="run",
            lease_token=current, lease_generation=2, effect_id="effect-b", expected_revision=3,
            **binding,
        )
        errors = []

        def submit(body):
            try:
                world.svc.execute("github.effect", body, route_class="isolated")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=submit, args=(body,)) for body in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].reason, "revision")
        parallel = sorted((span for span in world.svc.effects.spans if span[0] in {"effect-a", "effect-b"}), key=lambda item: item[1])
        self.assertEqual(len(parallel), 2)
        self.assertLessEqual(parallel[0][2], parallel[1][1] + 0.001)
        stored = {item["id"] for item in world.svc.store.read()["effects"]}
        self.assertEqual(len(stored), 1)
        follow, _ = world.post(
            "github.effect", "agent-1", run_id=run_id, credential_scope="run",
            lease_token=current, lease_generation=2, effect_id="effect-c",
            expected_revision=world.run(run_id)["revision"],
        )
        self.assertEqual(follow.status_code, 200, follow.text)
        self.assertFalse(follow.json()["github_fencing"])
        self.assertEqual(
            {item["id"] for item in world.svc.store.read()["effects"]},
            stored | {"effect-c"},
        )
        self.assertFalse(DurableEffectProxy.SIMULATES_GITHUB_FENCING)
        self.assertIn("GitHub fencing", DurableEffectProxy.FENCING_NOTICE)


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
        world.inject_gates()
        allowed, _ = world.post(
            "preview.merge", "host-1", credential_scope="merge", credential_generation=1, **world.evidence_fields(),
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        world.svc.clear_gate_evidence()
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
            response, _ = world.post("limit.consume", "agent-1")
            self.assertEqual(response.status_code, 200, response.text)
        limited, _ = world.post("limit.consume", "agent-1")
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json()["code"], "RATE_LIMITED")
        self.assertIn("script-src", limited.headers["content-security-policy"])
        world.reopen()
        still, _ = world.post("limit.consume", "agent-1")
        self.assertEqual(still.status_code, 429)
        buckets = world.svc.store.read()["limits"]
        self.assertEqual(len(buckets), 1)
        self.assertGreaterEqual(next(iter(buckets.values()))["count"], 4)
        self.assertNotIn("login", buckets)

        issued, _ = world.post("session.issue", "agent-1")
        raw = issued.json()["session_id"]
        self.assertNotIn(raw, world.svc.store.text())
        world.register("host-admin", "host", scopes=["pull", "merge", "publish"], admin=True)
        revoked, _ = world.post("session.revoke", "host-admin", session_id=raw)
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
        anonymous = https.get("/api/v1/control/branch-protection?overall=proven")
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(anonymous.json()["reason"], "unauthenticated")
        self.assertNotIn("overall", anonymous.json())
        query = "overall=proven"
        signed = world.sign(
            "host-1", "branch.read", method="GET", query=canonical_query(query), credential_scope="",
        )
        secure = https.get(
            "/api/v1/control/branch-protection?" + query,
            headers={"x-control-proof": base64.b64encode(json.dumps(signed).encode()).decode()},
        )
        self.assertEqual(secure.status_code, 200, secure.text)
        self.assertEqual(secure.json()["overall"], "unproven")
        self.assertFalse(secure.json()["rules_modified"])
        self.assertEqual(secure.json()["evidence_source"], "server_readonly_observation")
        self.assertEqual(secure.json()["permission"], "branch.read")
        self.assertFalse(secure.json()["client_query_is_evidence"])
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
        self.assertNotIn("control_authz", main)
        self.assertIn("pbkdf2_sha256", auth)
        self.assertIn("csrf", auth.lower())
        self.assertIn("script-src", (ROOT / "app" / "control_identity" / "boundary.py").read_text(encoding="utf-8"))


def effect_proxy_worker(root, effect_id, delay):
    import time as time_module

    from app.control_identity.effects import DurableEffectProxy

    proxy = DurableEffectProxy(root)
    proxy.check(effect_id, lambda: None, hold=lambda: time_module.sleep(delay))


class ReviewRegressionTests(unittest.TestCase):
    def test_unsigned_fields_and_secret_digest_are_rejected(self):
        world = standard_world()
        response, body = world.post(
            "signer.revoke", "host-1", target_signer="agent-2",
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["reason"], "admin")
        self.assertNotIn("agent-2", world.svc.store.read()["revoked_signers"])
        world.register("host-admin", "host", admin=True)
        allowed, signed = world.post("signer.revoke", "host-admin", target_signer="agent-2")
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertIn("agent-2", world.svc.store.read()["revoked_signers"])
        before_nonces = dict(world.svc.store.read()["nonces"])
        empty, _ = world.post("feedback.write", "agent-1", credential_scope="feedback", credential_generation=1, authorizer="")
        self.assertEqual(empty.status_code, 403)
        self.assertEqual(empty.json()["reason"], "empty_authorizer")
        self.assertEqual(world.svc.store.read()["nonces"], before_nonces)
        tampered = json.loads(json.dumps(signed))
        tampered["target_signer"] = "agent-1"
        rejected = world.client.post(world.path("signer.revoke", tampered), json=tampered)
        self.assertEqual(rejected.json()["reason"], "payload_hash")
        self.assertNotIn("agent-1", world.svc.store.read()["revoked_signers"])
        gated = json.loads(json.dumps(signed))
        gated["gates"] = {"frozen": False}
        gated["idempotency_key"] = "idem-gates"
        unsigned = world.client.post(world.path("signer.revoke", gated), json=gated)
        self.assertEqual(unsigned.status_code, 422)
        self.assertEqual(unsigned.json()["reason"], "canonical")

    def test_rejected_requests_do_not_pollute_and_reads_do_not_consume_nonces(self):
        world = standard_world()
        before = world.svc.store.text()
        denied, body = world.post("feedback.write", "host-1", credential_scope="feedback", credential_generation=1)
        self.assertEqual(denied.json()["reason"], "role")
        self.assertEqual(world.svc.store.text(), before)
        self.assertEqual(world.svc.store.read()["nonces"], {})
        first, signed = world.post("diagnostic.read", "service-a", credential_scope="read", credential_generation=1, target_app_id="app-a")
        second = world.client.post(world.path("diagnostic.read", signed), json=signed)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(world.svc.store.read()["nonces"], {})
        self.assertEqual(first.json()["read_replay_policy"], READ_REPLAY_POLICY)
        cross, _ = world.post(
            "diagnostic.read", "agent-1", credential_scope="read", credential_generation=1,
            task_id="task-other", target_app_id="",
        )
        self.assertEqual(cross.json()["reason"], "cross_task")
        self.assertEqual(world.svc.store.read()["nonces"], {})
        other_repo, _ = world.post(
            "diagnostic.read", "service-a", credential_scope="read", credential_generation=1,
            repo_id="other/repo", target_app_id="app-a",
        )
        self.assertEqual(other_repo.json()["reason"], "cross_repo")

    def test_recheck_sees_revocation_before_the_side_effect(self):
        world = standard_world()
        before = world.svc.store.text()

        def revoke(state):
            state["revoked_signers"].append("agent-1")

        world.svc.transaction_probe = revoke
        denied, _ = world.post("feedback.write", "agent-1", credential_scope="feedback", credential_generation=1)
        self.assertEqual(denied.json()["code"], "PROOF_REVOKED")
        self.assertEqual(world.svc.store.text(), before)
        world.svc.transaction_probe = None

    def test_server_rate_limit_and_session_owner(self):
        world = standard_world()
        issued, _ = world.post("session.issue", "agent-1")
        raw = issued.json()["session_id"]
        foreign, _ = world.post("session.check", "agent-2", session_id=raw)
        self.assertEqual(foreign.json()["reason"], "session_owner")
        outsider, _ = world.post("session.revoke", "host-1", session_id=raw)
        self.assertEqual(outsider.json()["reason"], "session_owner")
        still, _ = world.post("session.check", "agent-1", session_id=raw)
        self.assertEqual(still.status_code, 200, still.text)
        for _ in range(3):
            self.assertEqual(world.post("limit.consume", "agent-2")[0].status_code, 200)
        self.assertEqual(world.post("limit.consume", "agent-1")[0].status_code, 200)

    def test_store_fails_closed_and_effects_do_not_overlap_across_processes(self):
        import multiprocessing

        world = standard_world()
        world.svc.store.path.write_text("{", encoding="utf-8")
        with self.assertRaises(ControlError) as raised:
            world.svc.store.read()
        self.assertEqual(raised.exception.reason, "store_corrupt")
        self.assertEqual(world.svc.store.path.read_text(encoding="utf-8"), "{")

        scratch = Path(tempfile.mkdtemp())
        fresh = ControlIdentityService(scratch / "private-store", isolated=True, clock=world.clock)
        mode = fresh.store.root.stat().st_mode
        self.assertEqual(mode & 0o077, 0)
        os.chmod(fresh.store.root, 0o755)
        with self.assertRaises(ControlError) as raised:
            fresh.store.read()
        self.assertEqual(raised.exception.reason, "store_permissions")

        linked = ControlIdentityService(scratch / "link-store", isolated=True, clock=world.clock)
        linked.store.path.write_text(linked.store.text() or "{}", encoding="utf-8")
        target = scratch / "secret-target.json"
        target.write_text('{"secret":"github_pat_shouldnotfollow"}', encoding="utf-8")
        linked.store.path.unlink()
        linked.store.path.symlink_to(target)
        with self.assertRaises(ControlError) as raised:
            linked.store.read()
        self.assertEqual(raised.exception.reason, "store_symlink")

        from app.control_identity.durable import write_all

        kept = ControlIdentityService(scratch / "kept-store", isolated=True, clock=world.clock)

        def mutate(state):
            state["published"].append("kept")
            return state, None

        kept.store.update(mutate)
        original = kept.store.path.read_bytes()
        real_write = os.write

        def fail_write(fd, data):
            return 0

        os.write = fail_write
        try:
            with self.assertRaises(ControlError) as raised:
                kept.store.update(mutate)
            self.assertEqual(raised.exception.reason, "short_write")
        finally:
            os.write = real_write
        self.assertEqual(kept.store.path.read_bytes(), original)

        parts = []

        def partial(buf):
            if not parts:
                parts.append(True)
                return 1
            return len(buf)

        write_all(partial, b"abcd")

        proxy_root = scratch / "proxy-root"
        proxy_root.mkdir()
        ctx = multiprocessing.get_context("spawn")
        first = ctx.Process(target=effect_proxy_worker, args=(str(proxy_root), "proc-a", 0.2))
        second = ctx.Process(target=effect_proxy_worker, args=(str(proxy_root), "proc-b", 0.2))
        first.start()
        second.start()
        first.join(5)
        second.join(5)
        self.assertEqual(first.exitcode, 0)
        self.assertEqual(second.exitcode, 0)
        journal = json.loads((proxy_root / "effect-journal.json").read_text(encoding="utf-8"))
        finished = [row for row in journal if row["phase"] == "finished"]
        self.assertEqual(len(finished), 2)
        finished.sort(key=lambda row: row["start"])
        self.assertLessEqual(finished[0]["end"], finished[1]["start"])
        self.assertTrue(all(row["github_fencing"] is False for row in journal))

    def test_heartbeat_and_versioned_release_paths(self):
        world = standard_world()
        missing = world.client.post("/api/v1/control/releases:accept", json={"schema_version": 2})
        self.assertEqual(missing.status_code, 404)
        claimed, _ = world.post("run.claim", "agent-1", credential_scope="run")
        self.assertEqual(claimed.status_code, 200, claimed.text)
        claimed_body = claimed.json()
        started, _ = world.post(
            "run.start", "agent-1", run_id=claimed_body["run_id"], credential_scope="run",
            lease_token=claimed_body["lease_token"], lease_generation=1, expected_revision=1,
        )
        self.assertEqual(started.json()["state"], "RUNNING")
        world.clock.t += 10
        beat, _ = world.post(
            "run.heartbeat", "agent-1", run_id=claimed_body["run_id"], credential_scope="run",
            lease_token=claimed_body["lease_token"], lease_generation=1, expected_revision=started.json()["revision"],
        )
        self.assertEqual(beat.status_code, 200, beat.text)
        self.assertEqual(beat.json()["state"], "RUNNING")
        self.assertEqual(beat.json()["lease_generation"], 1)
        self.assertGreater(world.run(claimed_body["run_id"])["expires_at"], world.clock.t)
        self.assertEqual(world.run(claimed_body["run_id"])["run_id"], claimed_body["run_id"])
        candidate = world.svc.register_candidate(repo=REPO, task=TASK, head=HEAD, base=BASE, merge=MERGE, tree=TREE)
        rejected, _ = world.post(
            "release.reject", "accept-1", release_version="0.6.0", credential_scope="accept",
            candidate_id=candidate, head=HEAD, base=BASE, merge=MERGE, tree=TREE, object_id=candidate,
        )
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertEqual(world.svc.store.read()["acceptances"][candidate]["outcome"], "rejected")

    def test_shared_authz_api_is_server_configured(self):
        with self.assertRaises(AuthzError):
            AuthzService(registry={"actor": "test"}, isolated=True, policy_revision=1)
        registry = PrincipalRegistry(isolated=True)
        service = AuthzService(registry=registry, isolated=True, policy_revision=1)
        self.assertTrue(service.registry.test_identities_enabled)
        closed = PrincipalRegistry(isolated=False)
        self.assertFalse(closed.test_identities_enabled)
        principal = Principal(
            principal_id="agent-1", kind="agent", repo_id=REPO, app_id="", producer_id="agent-1",
            stream_id="agent-1:task", scopes=("task:" + TASK, "feedback"), trusted_source="agent",
        )
        self.assertIsInstance(principal.scopes, frozenset)
        self.assertEqual(trusted_source_for(principal), "agent")
        self.assertEqual(service.trusted_source_for(principal), "agent")
        with self.assertRaises(AuthzError):
            AuthzService(registry=registry, isolated=True, policy_revision="w01-isolated-1")
        with self.assertRaises(AuthzError):
            AuthzService.from_request({"actor": "test", "isolated": True, "policy_revision": 1})
        self.assertEqual(SOURCE_BY_KIND["policy_service"], "policy_service")
        policy_principal = Principal(
            principal_id="policy-1", kind="policy_service", repo_id=REPO, app_id="", producer_id="policy_service",
            stream_id="policy_service:authorization", scopes=frozenset({"event.append"}), trusted_source="policy_service",
        )
        self.assertEqual(trusted_source_for(policy_principal), "policy_service")
        for operation in ("event.append", "event.read", "diagnostic.cursor", "report.submit", "report.read"):
            self.assertIn(operation, MATRIX)
            self.assertIn(operation, REQUIRED_SCOPES)
        lied = Principal(
            principal_id="agent-1", kind="agent", repo_id=REPO, app_id="", producer_id="agent-1",
            stream_id="agent-1:task", scopes=("task:" + TASK,), trusted_source="policy_service",
        )
        with self.assertRaises(AuthzError):
            trusted_source_for(lied)
        with self.assertRaises(AuthzError):
            authorize(principal, "shell.exec", ResourceBinding(
                repo_id=REPO, app_id="", producer_id="agent-1", stream_id="agent-1:task",
                operation="shell.exec", scope="", task_id=TASK, target_app_id="",
            ))
        self.assertIs(READ_REPLAY_POLICY, world_policy())
        self.assertIn("nonce", WRITE_REPLAY_POLICY)


def world_policy():
    return READ_REPLAY_POLICY


class ReviewClosureTests(unittest.TestCase):
    def _candidate(self, world, head):
        return world.svc.register_candidate(repo=REPO, task=TASK, head=head, base=BASE, merge=MERGE, tree=TREE)

    def _claim(self, world, head):
        candidate = self._candidate(world, head)
        response, _ = world.post(
            "run.claim", "agent-1", credential_scope="run", phase="develop",
            candidate_id=candidate, head=head, base=BASE, merge=MERGE, tree=TREE,
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["attempt"], 1)
        return payload, candidate

    def test_parallel_chains_and_a_new_candidate_keep_separate_leases(self):
        world = standard_world()
        first, candidate_a = self._claim(world, HEAD)
        second, candidate_b = self._claim(world, HEAD_B)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(world.run(first["run_id"])["state"], "LEASED")
        self.assertEqual(world.run(second["run_id"])["state"], "LEASED")
        self.assertEqual(world.run(first["run_id"])["attempt"], 1)
        self.assertEqual(world.run(second["run_id"])["attempt"], 1)
        self.assertNotEqual(world.run(first["run_id"])["subject"], world.run(second["run_id"])["subject"])
        self.assertEqual(world.run(first["run_id"])["subject"], phase_subject("develop", TASK, candidate_a))
        done, _ = world.post(
            "run.complete", "agent-1", run_id=first["run_id"], credential_scope="run",
            lease_token=first["lease_token"], lease_generation=1, result="SUCCEEDED", expected_revision=1,
        )
        self.assertEqual(done.status_code, 200, done.text)
        again, _ = world.post(
            "run.claim", "agent-1", credential_scope="run", phase="develop",
            candidate_id=candidate_a, head=HEAD, base=BASE, merge=MERGE, tree=TREE,
        )
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["attempt"], 2)
        third_head = "f" * 40
        third, _ = self._claim(world, third_head)
        self.assertEqual(third["attempt"], 1)
        self.assertEqual(world.run(second["run_id"])["state"], "LEASED")

    def test_wrong_candidate_cross_subject_and_stale_lease_do_not_cross(self):
        world = standard_world()
        first, candidate_a = self._claim(world, HEAD)
        second, candidate_b = self._claim(world, HEAD_B)
        before_a = world.run(first["run_id"])
        before_b = world.run(second["run_id"])
        wrong, _ = world.post(
            "run.complete", "agent-1", run_id=first["run_id"], credential_scope="run",
            lease_token=first["lease_token"], lease_generation=1, result="SUCCEEDED", expected_revision=1,
            candidate_id=candidate_b, head=HEAD_B, base=BASE, merge=MERGE, tree=TREE, phase="develop",
        )
        self.assertEqual(wrong.json()["code"], "LEASE_FENCE_REJECTED")
        self.assertEqual(wrong.json()["reason"], "candidate")
        self.assertFalse(wrong.json()["changed"])
        crossed, _ = world.post(
            "run.complete", "agent-1", run_id=first["run_id"], credential_scope="run",
            lease_token=first["lease_token"], lease_generation=1, result="SUCCEEDED", expected_revision=1,
            candidate_id=candidate_a, head=HEAD, base=BASE, merge=MERGE, tree=TREE, phase="test",
        )
        self.assertEqual(crossed.json()["code"], "LEASE_FENCE_REJECTED")
        self.assertEqual(crossed.json()["reason"], "subject")
        self.assertEqual(world.run(first["run_id"])["state"], before_a["state"])
        self.assertEqual(world.run(second["run_id"])["state"], before_b["state"])
        renewed, _ = world.post(
            "run.renew", "agent-1", run_id=first["run_id"], credential_scope="run",
            lease_token=first["lease_token"], lease_generation=1, expected_revision=1,
        )
        self.assertEqual(renewed.status_code, 200, renewed.text)
        stale, _ = world.post(
            "run.complete", "agent-1", run_id=first["run_id"], credential_scope="run",
            lease_token=first["lease_token"], lease_generation=1, result="SUCCEEDED", expected_revision=2,
        )
        self.assertEqual(stale.json()["code"], "LEASE_FENCE_REJECTED")
        self.assertEqual(stale.json()["reason"], "generation")
        stolen, _ = world.post(
            "run.complete", "agent-1", run_id=second["run_id"], credential_scope="run",
            lease_token=first["lease_token"], lease_generation=1, result="SUCCEEDED", expected_revision=1,
            candidate_id=candidate_b, head=HEAD_B, base=BASE, merge=MERGE, tree=TREE, phase="develop",
        )
        self.assertEqual(stolen.json()["code"], "LEASE_FENCE_REJECTED")
        self.assertEqual(world.run(second["run_id"])["state"], "LEASED")
        self.assertEqual(world.run(second["run_id"])["revision"], before_b["revision"])
        self.assertNotEqual(world.run(first["run_id"])["state"], "SUCCEEDED")

    def test_lease_scope_rejects_a_different_app_producer_or_stream(self):
        world = standard_world()
        payload, candidate = self._claim(world, HEAD)
        run = world.run(payload["run_id"])
        common = dict(
            candidate_id=candidate, phase="develop", task_id=TASK, repo_id=REPO,
            head=HEAD, base=BASE, merge=MERGE, tree=TREE,
            producer_id=run["producer_id"], stream_id=run["stream_id"],
        )
        with self.assertRaises(ControlError) as raised:
            assert_lease_scope(run, app_id="app-b", **common)
        self.assertEqual(raised.exception.reason, "app")
        producer = dict(common)
        producer["app_id"] = run["app_id"]
        producer["producer_id"] = "other-producer"
        with self.assertRaises(ControlError) as raised:
            assert_lease_scope(run, **producer)
        self.assertEqual(raised.exception.reason, "producer")
        stream = dict(common)
        stream["app_id"] = run["app_id"]
        stream["stream_id"] = "other:task"
        with self.assertRaises(ControlError) as raised:
            assert_lease_scope(run, **stream)
        self.assertEqual(raised.exception.reason, "stream")
        omitted, _ = world.post("run.claim", "agent-1", credential_scope="run", producer_id="")
        self.assertEqual(omitted.json()["reason"], "cross_producer")
        masked, _ = world.post(
            "diagnostic.read", "service-a", credential_scope="read", credential_generation=1,
            app_id="app-b", target_app_id="app-a",
        )
        self.assertEqual(masked.json()["reason"], "cross_app")

    def test_policy_checks_role_scope_evidence_and_high_risk_binding(self):
        world = standard_world()
        known, _ = world.post("policy.evaluate", "host-1", action="deploy.apply")
        self.assertEqual(known.json()["code"], "POLICY_DENIED")
        self.assertEqual(known.json()["scope_class"], "ordinary")
        self.assertFalse(known.json()["allow"])
        role, _ = world.post(
            "policy.evaluate", "host-1", action="feedback.write", credential_scope="feedback", credential_generation=1,
        )
        self.assertEqual(role.json()["reason"], "role")
        world.inject_gates()
        scoped, _ = world.post(
            "policy.evaluate", "host-1", action="preview.merge", credential_scope="pull", credential_generation=1,
            **world.evidence_fields(),
        )
        self.assertEqual(scoped.json()["reason"], "scope")
        allowed, _ = world.post(
            "policy.evaluate", "host-1", action="preview.merge", credential_scope="merge", credential_generation=1,
            **world.evidence_fields(),
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertTrue(allowed.json()["allow"])
        self.assertFalse(allowed.json()["side_effect"])
        other = candidate_id_for(REPO, TASK, HEAD_B, BASE, MERGE, TREE)
        borrowed, _ = world.post(
            "policy.evaluate", "host-1", action="preview.merge", credential_scope="merge", credential_generation=1,
            candidate_id=other, head=HEAD_B, base=BASE, merge=MERGE, tree=TREE,
        )
        self.assertEqual(borrowed.json()["code"], "POLICY_DENIED")
        self.assertEqual(borrowed.json()["scope_class"], "ordinary")
        world.svc.clear_gate_evidence()
        missing, _ = world.post(
            "preview.merge", "host-1", credential_scope="merge", credential_generation=1, **world.evidence_fields(),
        )
        self.assertEqual(missing.json()["code"], "POLICY_DENIED")
        expired = dict(world._evidence)
        expired["not_before"] = world.clock.t - 40
        expired["not_after"] = world.clock.t
        world.svc.inject_gate_evidence(expired)
        stale_evidence, _ = world.post(
            "preview.merge", "host-1", credential_scope="merge", credential_generation=1, **world.evidence_fields(),
        )
        self.assertEqual(stale_evidence.json()["code"], "POLICY_DENIED")

        def keep_name(state):
            state["policy"]["high_risk_allow"] = ["host.privilege"]
            state["policy"]["high_risk_grants"] = []
            return state, None

        world.svc.store.update(keep_name)
        listed, _ = world.post("policy.evaluate", "host-1", action="host.privilege", object_id="obj-a")
        self.assertEqual(listed.json()["scope_class"], "high_risk")
        candidate = world.evidence_fields()["candidate_id"]
        subject = phase_subject("develop", TASK, candidate)
        world.svc.inject_high_risk_grant(
            principal_id="host-1", operation="host.privilege", object_id="obj-a", subject=subject,
        )
        other_object, _ = world.post(
            "policy.evaluate", "host-1", action="host.privilege", object_id="obj-b", phase="develop", **world.evidence_fields(),
        )
        self.assertEqual(other_object.json()["scope_class"], "high_risk")
        other_subject, _ = world.post(
            "policy.evaluate", "host-1", action="host.privilege", object_id="obj-a", phase="test", **world.evidence_fields(),
        )
        self.assertEqual(other_subject.json()["scope_class"], "high_risk")
        before = world.svc.store.text()
        granted, _ = world.post(
            "policy.evaluate", "host-1", action="host.privilege", object_id="obj-a", phase="develop", **world.evidence_fields(),
        )
        self.assertEqual(granted.status_code, 200, granted.text)
        self.assertTrue(granted.json()["allow"])
        self.assertEqual(granted.json()["scope_class"], "high_risk")
        self.assertFalse(granted.json()["side_effect"])
        self.assertEqual(world.svc.store.text(), before)
        direct, _ = world.post(
            "host.privilege", "host-1", object_id="obj-a", phase="develop", **world.evidence_fields(),
        )
        self.assertEqual(direct.json()["code"], "NOT_AUTHORIZED")
        self.assertEqual(world.svc.store.read()["policy"]["high_risk_grants"][0]["special_authorization"], True)

    def test_authorize_requires_every_scope(self):
        holder = Principal(
            principal_id="hostops-1", kind="hostops", repo_id=REPO, app_id="", producer_id="hostops-1",
            stream_id="hostops-1:diagnostic", scopes=frozenset({"read", "task:" + TASK}),
            trusted_source="hostops_readonly",
        )
        binding = ResourceBinding(
            repo_id=REPO, app_id="", producer_id="hostops-1", stream_id="hostops-1:diagnostic",
            operation="diagnostic.cursor", scope="read", task_id=TASK, target_app_id="",
        )
        with self.assertRaises(AuthzError) as raised:
            authorize(holder, "diagnostic.cursor", binding)
        self.assertEqual(raised.exception.reason, "cross_scope")
        complete = Principal(
            principal_id="hostops-1", kind="hostops", repo_id=REPO, app_id="", producer_id="hostops-1",
            stream_id="hostops-1:diagnostic",
            scopes=frozenset({"read", "diagnostic.cursor", "task:" + TASK}),
            trusted_source="hostops_readonly",
        )
        authorize(complete, "diagnostic.cursor", binding)
        self.assertTrue(REQUIRED_SCOPES["diagnostic.cursor"] <= complete.scopes)


if __name__ == "__main__":
    unittest.main()
