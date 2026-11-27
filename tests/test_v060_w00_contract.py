"""Validate V0.6.0 W00 fixtures. No network, database, or host access."""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "v060" / "w00"
MATRIX = ROOT / "docs" / "v060" / "01-capability-matrix.md"
BASE = "db19bff19307fa763fd50e94b4cedfda7181a141"
WRITE_FIELDS = ("actor", "scope", "expected_revision", "idempotency_key")
SECRET_MARKERS = ("github_pat_", "ghp_", "sk-", "AKIA", "BEGIN PRIVATE KEY")


def load(name):
    return json.loads((FIXTURE / name).read_text(encoding="utf-8"))


class ContractFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = load("contract.json")
        cls.items = load("items.json")
        cls.matrix = MATRIX.read_text(encoding="utf-8")

    def test_draft_is_not_implemented_and_matches_baseline(self):
        contract = self.contract
        self.assertEqual(contract["schema_version"], 1)
        self.assertEqual(contract["status"], "draft")
        self.assertFalse(contract["normative"])
        self.assertFalse(contract["exactly_once_across_github_and_db"])
        self.assertEqual(contract["baseline_sha"], BASE)
        self.assertEqual(contract["issue"], 204)
        self.assertEqual(self.items["baseline_sha"], BASE)
        self.assertEqual(self.items["counts"], {"items": 74, "p0": 60, "p1": 14})

    def test_entities_transitions_and_terminals(self):
        entities = self.contract["entities"]
        self.assertEqual(
            set(entities),
            {"task", "run", "candidate", "release", "deployment", "resource", "event"},
        )
        for name, entity in entities.items():
            if name == "task":
                states = set(entity["phases"])
                terminals = set(entity["terminal_phases"])
            else:
                states = set(entity["states"])
                terminals = set(entity["terminal"])
            seen = set()
            for row in entity["transitions"]:
                self.assertIn(row["from"], states)
                self.assertIn(row["to"], states)
                self.assertNotIn(row["from"], terminals)
                key = (row["from"], row["to"], row.get("guard", ""))
                self.assertNotIn(key, seen)
                seen.add(key)
                if "guard" in row:
                    self.assertTrue(row["guard"].strip())
            outgoing = {row["from"] for row in entity["transitions"]}
            self.assertTrue(terminals.isdisjoint(outgoing))

    def test_candidate_and_event_identity(self):
        candidate = self.contract["entities"]["candidate"]
        event = self.contract["entities"]["event"]
        self.assertEqual(candidate["required_identity"], ["head", "base", "merge", "tree"])
        for field in ("event_id", "schema_version", "producer_id", "producer_seq", "idempotency_key"):
            self.assertIn(field, event["required_fields"])

    def test_error_codes_are_unique(self):
        codes = [row["code"] for row in self.contract["error_codes"]]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertIn("LEASE_FENCE_REJECTED", codes)
        self.assertIn("PLATFORM_UNSUPPORTED", codes)
        self.assertIn("PRODUCTION_DB_FORBIDDEN", codes)

    def test_examples_have_no_side_effects_enabled(self):
        names = set()
        for example in self.contract["examples"]:
            self.assertFalse(example["implemented"])
            self.assertNotIn(example["name"], names)
            names.add(example["name"])
            blob = json.dumps(example, ensure_ascii=False)
            for marker in SECRET_MARKERS:
                self.assertNotIn(marker, blob)
            if example["side_effect"]:
                for field in WRITE_FIELDS:
                    self.assertIn(field, example["request"])
            else:
                self.assertIn(example["method"], {"GET", "POST"})
        self.assertIn("task_claim", names)
        self.assertIn("event_query", names)
        stale = next(row for row in self.contract["examples"] if row["name"] == "run_finish_stale_lease")
        self.assertEqual(stale["response"]["code"], "LEASE_FENCE_REJECTED")
        gap = next(row for row in self.contract["examples"] if row["name"] == "event_out_of_order")
        self.assertFalse(gap["response"]["applied"])
        self.assertEqual(gap["response"]["watermark_seq"], 7)
        query = next(row for row in self.contract["examples"] if row["name"] == "event_query")
        self.assertFalse(query["side_effect"])
        self.assertEqual(query["method"], "GET")

    def test_item_inventory_matches_the_matrix(self):
        rows = self.items["items"]
        self.assertEqual(len(rows), 74)
        self.assertEqual(len({row["id"] for row in rows}), 74)
        self.assertEqual(sum(row["priority"] == "P0" for row in rows), 60)
        self.assertEqual(sum(row["priority"] == "P1" for row in rows), 14)
        allowed = {"covered", "remaining", "outdated", "unproven"}
        labels = {"covered": "已覆盖", "remaining": "独有待做", "outdated": "过时", "unproven": "未证实"}
        for row in rows + self.items["supplements"]:
            self.assertIn(row["category"], allowed)
            self.assertEqual(row["category_label"], labels[row["category"]])
            self.assertIn(row["id"], self.matrix)
            self.assertIn(row["category_label"], self.matrix)
        supplement_ids = {row["id"] for row in self.items["supplements"]}
        self.assertEqual(
            supplement_ids,
            {"SUP-P0-1", "SUP-P0-2", "SUP-P0-3", "SUP-DATA", "SUP-SYNC", "SUP-F08", "SUP-197"},
        )
        for review in self.items["special_reviews"]:
            self.assertIn(f"#{review['id']}" if not str(review["id"]).startswith("#") else review["id"], self.matrix)
            self.assertIn(review["id"], self.matrix)


if __name__ == "__main__":
    unittest.main()
