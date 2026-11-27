"""Model-check the V0.6.0 W00 draft. Guards match by string only.

These tests are not production verification. They do not open real routes,
read host state, or prove that a lease generation by itself fences GitHub.
"""
import hashlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "v060" / "w00"
DOCS = ROOT / "docs" / "v060"
BASE = "db19bff19307fa763fd50e94b4cedfda7181a141"
WRITE_FIELDS = ("actor", "scope", "expected_revision", "idempotency_key")
SECRET_MARKERS = ("github_pat_", "ghp_", "sk-", "AKIA", "BEGIN PRIVATE KEY")
LABELS = {"covered": "已覆盖", "remaining": "独有待做", "outdated": "过时", "unproven": "未证实"}
P1 = {"F03", "F11", "U04", "U09", "S09", "S10", "D05", "D09", "A08", "A09", "I11", "I12", "R05", "R10"}
PACKAGES = {
    "W01": ["F04", "F05", "D02", "D06", "D07"],
    "W02": ["A01", "A02", "A03", "A07", "A08", "A09", "A10", "A11", "I07"],
    "W03": ["A04", "A05", "A06"],
    "W04": ["F01", "S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08"],
    "W05": ["F02", "F03", "F06", "F07", "F08", "F09", "F10", "R08"],
    "W06": ["U02", "D01", "D03", "D04", "D05", "D08", "D09"],
    "W07": ["R01", "R02", "R03", "R04", "R05", "R06", "R07", "R09"],
    "W08": ["U01", "U03", "U04", "U05", "U06", "U07", "U08"],
    "W09": ["I01", "I02", "I03", "I04", "I05", "I06", "I08", "I09", "I10", "I11"],
    "W10": ["S09"],
    "W11": ["F11", "U09", "S10", "D10", "A12", "I12", "R10"],
}


def load(name):
    return json.loads((FIXTURE / name).read_text(encoding="utf-8"))


def design_ids():
    counts = [("F", 11), ("U", 9), ("S", 10), ("D", 10), ("A", 12), ("I", 12), ("R", 10)]
    return [f"{prefix}{index:02d}" for prefix, count in counts for index in range(1, count + 1)]


def package_of(item_id):
    for package, ids in PACKAGES.items():
        if item_id in ids:
            return package
    raise KeyError(item_id)


def transitions_of(contract, machine):
    entities = contract["entities"]
    if machine == "task":
        return entities["task"]["transitions"]
    if machine == "task_chain":
        return entities["task"]["chains"]["transitions"]
    if machine == "event_delivery":
        return entities["event"]["delivery"]["transitions"]
    return entities[machine]["transitions"]


def allows(contract, machine, src, dst, guards):
    held = set(guards)
    matched = False
    for edge in transitions_of(contract, machine):
        if edge["from"] == src and edge["to"] == dst:
            matched = True
            guard = edge.get("guard")
            if guard is None or guard in held:
                return True
    return False if not matched else False


APPROVED_SHA = "b2011968a9c14845b89fe6d15dda4a595699e9ec"


def canonical_signed(spec, fields):
    domain = spec["domain_separator"]
    record = spec["record_separator"]
    parts = [spec["version"]]
    for name in spec["fields"]:
        value = str(fields[name])
        if domain in value or record in value:
            raise ValueError("separator in value")
        parts.append(name + domain + value)
    text = record.join(parts)
    if spec["trailing_newline"]:
        text += record
    return text


def lost_token_may_reclaim(state, reconciled, fenced):
    if state == "UNKNOWN" and not (reconciled and fenced):
        return False
    return bool(reconciled and fenced)


def candidate_id(spec, repo, task, head, base, merge, tree):
    if spec["trailing_newline"]:
        raise AssertionError("canonical encoding must not add a trailing newline")
    raw = "\n".join(["v1", repo, task, head, base, merge, tree]).encode(spec["encoding"])
    return spec["prefix"] + hashlib.sha256(raw).hexdigest()


def validate_event(contract, event):
    spec = contract["entities"]["event"]
    if "received_at" in event:
        return "SCHEMA_UNSUPPORTED"
    for field in spec["always_required"]:
        if event.get(field) in (None, ""):
            return "SCHEMA_UNSUPPORTED"
    required = spec["classes"][event["event_class"]]
    nullable = set(spec["nullable"].get(event["event_class"], []))
    for field in required:
        if field not in event:
            return "SCHEMA_UNSUPPORTED"
        if event[field] is None and field not in nullable:
            return "SCHEMA_UNSUPPORTED"
    if event["event_class"] == "deployment" and event.get("previous_healthy_sha") is None:
        if event.get("install_kind") != "first_install":
            return "EVIDENCE_MISSING"
    return None


def project(events):
    """Apply each stream independently. Authorization epochs cannot skip a gap."""
    watermark = {}
    applied = []
    rest = list(events)
    moved = True
    while moved:
        moved = False
        pending = []
        for event in rest:
            epoch = event.get("stream_epoch", 1)
            key = (event["stream_id"], epoch)
            if event.get("classification") == "never_existed" and event["channel"] == "authorization":
                pending.append(event)
                continue
            if event["channel"] == "authorization" and epoch > 1:
                closed = any(
                    item["stream_id"] == event["stream_id"]
                    and item.get("stream_epoch", 1) == epoch - 1
                    and item.get("epoch_closed")
                    for item in applied
                )
                if not closed:
                    pending.append(event)
                    continue
            if event["producer_seq"] == watermark.get(key, 0) + 1:
                watermark[key] = event["producer_seq"]
                applied.append(event)
                moved = True
            else:
                pending.append(event)
        rest = pending
    return [item["event_id"] for item in applied], [item["event_id"] for item in rest]


def no_change_commit_allowed(previous_healthy_sha, install_kind, healthy, pending_journal):
    if install_kind == "first_install" or previous_healthy_sha is None:
        return False
    return bool(healthy) and not pending_journal


def first_install_rollback(contract, resources_removed_proven):
    guards = ["first_install_resources_removed"] if resources_removed_proven else []
    if allows(contract, "deployment", "ROLLBACK", "ROLLED_BACK", guards):
        return "ROLLED_BACK"
    return "SAFE_FROZEN"


def can_close(chain_states, publish_evidence, task_acceptance):
    if not task_acceptance or not chain_states or len(publish_evidence) != len(chain_states):
        return False
    return all(state == "PUBLISHED" for state in chain_states) and all(publish_evidence)


def dependency_may_use_new_base(dependency_integrated, dependent_base, original_base, same_base_required):
    if same_base_required:
        return dependency_integrated and dependent_base == original_base
    return dependency_integrated


def prefer_fact(existing, incoming):
    if incoming["event_class"] == "diagnostic" and existing["event_class"] in {"authorization", "deployment", "candidate"}:
        return existing
    if incoming["revision"] != existing["revision"]:
        return incoming if incoming["revision"] > existing["revision"] else existing
    if incoming["authoritative"] and not existing["authoritative"]:
        return incoming
    return existing


def proxy_allow(current_generation, used_generation, scope, operation):
    return used_generation == current_generation and scope == operation


def web_boundary_ok(csp, body):
    if "script-src" not in csp:
        return False
    return "github_pat_" not in body and "BEGIN PRIVATE KEY" not in body


def terminals_have_no_outgoing(contract):
    entities = contract["entities"]
    machines = [
        ("task", entities["task"]["terminal_phases"], entities["task"]["transitions"]),
        ("task_chain", entities["task"]["chains"]["terminal"], entities["task"]["chains"]["transitions"]),
        ("run", entities["run"]["terminal"], entities["run"]["transitions"]),
        ("candidate", entities["candidate"]["terminal"], entities["candidate"]["transitions"]),
        ("release", entities["release"]["terminal"], entities["release"]["transitions"]),
        ("deployment", entities["deployment"]["terminal"], entities["deployment"]["transitions"]),
        ("resource", entities["resource"]["terminal"], entities["resource"]["transitions"]),
        ("event", entities["event"]["terminal"], entities["event"]["transitions"]),
        ("event_delivery", entities["event"]["delivery"]["terminal"], entities["event"]["delivery"]["transitions"]),
    ]
    problems = []
    for name, terminals, transitions in machines:
        outgoing = {edge["from"] for edge in transitions}
        problems.extend(f"{name}:{state}" for state in terminals if state in outgoing)
    return problems


def client_lease_request(current_state, current_generation, request_generation, server_now, expires_at, token_matches):
    """A rejected caller does not write UNKNOWN and does not touch a newer run."""
    if request_generation != current_generation or server_now >= expires_at or not token_matches:
        return {"changed": False, "state": current_state, "code": "LEASE_FENCE_REJECTED"}
    return {"changed": True, "state": current_state, "code": None}


def proxy_confirm_unknown(actor_role, effect_known):
    if actor_role != "trusted_proxy":
        return {"changed": False, "state": None, "code": "NOT_AUTHORIZED"}
    if effect_known:
        return {"changed": False, "state": None, "code": "EVIDENCE_MISSING"}
    return {"changed": True, "state": "UNKNOWN", "code": "SIDE_EFFECT_UNKNOWN"}


def renew_lease(server_now, expires_at, token_generation, current_generation):
    if token_generation != current_generation or server_now >= expires_at:
        return None
    return current_generation + 1


class Store(dict):
    pass


def call_idempotent(store, *, kind, proof_ref, allow, key, payload_hash, secret, server_now, expires_at, nonce, seen_nonces, revoked, isolated=False, route="real"):
    before = json.dumps(store, sort_keys=True)
    identity_ok = (
        route != "real"
        and isolated
        and kind == "test"
        and isinstance(proof_ref, str)
        and proof_ref.startswith("test-proof://")
        and expires_at > server_now
        and nonce not in seen_nonces
        and kind not in revoked
    )
    if not identity_ok:
        if json.dumps(store, sort_keys=True) != before:
            raise AssertionError("rejected auth changed the idempotency store")
        return {"code": "NOT_AUTHORIZED", "http": 403}, store
    if not allow:
        return {"code": "POLICY_DENIED", "http": 403}, store
    seen_nonces.add(nonce)
    if key in store:
        record = store[key]
        if record["hash"] != payload_hash:
            return {"code": "IDEMPOTENCY_PAYLOAD_MISMATCH", "http": 409}, store
        return {
            "code": "IDEMPOTENCY_REPLAY",
            "http": 200,
            "request_id": record["request_id"],
            "original_result": dict(record["original_public"]),
            "current_state": record["current_state"],
            "lease_token": None,
            "replayed": True,
        }, store
    request_id = f"req-{len(store) + 1}"
    public = {"state": "LEASED", "request_id": request_id}
    token = f"tok-{request_id}" if secret else None
    store[key] = {
        "hash": payload_hash,
        "request_id": request_id,
        "original_public": public,
        "current_state": "LEASED",
        "token_issued": token is not None,
    }
    body = {**public, "replayed": False}
    if token:
        body["lease_token"] = token
    return body, store


def acceptance_allowed(signer_kind, production_verification_enabled):
    if signer_kind != "user_acceptance":
        return False
    return bool(production_verification_enabled)


def parse_matrix_rows(text):
    rows = {}
    pattern = re.compile(r"^\| ([A-Z][A-Z0-9-]*) \| (P[01]) \| (W\d+) \| ([^|]+) \|")
    for line in text.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        item_id, priority, package, label = match.groups()
        rows[item_id] = {"priority": priority, "package": package, "category_label": label.strip()}
    return rows


class ContractFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = load("contract.json")
        cls.items = load("items.json")
        cls.matrix = (DOCS / "01-capability-matrix.md").read_text(encoding="utf-8")
        cls.architecture = (DOCS / "02-architecture-contract.md").read_text(encoding="utf-8")
        cls.ownership = (DOCS / "04-file-ownership.md").read_text(encoding="utf-8")
        cls.plan = (DOCS / "07-w01-w02-plan.md").read_text(encoding="utf-8")

    def test_draft_is_not_frozen(self):
        contract = self.contract
        self.assertEqual(contract["schema_version"], 2)
        self.assertEqual(contract["status"], "draft")
        self.assertTrue(contract["normative"])
        self.assertEqual(contract["normative_scope"], "w01_w02_isolated_development")
        self.assertEqual(contract["normative_sha"], APPROVED_SHA)
        self.assertFalse(contract["merge_authorized"])
        self.assertEqual(contract["freeze"]["approved_sha"], APPROVED_SHA)
        self.assertEqual(contract["freeze"]["approved_comment"], 7217178629)
        self.assertFalse(contract["freeze"]["merge_authorized"])
        self.assertFalse(contract["freeze"]["release_authorized"])
        self.assertFalse(contract["freeze"]["machine_acceptance"])
        self.assertTrue(contract["freeze"]["closeout_verified"])
        self.assertFalse(contract["exactly_once_across_github_and_db"])
        self.assertEqual(contract["clock"], "server")
        self.assertEqual(contract["baseline_sha"], BASE)
        self.assertEqual(contract["revision_comment"], 7216590735)
        self.assertTrue(contract["model_tests"]["not_production_verification"])
        self.assertFalse(contract["identity"]["lease"]["github_native_fencing"])
        self.assertFalse(contract["identity"]["test_identity"]["client_supplied_proof_enters_real_routes"])
        self.assertFalse(contract["identity"]["production_verification_enabled"])
        self.assertFalse(contract["diagnostic"]["subscribe"])
        self.assertEqual(contract["diagnostic"]["mode"], "poll_cursor")
        self.assertEqual(contract["diagnostic"]["cursor_key"], ["stream_id", "stream_epoch"])
        self.assertIn("after_seq", contract["diagnostic"]["query"])
        self.assertFalse(contract["identity"]["lease"]["unknown_alone_allows_reclaim"])
        self.assertEqual(
            contract["identity"]["lease"]["lost_token_reclaim_requires"],
            ["trusted_reconciliation", "fencing"],
        )
        self.assertFalse(lost_token_may_reclaim("UNKNOWN", False, False))
        self.assertFalse(lost_token_may_reclaim("UNKNOWN", True, False))
        self.assertTrue(lost_token_may_reclaim("UNKNOWN", True, True))
        signed = contract["identity"]["signature_canonical"]
        self.assertEqual(signed["fields"], contract["identity"]["signed_content"])
        sample = {name: f"value-{name}" for name in signed["fields"]}
        encoded = canonical_signed(signed, sample)
        self.assertTrue(encoded.startswith("v1\n"))
        self.assertIn("payload_hash\u001fvalue-payload_hash", encoded)
        self.assertIn("signer_id\u001fvalue-signer_id", encoded)
        self.assertIn("key_id\u001fvalue-key_id", encoded)
        self.assertIn("issued_at\u001fvalue-issued_at", encoded)
        sample["payload_hash"] = "other-hash"
        self.assertNotEqual(canonical_signed(signed, sample), encoded)
        self.assertEqual(len(contract["entities"]["candidate"]["id"]["lines"]), 7)

    def test_transition_cases_follow_the_machine(self):
        seen = set()
        for case in self.contract["cases"]:
            self.assertEqual(case["kind"], "transition")
            self.assertNotIn(case["id"], seen)
            seen.add(case["id"])
            got = allows(self.contract, case["machine"], case["from"], case["to"], case["guards"])
            expect = case["expect"] == "allow"
            self.assertEqual(got, expect, case["id"])
        self.assertIn("leased_expiry", seen)
        self.assertIn("unknown_succeeded", seen)
        self.assertIn("retry_wait_does_not_resume", seen)
        self.assertFalse(any(
            edge["from"] == "RETRY_WAIT" and edge["to"] == "RUNNING"
            for edge in self.contract["entities"]["run"]["transitions"]
        ))
        self.assertFalse(any(
            edge["from"] == "PUBLISHING" and edge["to"] == "ACCEPTANCE"
            for edge in self.contract["entities"]["release"]["transitions"]
        ))
        deployment = self.contract["entities"]["deployment"]["transitions"]
        self.assertFalse(any(edge["from"] == "SAFE_FROZEN" for edge in deployment))
        for src in ("LEASED", "RUNNING"):
            for dst in ("CANCELLED", "SUPERSEDED"):
                edges = [edge for edge in self.contract["entities"]["run"]["transitions"]
                         if edge["from"] == src and edge["to"] == dst]
                self.assertTrue(edges, f"{src}->{dst}")
                self.assertTrue(all(edge.get("guard") for edge in edges))

    def test_lease_generation_uses_server_clock_and_hides_replayed_tokens(self):
        stale = client_lease_request("RUNNING", 2, 1, 10, 50, False)
        self.assertFalse(stale["changed"])
        self.assertEqual(stale["state"], "RUNNING")
        self.assertEqual(stale["code"], "LEASE_FENCE_REJECTED")
        expired = client_lease_request("RUNNING", 1, 1, 100, 50, True)
        self.assertFalse(expired["changed"])
        self.assertEqual(expired["state"], "RUNNING")
        self.assertEqual(proxy_confirm_unknown("agent", False)["code"], "NOT_AUTHORIZED")
        confirmed = proxy_confirm_unknown("trusted_proxy", False)
        self.assertTrue(confirmed["changed"])
        self.assertEqual(confirmed["state"], "UNKNOWN")
        self.assertEqual(renew_lease(10, 50, 2, 2), 3)
        self.assertIsNone(renew_lease(10, 50, 2, 3))
        store, seen, revoked = Store(), set(), set()
        rejected, store = call_idempotent(
            store, kind="test", proof_ref="test-proof://w00/fixture", allow=True, key="idem-real",
            payload_hash="h1", secret=True, server_now=10, expires_at=50, nonce="n0",
            seen_nonces=seen, revoked=revoked, isolated=True, route="real",
        )
        self.assertEqual(rejected["code"], "NOT_AUTHORIZED")
        self.assertNotIn("idem-real", store)
        bare, store = call_idempotent(
            store, kind="test", proof_ref="test-proof://w00/fixture", allow=True, key="idem-bare",
            payload_hash="h1", secret=True, server_now=10, expires_at=50, nonce="n0b",
            seen_nonces=seen, revoked=revoked, isolated=False, route="isolated",
        )
        self.assertEqual(bare["code"], "NOT_AUTHORIZED")
        self.assertNotIn("idem-bare", store)
        first, store = call_idempotent(
            store, kind="test", proof_ref="test-proof://w00/fixture", allow=True, key="idem-1",
            payload_hash="h1", secret=True, server_now=10, expires_at=50, nonce="n1",
            seen_nonces=seen, revoked=revoked, isolated=True, route="isolated",
        )
        self.assertEqual(first["lease_token"], "tok-req-1")
        denied, store = call_idempotent(
            store, kind="agent", proof_ref="test-proof://w00/fixture", allow=True, key="idem-2",
            payload_hash="h2", secret=True, server_now=10, expires_at=50, nonce="n2",
            seen_nonces=seen, revoked=revoked, isolated=True, route="isolated",
        )
        self.assertEqual(denied["code"], "NOT_AUTHORIZED")
        self.assertNotIn("idem-2", store)
        blocked, store = call_idempotent(
            store, kind="test", proof_ref="test-proof://w00/fixture", allow=False, key="idem-3",
            payload_hash="h3", secret=True, server_now=10, expires_at=50, nonce="n3",
            seen_nonces=seen, revoked=revoked, isolated=True, route="isolated",
        )
        self.assertEqual(blocked["code"], "POLICY_DENIED")
        self.assertNotIn("idem-3", store)
        store["idem-1"]["current_state"] = "RUNNING"
        replay, store = call_idempotent(
            store, kind="test", proof_ref="test-proof://w00/fixture", allow=True, key="idem-1",
            payload_hash="h1", secret=True, server_now=11, expires_at=50, nonce="n4",
            seen_nonces=seen, revoked=revoked, isolated=True, route="isolated",
        )
        self.assertIsNone(replay["lease_token"])
        self.assertEqual(replay["original_result"]["state"], "LEASED")
        self.assertEqual(replay["current_state"], "RUNNING")
        self.assertNotEqual(replay["original_result"]["state"], replay["current_state"])
        mismatch, store = call_idempotent(
            store, kind="test", proof_ref="test-proof://w00/fixture", allow=True, key="idem-1",
            payload_hash="h-other", secret=True, server_now=12, expires_at=50, nonce="n5",
            seen_nonces=seen, revoked=revoked, isolated=True, route="isolated",
        )
        self.assertEqual(mismatch["code"], "IDEMPOTENCY_PAYLOAD_MISMATCH")
        self.assertTrue(store["idem-1"]["token_issued"])
        again, _store = call_idempotent(
            store, kind="test", proof_ref="test-proof://w00/fixture", allow=True, key="idem-1",
            payload_hash="h1", secret=True, server_now=13, expires_at=50, nonce="n6",
            seen_nonces=seen, revoked=revoked, isolated=True, route="isolated",
        )
        self.assertIsNone(again["lease_token"])
        self.assertFalse(self.contract["identity"]["same_key_mints_second_token"])
        self.assertFalse(self.contract["identity"]["replay_returns_lease_token"])
        self.assertFalse(acceptance_allowed("user_acceptance", False))
        self.assertFalse(acceptance_allowed("macci", True))
        self.assertFalse(acceptance_allowed("agent", True))
        self.assertFalse(acceptance_allowed("candidate", True))
        self.assertTrue(acceptance_allowed("user_acceptance", True))

    def test_candidate_identity_and_event_projection(self):
        spec = self.contract["entities"]["candidate"]["id"]
        vector = spec["vector"]
        self.assertEqual(
            candidate_id(spec, vector["repo_id"], vector["task_id"], vector["head"], vector["base"], vector["merge"], vector["tree"]),
            vector["candidate_id"],
        )
        other_repo = candidate_id(spec, "other/repo", vector["task_id"], vector["head"], vector["base"], vector["merge"], vector["tree"])
        other_task = candidate_id(spec, vector["repo_id"], "tsk_other", vector["head"], vector["base"], vector["merge"], vector["tree"])
        self.assertNotEqual(other_repo, vector["candidate_id"])
        self.assertNotEqual(other_task, vector["candidate_id"])
        self.assertIsNone(re.fullmatch(r"[0-9a-f]{40}", vector["head"].upper()))
        task_event = {
            "event_id": "evt_1", "schema_version": 2, "event_type": "run.started", "event_class": "task",
            "repo_id": "layho/web-app-init-base-env", "producer_id": "fixture", "stream_id": "fixture:task",
            "stream_epoch": 1, "producer_seq": 1, "occurred_at": "2026-11-27T10:00:00Z", "idempotency_key": "idem-e",
            "app_id": "base", "host_id": None, "task_id": "tsk_example_w00", "run_id": "run_1",
            "attempt": 1, "revision": 1, "stage": "develop", "state": "RUNNING",
        }
        self.assertIsNone(validate_event(self.contract, task_event))
        missing = dict(task_event)
        missing.pop("run_id")
        self.assertEqual(validate_event(self.contract, missing), "SCHEMA_UNSUPPORTED")
        stamped = dict(task_event, received_at="2026-11-27T10:00:01Z")
        self.assertEqual(validate_event(self.contract, stamped), "SCHEMA_UNSUPPORTED")
        sha = "a" * 40
        first = dict(task_event, event_class="deployment", event_type="deployment.commit",
                     deployment_id="dep_1", host_id="host-test", candidate_id="cand_example",
                     head=sha, base="b" * 40, merge="c" * 40, tree="d" * 40, running_sha=sha,
                     artifact_digest="sha256:" + "e" * 64, previous_healthy_sha=None,
                     install_kind="first_install", stage="PLAN", state="PLAN",
                     trusted_source="host_deployer")
        for field in ("run_id", "attempt", "task_id"):
            first.pop(field, None)
        self.assertIsNone(validate_event(self.contract, first))
        upgrade = dict(first, install_kind="upgrade")
        self.assertEqual(validate_event(self.contract, upgrade), "EVIDENCE_MISSING")
        granted = {
            "event_id": "evt_auth", "schema_version": 2, "event_type": "authorization.granted",
            "event_class": "authorization", "repo_id": "layho/web-app-init-base-env",
            "producer_id": "policy_service", "stream_id": "policy_service:authorization",
            "stream_epoch": 1, "producer_seq": 1, "occurred_at": "2026-11-27T10:00:00Z",
            "idempotency_key": "idem-auth", "task_id": "tsk_example_w00", "candidate_id": "cand_example",
            "head": sha, "base": "b" * 40, "merge": "c" * 40, "tree": "d" * 40,
            "operation": "preview.merge", "policy_revision": 3, "revision": 3,
            "stage": "integrate", "state": "RECORDED", "evidence_ref": "policy://3",
            "trusted_source": "policy_service",
        }
        self.assertIsNone(validate_event(self.contract, granted))
        incomplete = dict(granted)
        incomplete.pop("operation")
        incomplete.pop("policy_revision")
        self.assertEqual(validate_event(self.contract, incomplete), "SCHEMA_UNSUPPORTED")
        self.assertEqual(
            self.contract["entities"]["event"]["event_types"]["deployment.commit"]["trusted_source"],
            "host_deployer",
        )
        applied, held = project([
            {"event_id": "d1", "stream_id": "macci:diagnostic", "stream_epoch": 1, "producer_seq": 1, "channel": "diagnostic"},
            {"event_id": "a2", "stream_id": "policy:authorization", "stream_epoch": 1, "producer_seq": 2, "channel": "authorization"},
            {"event_id": "a1", "stream_id": "policy:authorization", "stream_epoch": 1, "producer_seq": 1,
             "channel": "authorization", "classification": "never_existed"},
        ])
        self.assertEqual(applied, ["d1"])
        self.assertCountEqual(held, ["a2", "a1"])
        bypassed, bypass_held = project([
            {"event_id": "gap", "stream_id": "policy:authorization", "stream_epoch": 1, "producer_seq": 2, "channel": "authorization"},
            {"event_id": "new-epoch", "stream_id": "policy:authorization", "stream_epoch": 2, "producer_seq": 1, "channel": "authorization"},
        ])
        self.assertEqual(bypassed, [])
        self.assertCountEqual(bypass_held, ["gap", "new-epoch"])
        closed, _held = project([
            {"event_id": "close", "stream_id": "policy:authorization", "stream_epoch": 1, "producer_seq": 1,
             "channel": "authorization", "epoch_closed": True},
            {"event_id": "next", "stream_id": "policy:authorization", "stream_epoch": 2, "producer_seq": 1, "channel": "authorization"},
        ])
        self.assertEqual(closed, ["close", "next"])
        self.assertFalse(self.contract["entities"]["event"]["transitions"])
        self.assertTrue(self.contract["entities"]["event"]["delivery"]["transitions"])
        self.assertFalse(no_change_commit_allowed(None, "first_install", True, False))
        self.assertFalse(no_change_commit_allowed("b" * 40, "first_install", True, False))
        self.assertTrue(no_change_commit_allowed("b" * 40, "upgrade", True, False))
        self.assertEqual(first_install_rollback(self.contract, False), "SAFE_FROZEN")
        self.assertEqual(first_install_rollback(self.contract, True), "ROLLED_BACK")
        self.assertFalse(allows(self.contract, "deployment", "ROLLBACK", "ROLLED_BACK", ["old_health_proved"]))
        self.assertFalse(can_close(["SATISFIED", "SATISFIED"], [True, True], True))
        self.assertTrue(can_close(["PUBLISHED", "PUBLISHED"], [True, True], True))
        self.assertFalse(can_close(["PUBLISHED"], [True], False))
        self.assertFalse(self.contract["dependencies"]["same_base_required"])
        self.assertTrue(dependency_may_use_new_base(True, "new-base", "old-base", False))
        self.assertFalse(dependency_may_use_new_base(True, "new-base", "old-base", True))
        kept = prefer_fact(
            {"event_class": "deployment", "revision": 4, "authoritative": True},
            {"event_class": "diagnostic", "revision": 9, "authoritative": False},
        )
        self.assertEqual(kept["revision"], 4)
        self.assertFalse(self.contract["entities"]["task"]["reopen"])
        self.assertEqual(terminals_have_no_outgoing(self.contract), [])

    def test_examples_cover_writes_and_http_codes(self):
        by_code = {row["code"]: row["http"] for row in self.contract["error_codes"]}
        self.assertEqual(len(by_code), len(self.contract["error_codes"]))
        names = [example["name"] for example in self.contract["examples"]]
        self.assertEqual(len(names), len(set(names)))
        for operation in self.contract["write_operations"]:
            matches = [example for example in self.contract["examples"] if example["name"] == operation["name"]]
            self.assertEqual(len(matches), 1, operation["name"])
            example = matches[0]
            self.assertFalse(example["implemented"])
            self.assertEqual(example["method"], operation["method"])
            self.assertEqual(example["path"], operation["path"])
            self.assertTrue(example["side_effect"])
            for field in WRITE_FIELDS:
                self.assertIn(field, example["request"])
            self.assertIn(example["acceptance_owner"], {"user", "MacCI", "host", "publisher"})
            blob = json.dumps(example, ensure_ascii=False)
            for marker in SECRET_MARKERS:
                self.assertNotIn(marker, blob)
        replay = next(example for example in self.contract["examples"] if example["name"] == "task_claim_replay")
        self.assertIsNone(replay["response"]["lease_token"])
        self.assertNotEqual(replay["response"]["original_result"]["state"], replay["response"]["current_state"])
        for example in self.contract["examples"]:
            code = example["response"].get("code")
            if code:
                self.assertEqual(example["response"]["http"], by_code[code], example["name"])
        poll = next(example for example in self.contract["examples"] if example["name"] == "diagnostic_poll")
        self.assertFalse(poll["side_effect"])
        self.assertIn("after_seq", poll["path"])
        self.assertEqual(self.contract["acceptance_owners"]["technical_review"], "ChatGPT")
        self.assertEqual(self.contract["acceptance_owners"]["functional_acceptance"], "user")
        for name in ("task_renew", "run_complete", "run_start", "run_release", "run_heartbeat"):
            example = next(row for row in self.contract["examples"] if row["name"] == name)
            token = example["request"]["lease_token"]
            generation = example["request"]["lease_generation"]
            binding = example["request"]["lease_binding"]
            self.assertNotEqual(token, str(generation))
            self.assertNotIn("shown-once", token)
            self.assertEqual(binding["principal_id"], example["request"]["actor"]["id"])
            self.assertEqual(binding["run_id"], example["request"]["run_id"])
            self.assertEqual(binding["scope"], example["request"]["scope"]["repo"])
            self.assertEqual(binding["lease_generation"], generation)
        renew = next(row for row in self.contract["examples"] if row["name"] == "task_renew")
        self.assertNotEqual(renew["request"]["lease_token"], renew["response"]["lease_token"])
        stale = next(row for row in self.contract["examples"] if row["name"] == "run_finish_stale_lease")
        self.assertFalse(stale["response"]["changed"])
        self.assertNotEqual(stale["response"]["state"], "UNKNOWN")
        self.assertIn("signature", self.contract["identity"]["proof_fields"])
        self.assertIn("key_id", self.contract["identity"]["proof_fields"])
        for field in ("operation", "candidate_id", "policy_revision"):
            self.assertIn(field, self.contract["identity"]["signed_content"])

    def test_inventory_matches_design_and_matrix_rows(self):
        expected = design_ids()
        self.assertEqual(len(expected), 74)
        self.assertEqual(len(P1), 14)
        rows = {row["id"]: row for row in self.items["items"]}
        self.assertEqual(list(rows), expected)
        matrix = parse_matrix_rows(self.matrix)
        for item_id in expected:
            row = rows[item_id]
            self.assertEqual(row["package"], package_of(item_id))
            self.assertEqual(row["priority"], "P1" if item_id in P1 else "P0")
            self.assertEqual(row["category_label"], LABELS[row["category"]])
            self.assertIn(item_id, matrix)
            self.assertEqual(matrix[item_id]["priority"], row["priority"])
            self.assertEqual(matrix[item_id]["package"], row["package"])
            self.assertEqual(matrix[item_id]["category_label"], row["category_label"])
        supplements = {row["id"]: row for row in self.items["supplements"]}
        self.assertEqual(set(supplements), {"SUP-P0-1", "SUP-P0-2", "SUP-P0-3", "SUP-DATA", "SUP-SYNC", "SUP-F08", "SUP-197"})
        for item_id, row in supplements.items():
            self.assertEqual(matrix[item_id]["priority"], row["priority"])
            self.assertEqual(matrix[item_id]["package"], row["package"])
            self.assertEqual(matrix[item_id]["category_label"], row["category_label"])

    def test_documents_state_the_revised_rules(self):
        self.assertIn("PUBLISH_UNKNOWN", self.architecture)
        self.assertIn("首次安装", self.architecture)
        self.assertIn("不返回明文租约", self.architecture)
        self.assertIn("七行", self.architecture)
        self.assertNotIn("六行依次", self.architecture)
        self.assertIn("stream_id 加 stream_epoch", self.architecture)
        self.assertIn("Run.start", self.architecture)
        self.assertIn(":heartbeat", self.architecture)
        self.assertIn("changed 为 false", self.architecture)
        self.assertIn(APPROVED_SHA, self.architecture)
        self.assertIn("不是合并或发布授权", self.architecture)
        self.assertIn("进入 UNKNOWN 本身不允许重新领取", self.architecture)
        self.assertIn("after_seq", self.architecture)
        self.assertIn("W03 先交付隔离契约", self.ownership)
        self.assertIn("app/control_routes_", self.ownership)
        self.assertNotIn("全部 frontend", self.ownership)
        self.assertNotIn("默认仍读旧事件", self.plan)
        self.assertNotIn("生产认证行为的 diff 为空", self.plan)
        self.assertIn("逐条反馈", self.plan)
        self.assertIn("隔离验证", self.plan)
        self.assertNotIn("按小时汇总", self.plan)
        self.assertIn("轮换后拒绝旧代次", self.plan)
        self.assertIn("script-src", self.plan)
        self.assertIn("模型测试", self.architecture)
        self.assertIn("可信代理", self.architecture)
        self.assertIn("key_id", self.architecture)
        self.assertIn("W06 不修改", self.ownership)
        self.assertIn("app/control_routes_w08.py", self.ownership)
        self.assertIn("app/control_routes_w09.py", self.ownership)
        self.assertFalse(proxy_allow(2, 1, "feedback", "feedback"))
        self.assertFalse(proxy_allow(2, 2, "feedback", "merge"))
        self.assertTrue(proxy_allow(2, 2, "feedback", "feedback"))
        durable = {"count": 10, "revoked": {"session-old"}}
        process = {"count": 0, "revoked": set()}
        process.clear()
        self.assertEqual(durable["count"], 10)
        self.assertIn("session-old", durable["revoked"])
        self.assertFalse(web_boundary_ok("default-src 'self'", "ok"))
        self.assertFalse(web_boundary_ok("script-src 'self'", "github_pat_example"))
        self.assertTrue(web_boundary_ok("script-src 'self'", "request failed"))


if __name__ == "__main__":
    unittest.main()
